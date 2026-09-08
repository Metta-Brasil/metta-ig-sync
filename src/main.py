"""Entry point for metta-ig-sync.

Usage:
    python -m src.main

Environment variables required:
    INSTAGRAM_ACCESS_TOKEN       — long-lived Instagram Graph API token
    GOOGLE_SERVICE_ACCOUNT_JSON  — service account key (raw JSON or base64)

Optional:
    DRY_RUN=true                 — skip all writes to Google Sheets
    METTA_INSTAGRAM_USER_ID      — override default Metta IG user ID
    TIAGO_INSTAGRAM_USER_ID      — override default Tiago IG user ID
    META_ADS_ACCESS_TOKEN        — token com ads_read; descobre os posts
                                   impulsionados sem ninguém digitar link
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta

from . import boosted, config
from . import ig_web_insights as web
from .instagram import BRT, IGClient, parse_media_date, parse_media_datetime
from .sheets import (
    _build_service,
    boosted_hist_upsert,
    boosted_input_read,
    boosted_input_write,
    demographics_overwrite,
    posts_overwrite,
    profile_upsert,
    profile_upsert_partial,
    stories_upsert,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)


def _build_post_row(media: dict, insights: dict) -> dict:
    """Merge media fields and insights into a single row dict."""
    media_type = media.get("media_type", "")
    ts = media.get("timestamp", "")
    post_date = parse_media_date(ts) if ts else date.today()

    # Extract BRT hour for the new "Hora" column
    if ts:
        dt_brt = parse_media_datetime(ts)
        hora = dt_brt.strftime("%H:%M")
    else:
        hora = ""

    caption_raw = media.get("caption") or ""
    caption = caption_raw[:500]

    thumbnail_url = (
        media.get("thumbnail_url")
        or media.get("media_url")
        or ""
    )

    likes = insights.get("likes") or int(media.get("like_count") or 0)
    comments = insights.get("comments") or int(media.get("comments_count") or 0)
    reach = int(insights.get("reach") or 0)
    saved = int(insights.get("saved") or 0)
    shares = int(insights.get("shares") or 0)
    reposts = int(insights.get("reposts") or 0)
    views = int(insights.get("views") or 0)
    skip_rate = float(insights.get("skip_rate") or 0.0)
    profile_visits = int(insights.get("profile_visits") or 0)
    follows = int(insights.get("follows") or 0)

    engagement_num = likes + comments + saved + shares
    engagement_rate = round(engagement_num / reach * 100, 2) if reach > 0 else 0.0

    return {
        "post_id": media.get("id", ""),
        "date": post_date,
        "media_type": media_type,
        "caption": caption,
        "permalink": media.get("permalink", ""),
        "thumbnail_url": thumbnail_url,
        "likes": likes,
        "comments": comments,
        "views": views,
        "reach": reach,
        "saved": saved,
        "shares": shares,
        "reposts": reposts,
        "skip_rate": skip_rate,
        "engagement_rate": engagement_rate,
        "hora": hora,
        "profile_visits": profile_visits,
        "follows": follows,
    }


def _build_story_row(story: dict, today: date) -> dict:
    """Merge a story's fields + insights into a sheet row. `coletado_em` = today."""
    ts = story.get("timestamp", "")
    story_date = parse_media_date(ts) if ts else today
    hora = parse_media_datetime(ts).strftime("%H:%M") if ts else ""
    return {
        "story_id": story.get("story_id", ""),
        "date": story_date,
        "hora": hora,
        "media_type": story.get("media_type", ""),
        "permalink": story.get("permalink", ""),
        "thumbnail_url": story.get("thumbnail_url", ""),
        "views": int(story.get("views") or 0),
        "reach": int(story.get("reach") or 0),
        "navigation": int(story.get("navigation") or 0),
        "replies": int(story.get("replies") or 0),
        "shares": int(story.get("shares") or 0),
        "total_interactions": int(story.get("total_interactions") or 0),
        "follows": int(story.get("follows") or 0),
        "profile_visits": int(story.get("profile_visits") or 0),
        "coletado_em": today,
    }


