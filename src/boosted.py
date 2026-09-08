"""Impulsionamentos: série diária de visitas ao perfil e seguidores por post.

Por que este módulo existe
--------------------------
A aba ig_*_posts guarda o ACUMULADO de cada post desde a publicação, e é
reescrita a cada execução. Serve pra saber "quanto esse post já rendeu",
mas não responde "quanto rendeu em julho": a Media Insights API não recorta
por data — passar since/until ou period=day é aceito e ignorado, a resposta
volta sempre com period=lifetime.

A saída daqui é uma linha por (dia, post). Durante o dia as execuções
atualizam a linha do dia; virou a data, abre linha nova. O ganho de um dia
é a diferença entre o fechamento dele e o do dia anterior — que é o número
que o dashboard mostra.

Isso só funciona daqui pra frente: snapshot que não foi tirado não se
reconstrói. Para o período anterior à primeira execução só existe o
acumulado.

De onde sai a lista de posts
----------------------------
Duas fontes, unidas:

1. Meta Ads API — o criativo de um impulsionamento expõe
   `source_instagram_media_id`, que aponta pro post ORIGINAL do perfil.
   Cobre os anúncios sozinho, ativos ou pausados, sem ninguém digitar nada.
   Não confundir com `effective_instagram_media_id`: esse é a CÓPIA que o
   Meta cria pro anúncio (media_product_type=AD), e a Insights API recusa
   profile_visits/follows para ela.

2. A coluna A da aba de entrada — pra forçar um post que a descoberta não
   pegou (conta de anúncio fora da lista, post de parceiro, etc). Colar o
   link basta; o resto das colunas o sync preenche.

Reels ficam sem visitas/seguidores: a API recusa a métrica para
media_product_type=REELS. A linha é gravada mesmo assim, com as células
vazias, pra diferenciar "não temos o dado" de "deu zero".
"""

import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

from . import config
from .instagram import BRT

log = logging.getLogger(__name__)

_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)")
_TOKEN_RE = re.compile(r"access_token=[^&\s]+")

# Teto de páginas por conta de anúncio. Conta antiga tem milhares de anúncios;
# sem teto a descoberta viraria a parte mais cara do sync pra achar os mesmos
# poucos impulsionamentos, que são sempre os mais recentes.
MAX_PAGINAS_ADS = 12

# O Meta nomeia impulsionamento como "Post do Instagram: <início da legenda>".
# Sem esse filtro, QUALQUER anúncio que reaproveite uma mídia do perfil entra
# na lista (source_instagram_media_id existe em todos eles) e a aba enche de
# post que ninguém turbinou. Anúncio que escape do padrão entra pela coluna A.
_NOME_IMPULSIONAMENTO = ("post do instagram", "instagram post")


def is_impulsionamento(nome: str) -> bool:
    n = (nome or "").strip().lower()
    return any(n.startswith(p) for p in _NOME_IMPULSIONAMENTO)


def _redact(msg: Any) -> str:
    return _TOKEN_RE.sub("access_token=REDACTED", str(msg))


def extract_shortcode(link: str) -> str:
    """Shortcode de um permalink. '' se o texto não for um link de post."""
    m = _SHORTCODE_RE.search((link or "").strip())
    return m.group(1) if m else ""


# ----------------------------------------------------------------------
# Fonte 1: descoberta pelo Meta Ads
# ----------------------------------------------------------------------