def _build_profile_row(profile: dict, reach_28d: int, extra: dict, today: date) -> dict:
    """Build the profile row dict for the spreadsheet.

    `today` is the BRT date (passed in) — not date.today(), which on the UTC
    GitHub runner would roll to D+1 during the 21:00–24:00 BRT window.
    """
    return {
        "date": today,
        "followers": profile["followers"],
        "following": profile["following"],
        "posts": profile["posts"],
        "reach_28d": reach_28d,
        "alcance_dia": extra.get("alcance_dia", 0),
        "contas_engajadas_dia": extra.get("contas_engajadas_dia", 0),
        "interacoes_dia": extra.get("interacoes_dia", 0),
        "views_dia": extra.get("views_dia", 0),
    }


def sync_account(svc, account: dict, token: str) -> bool:
    """Sync one Instagram account. Returns True on success, False on failure."""
    name = account["name"]
    user_id = account["user_id"]
    sheet_profile = account["sheet_profile"]
    sheet_posts = account["sheet_posts"]

    log.info("=== Starting sync for account: %s (user_id=%s) ===", name, user_id)

    client = IGClient(token=token, user_id=user_id)
    today = datetime.now(BRT).date()

    # --- Profile snapshot (today: full row) ---
    try:
        profile = client.get_profile()
        reach_28d = client.get_reach_28d()
        extra = client.get_daily_metrics_today()
        profile_row = _build_profile_row(profile, reach_28d, extra, today)
        log.info(
            "[%s] Profile %s: followers=%d, following=%d, posts=%d, reach_28d=%d, "
            "alcance_dia=%d, views=%d, contas_engajadas=%d, interacoes=%d",
            name, today, profile["followers"], profile["following"], profile["posts"],
            reach_28d, extra["alcance_dia"], extra["views_dia"],
            extra["contas_engajadas_dia"], extra["interacoes_dia"],
        )
        profile_upsert(svc, sheet_profile, profile_row)
    except Exception as exc:
        log.error("[%s] Profile sync failed: %s", name, exc, exc_info=True)
        return False

    # --- Rolling reconciliation window (D-1, D-2: daily metrics only) ---
    # The API finalizes insight data within ~48h. Re-fetching D-1/D-2 lets the
    # daily cells settle to their final values; days older than D-2 fall out of
    # the window and are never touched again, so closed periods freeze on their
    # own. A failure here must NOT block the posts sync below.
    try:
        for delta in (1, 2):
            day = today - timedelta(days=delta)
            metrics = client.get_daily_metrics_for_day(day)
            profile_upsert_partial(svc, sheet_profile, day, metrics)
    except Exception as exc:
        log.error("[%s] Reconciliation window failed (non-fatal): %s", name, exc, exc_info=True)

    # --- Posts ---
    try:
        media_list = client.get_media_list(max_posts=100)
        log.info("[%s] Fetched %d media items.", name, len(media_list))
    except Exception as exc:
        log.error("[%s] Media list fetch failed: %s", name, exc, exc_info=True)
        return False

    post_rows = []
    for i, media in enumerate(media_list, start=1):
        media_id = media.get("id", "")
        media_type = media.get("media_type", "")

        if i % 10 == 0 or i == 1:
            log.info("[%s] Fetching insights: %d/%d (post_id=%s)", name, i, len(media_list), media_id)

        insights = client.get_post_insights(media_id, media_type)
        row = _build_post_row(media, insights)
        post_rows.append(row)

    try:
        posts_overwrite(svc, sheet_posts, post_rows)
    except Exception as exc:
        log.error("[%s] Posts write failed: %s", name, exc, exc_info=True)
        return False

    # --- Follower demographics (overwrite daily) ---
    # Non-fatal: a failure here must not fail the whole account sync.
    sheet_demographics = account.get("sheet_demographics")
    if sheet_demographics:
        try:
            demo_rows = client.get_follower_demographics()
            for r in demo_rows:
                r["coletado_em"] = today
            demographics_overwrite(svc, sheet_demographics, demo_rows)
            log.info("[%s] Demographics: %d rows written to %s.", name, len(demo_rows), sheet_demographics)
        except Exception as exc:
            log.error("[%s] Demographics sync failed (non-fatal): %s", name, exc, exc_info=True)

    # --- Stories (append-only upsert por id; preserva histórico ao expirar) ---
    # Non-fatal: uma falha aqui não pode derrubar o sync da conta.
    sheet_stories = account.get("sheet_stories")
    if sheet_stories:
        try:
            stories = client.get_stories()
            story_rows = [_build_story_row(s, today) for s in stories]
            stories_upsert(svc, sheet_stories, story_rows)
            log.info("[%s] Stories: %d ativos upsertados em %s.", name, len(story_rows), sheet_stories)
        except Exception as exc:
            log.error("[%s] Stories sync failed (non-fatal): %s", name, exc, exc_info=True)

    log.info("=== Done: %s — %d posts synced ===", name, len(post_rows))
    return True


def sync_boosted(svc, token: str) -> bool:
    """Série diária de visitas/seguidores dos posts impulsionados.

    Roda uma vez por execução, não por conta: a lista é comum às duas e um
    media_id só é visível pelo token da conta dona, então cada post é
    tentado em cada conta até uma responder.

    Retorna False só em erro inesperado — post que não resolve ou que a API
    não cobre (Reels) é registrado e segue o baile.
    """
    log.info("=== Impulsionamentos ===")

    clients = {a["name"]: IGClient(token, a["user_id"]) for a in config.ACCOUNTS}
    entrada = boosted_input_read(svc)

    # media_id conhecido → o que a planilha já sabe daquele post
    conhecidos: dict = {}
    pendentes: dict = {}  # shortcode → link, ainda sem media_id
    for row in entrada:
        if row["media_id"]:
            conhecidos[row["media_id"]] = row
        else:
            sc = boosted.extract_shortcode(row["link"])
            if sc:
                pendentes[sc] = row["link"]
            else:
                log.warning("Impulsionados: linha ignorada, link inválido: %r", row["link"])

    descobertos, ad_ids = boosted.discover_from_ads(config.META_ADS_ACCESS_TOKEN)
    # Token de ads configurado e descoberta vazia = alguma coisa quebrou
    # (token recusado, conta sem acesso, mudança de nomenclatura). Antes isso
    # passava como sucesso: a aba parava de crescer e nada acusava, enquanto
    # o histórico dos posts já conhecidos continuava enchendo normalmente.
    if config.META_ADS_ACCESS_TOKEN and not descobertos:
        raise RuntimeError(
            "descoberta de impulsionamentos voltou vazia com token de ads "
            "configurado — token recusado ou nomenclatura mudou"
        )
    for mid, campanha in descobertos.items():
        alvo = conhecidos.setdefault(
            mid, {"media_id": mid, "link": "", "origem": "meta_ads"}
        )
        # Vale também pra linha que veio da planilha: quem sabe o nome do
        # anúncio é o Meta Ads, não quem colou o link.
        alvo["campanha"] = campanha

    # Resolve os links colados que ainda não têm media_id, em cada conta
    if pendentes:
        for nome, client in clients.items():
            if not pendentes:
                break
            achados = boosted.resolve_shortcodes(client, list(pendentes))
            for sc, mid in achados.items():
                conhecidos.setdefault(mid, {
                    "media_id": mid, "link": pendentes.pop(sc), "origem": "manual",
                })
    for sc, link in pendentes.items():
        log.warning("Impulsionados: não achei o post do link %s (post muito antigo?).", link)

    if not conhecidos:
        log.info("Impulsionados: nenhum post na lista, nada a coletar.")
        return True

    # Sessões web (cookie por conta). Falha de sessão não derruba o sync: as
    # colunas da Graph API continuam, e o log diz qual conta ficou sem.
    sessoes: dict = {}
    for nome, sid in config.IG_SESSIONIDS.items():
        if not sid or sid == "PREENCHER":
            log.warning("Impulsionados: sem cookie de sessão para %s — "
                        "colunas (IG)/Anúncio ficarão vazias nessa conta.", nome)
            continue
        try:
            sess = web.WebSession(sid)
            sess.preparar()
            sessoes[nome] = sess
            log.info("Impulsionados: sessão web de %s pronta.", nome)
        except Exception as exc:
            log.error("Impulsionados: sessão web de %s falhou: %s", nome, exc)

    dia = datetime.now(BRT).date()
    agora = boosted.now_brt_hhmm()
    hist_rows: list = []
    input_rows: list = []

    for mid, meta in conhecidos.items():
        dados = None
        conta = ""
        for nome, client in clients.items():
            dados = boosted.collect(client, mid)
            if dados:
                conta = nome
                break

        if not dados:
            # Post de conta fora do grupo, apagado, ou token sem acesso.
            log.warning("Impulsionados: media %s não visível por nenhuma conta.", mid)
            input_rows.append({
                "link": meta.get("link", ""), "media_id": mid, "conta": "",
                "campanha": meta.get("campanha", ""),
                "tipo": "", "tem_dado": "nao (post inacessivel)",
                "origem": meta.get("origem", "manual"),
            })
            continue

        # Dono real do post, não "quem respondeu primeiro": os dois tokens
        # enxergam as mídias das duas contas.
        dono = {"tiago.alves.oliveira": "tiago",
                "metta.brasil": "metta"}.get(dados.get("dono", ""), conta)

        dados_web = {}
        sess = sessoes.get(dono)
        if sess:
            try:
                dados_web = web.coletar(
                    sess, web.pk_from_link(dados["link"]), ad_ids.get(mid, "")
                )
            except Exception as exc:
                log.warning("Impulsionados: insights web de %s falharam: %s",
                            mid, exc)

        tem = dados["tipo"] == "FEED"
        input_rows.append({
            "link": dados["link"], "media_id": mid, "conta": dono,
            "campanha": meta.get("campanha", ""),
            "tipo": dados["tipo"],
            "tem_dado": "sim" if tem else f"nao ({dados['tipo'].lower()})",
            "origem": meta.get("origem", "manual"),
        })
        hist_rows.extend(
            boosted.build_rows([dados], dono, dia, agora,
                               meta.get("campanha", ""), dados_web)
        )

    com_dado = sum(1 for r in input_rows if r["tem_dado"] == "sim")
    inacessiveis = [r["media_id"] for r in input_rows
                    if r["tem_dado"] == "nao (post inacessivel)"]
    log.info(
        "Impulsionados: %d post(s), %d com visitas/seguidores disponíveis.",
        len(input_rows), com_dado,
    )
    # Post que nenhuma conta enxerga trava a série daquele post. Um ou outro
    # é esperado (post apagado); metade da lista é sinal de token vencido.
    if inacessiveis and len(inacessiveis) > len(input_rows) / 2:
        raise RuntimeError(
            f"{len(inacessiveis)} de {len(input_rows)} posts inacessíveis — "
            "token do Instagram provavelmente vencido"
        )
    if inacessiveis:
        log.warning("Impulsionados: %d post(s) inacessível(is): %s",
                    len(inacessiveis), ", ".join(inacessiveis[:10]))

    boosted_input_write(svc, input_rows)
    boosted_hist_upsert(svc, hist_rows)
    return True


def main() -> int:
    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    if not token:
        log.error("INSTAGRAM_ACCESS_TOKEN env var is required.")
        return 1

    if config.DRY_RUN:
        log.info("DRY_RUN=true — no writes will be made to Google Sheets.")

    try:
        svc = _build_service()
    except Exception as exc:
        log.error("Failed to build Google Sheets service: %s", exc, exc_info=True)
        return 1

    accounts = config.ACCOUNTS
    if config.SYNC_ONLY:
        accounts = [a for a in accounts if a["name"] == config.SYNC_ONLY]
        log.info("SYNC_ONLY=%s — syncing %d account(s).", config.SYNC_ONLY, len(accounts))
        if not accounts:
            log.error("SYNC_ONLY=%s matched no account.", config.SYNC_ONLY)
            return 1

    any_failed = False
    for account in accounts:
        success = sync_account(svc, account, token)
        if not success:
            any_failed = True

    try:
        sync_boosted(svc, token)
    except Exception as exc:
        # Coleta nova e isolada: não derruba o sync das abas que já rodavam.
        log.error("Impulsionados falhou: %s", exc, exc_info=True)
        any_failed = True

    if any_failed:
        log.error("One or more accounts failed to sync.")
        return 1

    log.info("All accounts synced successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