def discover_from_ads(token: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """({media_id: nome do anúncio}, {media_id: ad_id}) dos impulsionamentos.

    Só entram anúncios COM ENTREGA na janela. O ad_id sai junto porque é o
    `adgroup_id` que a query de insights do anúncio exige.

    O critério é entrega, não posição na lista nem data de criação.

    Histórico das duas tentativas anteriores, porque as duas quebravam:
      1. Varredura cega de `/ads`, 50 por página, teto de 12 páginas. A CA01
         e a METTA-CA01 têm 2.000 anúncios cada e havia impulsionamento na
         posição 1942 — ficava de fora, em silêncio.
      2. Tirar o teto e filtrar por nome no servidor. Aí aparecem 185 posts,
         quase todos de 2023/24, e o sync passaria a coletar insight de 185
         mídias por hora pra acompanhar ~25 que interessam.

    Entrega resolve os dois: `/insights` a nível de anúncio só devolve o que
    rodou na janela. Hoje são 30 anúncios em 19 páginas no ano inteiro,
    contra 2.000+ da varredura cega.

    Falha em silêncio (dicionário vazio) — sem token de ads a aba manual
    continua funcionando, e o sync das outras abas não pode quebrar por
    causa disso. O chamador registra quando vem vazio.
    """
    if not token:
        return {}, {}

    base = config.IG_BASE_URL
    sess = requests.Session()

    def get(path: str, **params: Any) -> Dict[str, Any]:
        params["access_token"] = token
        try:
            r = sess.get(f"{base}/{path}", params=params, timeout=60)
            return r.json()
        except Exception as exc:
            log.warning("discover_from_ads: falha em %s: %s", path, _redact(exc))
            return {}

    def get_url(url: str) -> Dict[str, Any]:
        try:
            return sess.get(url, timeout=60).json()
        except Exception as exc:
            log.warning("discover_from_ads: falha ao paginar: %s", _redact(exc))
            return {}

    accounts = get("me/adaccounts", fields="id", limit=100).get("data", [])
    if not accounts:
        log.warning("discover_from_ads: nenhuma conta de anúncio acessível.")
        return {}, {}

    hoje = datetime.now(BRT).date()
    desde = hoje - timedelta(days=config.BOOSTED_DISCOVERY_DAYS)
    janela = json.dumps({"since": desde.isoformat(), "until": hoje.isoformat()})

    # 1º passe: quais anúncios de impulsionamento tiveram entrega na janela.
    nomes: Dict[str, str] = {}
    for acct in accounts:
        params = {
            "level": "ad",
            "fields": "ad_id,ad_name",
            "time_range": janela,
            "limit": 500,
        }
        path = f"{acct['id']}/insights"
        paginas = 0
        while path and paginas < MAX_PAGINAS_ADS:
            data = get(path, **params) if paginas == 0 else get_url(path)
            if not data or "error" in data:
                if data:
                    log.warning(
                        "discover_from_ads: %s: %s",
                        acct["id"], str(data["error"].get("message"))[:120],
                    )
                break
            for row in data.get("data", []):
                if is_impulsionamento(row.get("ad_name", "")):
                    nomes[str(row["ad_id"])] = (row.get("ad_name") or "").strip()
            path = data.get("paging", {}).get("next")
            paginas += 1

    if not nomes:
        log.warning(
            "discover_from_ads: nenhum impulsionamento com entrega nos "
            "últimos %d dias.", config.BOOSTED_DISCOVERY_DAYS,
        )
        return {}, {}

    # 2º passe: o media_id do post ORIGINAL. `/insights` não devolve criativo,
    # então é uma leitura em lote por ids — 1 chamada a cada 50 anúncios.
    found: Dict[str, str] = {}
    ad_ids: Dict[str, str] = {}
    ids = list(nomes)
    for k in range(0, len(ids), 50):
        lote = ids[k:k + 50]
        data = get("", ids=",".join(lote),
                   fields="creative{source_instagram_media_id}")
        if not data or "error" in data:
            if data:
                log.warning(
                    "discover_from_ads: lote de criativos: %s",
                    str(data.get("error", {}).get("message"))[:120],
                )
            continue
        for ad_id, ad in data.items():
            mid = ((ad or {}).get("creative") or {}).get("source_instagram_media_id")
            if mid and mid not in found:
                found[mid] = nomes.get(str(ad_id), "")
                ad_ids[mid] = str(ad_id)

    log.info(
        "discover_from_ads: %d anúncio(s) com entrega em %d dias -> %d post(s) "
        "impulsionado(s), em %d conta(s).",
        len(nomes), config.BOOSTED_DISCOVERY_DAYS, len(found), len(accounts),
    )
    return found, ad_ids


# ----------------------------------------------------------------------
# Fonte 2: links colados na aba
# ----------------------------------------------------------------------

def resolve_shortcodes(client, shortcodes: List[str], max_scan: int = 600) -> Dict[str, str]:
    """{shortcode: media_id} varrendo as mídias da conta.

    A Graph API não resolve shortcode → media_id direto, então a única
    saída é paginar o perfil comparando permalinks. Para assim que acha
    todos os procurados, e o chamador guarda o media_id na planilha — o
    custo é uma vez por post novo, não uma vez por execução.
    """
    alvo = {s for s in shortcodes if s}
    if not alvo:
        return {}

    out: Dict[str, str] = {}
    scanned = 0
    for media in client.iter_media(max_items=max_scan):
        scanned += 1
        sc = extract_shortcode(media.get("permalink", ""))
        if sc in alvo:
            out[sc] = media["id"]
            alvo.discard(sc)
            if not alvo:
                break

    if alvo:
        log.warning(
            "resolve_shortcodes: %d link(s) não encontrado(s) nos %d posts mais "
            "recentes: %s", len(alvo), scanned, ", ".join(sorted(alvo)),
        )
    return out


# ----------------------------------------------------------------------
# Coleta
# ----------------------------------------------------------------------

def collect(client, media_id: str) -> Optional[Dict[str, Any]]:
    """Metadados + insights de um post. None se o post não é da conta.

    Um media_id que veio do Meta Ads pode pertencer à outra conta (metta vs
    tiago) — nesse caso o token atual não enxerga e a resposta é erro. Isso
    é esperado, não é falha: o chamador tenta a outra conta.
    """
    meta = client.get_media_meta(media_id)
    if not meta:
        return None

    ins = client.get_boost_insights(media_id, meta.get("media_product_type", ""))
    return {
        "media_id": media_id,
        "dono": meta.get("username", ""),
        "link": meta.get("permalink", ""),
        "legenda": (meta.get("caption") or "").replace("\n", " ")[:120],
        "tipo": meta.get("media_product_type", ""),
        **ins,
    }


def build_rows(
    coletados: List[Dict[str, Any]],
    conta: str,
    dia: date,
    agora: str,
    campanha: str = "",
    web: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Uma linha de histórico por post coletado."""
    web = web or {}
    return [
        {
            "data": dia,
            "media_id": c["media_id"],
            "conta": conta,
            "link": c["link"],
            "legenda": c["legenda"],
            "tipo": c["tipo"],
            "profile_visits": c.get("profile_visits", ""),
            "follows": c.get("follows", ""),
            "reach": c.get("reach", ""),
            "views": c.get("views", ""),
            "atualizado": agora,
            "campanha": campanha,
            **{k: web.get(k, "") if web.get(k) is not None else ""
               for k in ("web_profile_visits", "web_follows", "web_reach",
                         "ad_profile_visits", "ad_follows", "ad_reach",
                         "ad_views")},
        }
        for c in coletados
    ]


def now_brt_hhmm() -> str:
    return datetime.now(BRT).strftime("%H:%M")
