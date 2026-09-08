"""Instagram Graph API client for metta-ig-sync."""

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

from . import config

log = logging.getLogger(__name__)

BRT = ZoneInfo("America/Sao_Paulo")

# requests coloca a URL inteira na mensagem da exceção, e a URL leva o
# access_token. O GitHub mascara secrets no log do Actions, mas execução local
# e qualquer log agregado não mascaram — então o token sai daqui redigido.
_TOKEN_RE = re.compile(r"access_token=[^&\s]+")


def _redact(msg: Any) -> str:
    return _TOKEN_RE.sub("access_token=REDACTED", str(msg))

# Metrics to request per media type
_COMMON_INSIGHT_METRICS = "views,reach,saved,shares"
_VIDEO_EXTRA_METRICS = ",likes,comments,reposts,reels_skip_rate"
# profile_visits/follows are supported ONLY for FEED media (IMAGE/CAROUSEL_ALBUM);
# requesting them for a Reel returns "(#100) ... does not support ... for this
# media product type". So they go only on the non-VIDEO branch.
_FEED_EXTRA_METRICS = ",profile_visits,follows"


class IGClient:
    def __init__(self, token: str, user_id: str) -> None:
        self._token = token
        self._user_id = user_id
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET with retry/backoff on transient errors. Raises on unrecoverable errors."""
        url = f"{config.IG_BASE_URL}/{path.lstrip('/')}"
        p = {"access_token": self._token}
        if params:
            p.update(params)

        last_exc: Optional[Exception] = None
        for attempt in range(1, config.RETRY_MAX_ATTEMPTS + 1):
            try:
                resp = self._session.get(url, params=p, timeout=30)
                if resp.status_code in config.RETRYABLE_HTTP:
                    wait = config.RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                    log.warning(
                        "HTTP %s for %s (attempt %d/%d), retrying in %ds",
                        resp.status_code, path, attempt, config.RETRY_MAX_ATTEMPTS, wait,
                    )
                    time.sleep(wait)
                    continue
                if not resp.ok:
                    log.warning(
                        "HTTP %s for %s: %s",
                        resp.status_code, path, resp.text[:600],
                    )
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                wait = config.RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                log.warning(
                    "Request error for %s (attempt %d/%d): %s, retrying in %ds",
                    path, attempt, config.RETRY_MAX_ATTEMPTS, _redact(exc), wait,
                )
                time.sleep(wait)

        raise RuntimeError(
            f"All {config.RETRY_MAX_ATTEMPTS} attempts failed for {path}"
        ) from last_exc

    def _get_safe(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """GET that never raises — returns {} on any error."""
        try:
            return self._get(path, params)
        except Exception as exc:
            log.warning("_get_safe swallowed error for %s: %s", path, exc)
            return {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_profile(self) -> Dict[str, Any]:
        """Return basic profile fields: followers_count, follows_count, media_count."""
        data = self._get(
            self._user_id,
            params={"fields": "followers_count,follows_count,media_count"},
        )
        return {
            "followers": int(data.get("followers_count", 0)),
            "following": int(data.get("follows_count", 0)),
            "posts": int(data.get("media_count", 0)),
        }

    def get_reach_28d(self) -> int:
        """Return account reach for the last 28 days (lifetime insight).

        Uses period=days_28 which returns a single bucketed value.
        Returns 0 on any error.
        """
        data = self._get_safe(
            f"{self._user_id}/insights",
            params={"metric": "reach", "period": "days_28"},
        )
        try:
            items = data.get("data", [])
            if not items:
                return 0
            values = items[0].get("values", [])
            if not values:
                return 0
            return int(values[-1].get("value") or 0)
        except Exception as exc:
            log.warning("get_reach_28d parse error for user %s: %s", self._user_id, exc)
            return 0

    def get_daily_metrics_today(self) -> Dict[str, int]:
        """Return today's daily account metrics.

        Returns dict with:
          - alcance_dia: reach for the current day
          - views_dia: views today (metric_type=total_value/day)
          - contas_engajadas_dia: accounts engaged today (metric_type=total_value/day)
          - interacoes_dia: total interactions today (metric_type=total_value/day)

        Note: views, accounts_engaged and total_interactions only support
        period=day with metric_type=total_value; period=days_28 returns 400 for
        these metrics.

        All default to 0 on any error (graceful degradation).
        """
        result: Dict[str, int] = {
            "alcance_dia": 0,
            "views_dia": 0,
            "contas_engajadas_dia": 0,
            "interacoes_dia": 0,
        }

        # Daily reach (period=day, no metric_type needed)
        reach_data = self._get_safe(
            f"{self._user_id}/insights",
            params={"metric": "reach", "period": "day"},
        )
        try:
            for item in reach_data.get("data", []):
                if item.get("name") == "reach":
                    values = item.get("values", [])
                    if values:
                        result["alcance_dia"] = int(values[-1].get("value") or 0)
        except Exception as exc:
            log.warning("get_daily_metrics_today reach/day parse error for %s: %s", self._user_id, exc)

        # Daily total_value metrics (period=day + metric_type=total_value).
        # views, accounts_engaged and total_interactions do NOT support
        # period=days_28; period=day gives today's aggregate via total_value.value.
        tv_data = self._get_safe(
            f"{self._user_id}/insights",
            params={
                "metric": "views,accounts_engaged,total_interactions",
                "period": "day",
                "metric_type": "total_value",
            },
        )
        _MAP = {
            "views": "views_dia",
            "accounts_engaged": "contas_engajadas_dia",
            "total_interactions": "interacoes_dia",
        }
        try:
            for item in tv_data.get("data", []):
                key = _MAP.get(item.get("name"))
                if key:
                    result[key] = _parse_insight_value(item)
        except Exception as exc:
            log.warning("get_daily_metrics_today tv parse error for %s: %s", self._user_id, exc)

        return result

    def get_daily_metrics_for_day(self, day: date) -> Dict[str, Optional[int]]:
        """Return daily account metrics for a SPECIFIC past day (BRT).

        Used by the rolling reconciliation window (D-1, D-2) and the backfill.
        Queries a single-day window [day, day+1) so each metric's total_value /
        series point maps unambiguously to `day`.

        Values that are missing or negative (API garbage before a metric's
        availability date) become None → the caller writes an EMPTY cell, never
        a fake 0. All keys default to None on error (graceful degradation).
        """
        since = day.isoformat()
        until = (day + timedelta(days=1)).isoformat()
        result: Dict[str, Optional[int]] = {
            "alcance_dia": None,
            "views_dia": None,
            "contas_engajadas_dia": None,
            "interacoes_dia": None,
        }

        # Daily reach as a single-day series window
        reach_data = self._get_safe(
            f"{self._user_id}/insights",
            params={"metric": "reach", "period": "day", "since": since, "until": until},
        )
        try:
            for item in reach_data.get("data", []):
                if item.get("name") == "reach":
                    values = item.get("values", [])
                    if values:
                        result["alcance_dia"] = _sanitize(values[-1].get("value"))
        except Exception as exc:
            log.warning("get_daily_metrics_for_day reach parse error %s %s: %s", self._user_id, since, exc)

        # total_value metrics over the single-day window
        tv_data = self._get_safe(
            f"{self._user_id}/insights",
            params={
                "metric": "views,accounts_engaged,total_interactions",
                "period": "day",
                "metric_type": "total_value",
                "since": since,
                "until": until,
            },
        )
        _MAP = {
            "views": "views_dia",
            "accounts_engaged": "contas_engajadas_dia",
            "total_interactions": "interacoes_dia",
        }
        try:
            for item in tv_data.get("data", []):
                key = _MAP.get(item.get("name"))
                if key:
                    tv = item.get("total_value") or {}
                    raw = tv.get("value") if isinstance(tv, dict) else tv
                    result[key] = _sanitize(raw)
        except Exception as exc:
            log.warning("get_daily_metrics_for_day tv parse error %s %s: %s", self._user_id, since, exc)

        return result

    def get_media_list(self, max_posts: int = 100) -> List[Dict[str, Any]]:
        """Return up to max_posts media items with basic fields, newest first."""
        fields = (
            "id,timestamp,media_type,caption,permalink,"
            "thumbnail_url,media_url,like_count,comments_count"
        )
        collected: List[Dict[str, Any]] = []
        params: Dict[str, Any] = {
            "fields": fields,
            "limit": min(max_posts, 100),
        }
        path = f"{self._user_id}/media"

        while len(collected) < max_posts:
            data = self._get(path, params)
            items = data.get("data", [])
            collected.extend(items)

            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url or len(collected) >= max_posts:
                break

            cursors = paging.get("cursors", {})
            after = cursors.get("after")
            if after:
                params["after"] = after
            else:
                break

        return collected[:max_posts]

    def iter_media(self, max_items: int = 600):
        """Itera as mídias da conta, mais recentes primeiro, paginando sob demanda.

        get_media_list baixa tudo antes de devolver. Aqui o consumidor pode
        parar no meio — usado pra achar um permalink específico sem varrer o
        perfil inteiro.
        """
        params: Dict[str, Any] = {"fields": "id,permalink", "limit": 100}
        path = f"{self._user_id}/media"
        seen = 0
        while seen < max_items:
            data = self._get_safe(path, params)
            items = data.get("data", [])
            if not items:
                return
            for it in items:
                yield it
                seen += 1
                if seen >= max_items:
                    return
            after = (data.get("paging", {}).get("cursors", {}) or {}).get("after")
            if not after or not data.get("paging", {}).get("next"):
                return
            params["after"] = after

    def get_media_meta(self, media_id: str) -> Dict[str, Any]:
        """Metadados de uma mídia por ID. {} se a mídia não é desta conta.

        Uma tentativa só, sem retry: um media_id vindo do Meta Ads pode ser
        da outra conta do grupo, e aí o 400 é a RESPOSTA esperada, não uma
        falha transitória. Com o backoff normal (5+10+20+40s) cada post de
        outra conta custaria 75s e a coleta não terminaria.
        """
        url = f"{config.IG_BASE_URL}/{media_id}"
        try:
            resp = self._session.get(url, params={
                "access_token": self._token,
                # `username` é o DONO real do post. Os dois tokens enxergam as
                # mídias das duas contas (mesmo grupo), então "de quem respondeu
                # primeiro" não serve: marcava metta em 27 posts, dos quais 19
                # são do tiago. E o insight da tela só abre pro dono.
                "fields": ("permalink,caption,media_type,media_product_type,"
                           "timestamp,username"),
            }, timeout=30)
            if not resp.ok:
                return {}
            data = resp.json()
            return {} if "error" in data else data
        except Exception as exc:
            log.warning("get_media_meta %s: %s", media_id, _redact(exc))
            return {}

    def get_boost_insights(self, media_id: str, media_product_type: str) -> Dict[str, Any]:
        """Visitas ao perfil e seguidores de um post, mais alcance e views.

        profile_visits/follows só existem para FEED. Para REELS a API responde
        "(#100) does not support ... for this media product type", então nem
        são pedidos: pedir junto derrubaria a chamada inteira e a gente
        perderia alcance e views também.

        Ausência é gravada como string vazia, não zero — a planilha precisa
        distinguir "a API não fornece" de "rendeu zero seguidor".
        """
        tem_perfil = media_product_type == "FEED"
        metrics = "views,reach" + (",profile_visits,follows" if tem_perfil else "")

        data = self._get_safe(f"{media_id}/insights", params={"metric": metrics})
        vals = {}
        for item in data.get("data", []):
            try:
                vals[item["name"]] = int(item["values"][0]["value"] or 0)
            except Exception:
                continue

        return {
            "views": vals.get("views", ""),
            "reach": vals.get("reach", ""),
            "profile_visits": vals.get("profile_visits", "") if tem_perfil else "",
            "follows": vals.get("follows", "") if tem_perfil else "",
        }

    def get_post_insights(
        self, media_id: str, media_type: str
    ) -> Dict[str, Any]:
        """Return insights dict for a single post.

        Never raises — logs warning and returns zeros on any error.
        Keys: views, reach, saved, shares, likes, comments, reposts, skip_rate
        """
        metrics = _COMMON_INSIGHT_METRICS
        if media_type == "VIDEO":
            metrics += _VIDEO_EXTRA_METRICS
        else:
            metrics += _FEED_EXTRA_METRICS

        data = self._get_safe(
            f"{media_id}/insights",
            params={"metric": metrics},
        )

        result: Dict[str, Any] = {
            "views": 0,
            "reach": 0,
            "saved": 0,
            "shares": 0,
            "likes": 0,
            "comments": 0,
            "reposts": 0,
            "skip_rate": 0.0,
            "profile_visits": 0,
            "follows": 0,
        }

        if not data:
            return result

        try:
            for item in data.get("data", []):
                name = item.get("name", "")
                values = item.get("values", [])
                val = values[0].get("value", 0) if values else item.get("value", 0)

                if name == "views":
                    result["views"] = int(val or 0)
                elif name == "reach":
                    result["reach"] = int(val or 0)
                elif name == "saved":
                    result["saved"] = int(val or 0)
                elif name == "shares":
                    result["shares"] = int(val or 0)
                elif name == "likes":
                    result["likes"] = int(val or 0)
                elif name == "comments":
                    result["comments"] = int(val or 0)
                elif name == "reposts":
                    result["reposts"] = int(val or 0)
                elif name == "reels_skip_rate":
                    f = float(val or 0)
                    result["skip_rate"] = round(f * 100 if f <= 1.0 else f, 2)
                elif name == "profile_visits":
                    result["profile_visits"] = int(val or 0)
                elif name == "follows":
                    result["follows"] = int(val or 0)
        except Exception as exc:
            log.warning("get_post_insights parse error for %s: %s", media_id, exc)

        return result

    def get_follower_demographics(self) -> List[Dict[str, Any]]:
        """Return follower demographics rows: age×gender, top cities, top countries.

        Uses the lifetime `follower_demographics` metric with metric_type=total_value
        and a per-call breakdown. Requires ≥100 followers (both accounts qualify).

        Each returned dict: {dimensao, chave, seguidores}. `chave` for the
        age×gender cross is "<age>|<gender>" (e.g. "25-34|F"). Never raises —
        a breakdown that errors just contributes nothing (graceful degradation).
        """
        out: List[Dict[str, Any]] = []
        # (api breakdown value, our dimensao label)
        plan = [
            ("age,gender", "idade_genero"),
            ("city", "cidade"),
            ("country", "pais"),
        ]
        for breakdown, dimensao in plan:
            data = self._get_safe(
                f"{self._user_id}/insights",
                params={
                    "metric": "follower_demographics",
                    "period": "lifetime",
                    "metric_type": "total_value",
                    "breakdown": breakdown,
                },
            )
            try:
                items = data.get("data", [])
                if not items:
                    log.warning(
                        "get_follower_demographics: empty for breakdown=%s user=%s",
                        breakdown, self._user_id,
                    )
                    continue
                tv = items[0].get("total_value") or {}
                for bd in tv.get("breakdowns", []):
                    for res in bd.get("results", []):
                        dims = res.get("dimension_values", [])
                        chave = "|".join(str(d) for d in dims)
                        value = res.get("value")
                        if chave == "" or value is None:
                            continue
                        out.append({
                            "dimensao": dimensao,
                            "chave": chave,
                            "seguidores": int(value),
                        })
            except Exception as exc:
                log.warning(
                    "get_follower_demographics parse error breakdown=%s user=%s: %s",
                    breakdown, self._user_id, exc,
                )
        return out

    def get_stories(self) -> List[Dict[str, Any]]:
        """Return currently-active stories (~last 24h) with per-story insights.

        The /stories edge only returns stories still LIVE; expired stories are
        gone from the API (there is no history/backfill). The caller upserts by
        story id into an append-only sheet, so the history accrues from the
        first collection forward. The hourly sync catches each story ~24x.

        Never raises — [] on error; a per-story insight failure → zeros.
        """
        data = self._get_safe(
            f"{self._user_id}/stories",
            params={
                "fields": "id,media_type,timestamp,permalink,thumbnail_url,media_url"
            },
        )
        items = data.get("data", []) if data else []
        out: List[Dict[str, Any]] = []
        for st in items:
            story_id = st.get("id", "")
            if not story_id:
                continue
            insights = self._get_story_insights(story_id)
            out.append({
                "story_id": story_id,
                "media_type": st.get("media_type", ""),
                "timestamp": st.get("timestamp", ""),
                "permalink": st.get("permalink", ""),
                "thumbnail_url": st.get("thumbnail_url") or st.get("media_url") or "",
                **insights,
            })
        return out

    def _get_story_insights(self, story_id: str) -> Dict[str, int]:
        """Per-story insights. All metrics default to 0 on any error."""
        keys = (
            "reach", "replies", "shares", "total_interactions",
            "follows", "profile_visits", "navigation", "views",
        )
        result: Dict[str, int] = {k: 0 for k in keys}
        data = self._get_safe(
            f"{story_id}/insights",
            params={"metric": ",".join(keys)},
        )
        if not data:
            return result
        try:
            for item in data.get("data", []):
                name = item.get("name", "")
                values = item.get("values", [])
                val = values[0].get("value", 0) if values else item.get("value", 0)
                if name in result:
                    result[name] = int(val or 0)
        except Exception as exc:
            log.warning("_get_story_insights parse error for %s: %s", story_id, exc)
        return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _sanitize(raw: Any) -> Optional[int]:
    """Coerce a raw API value to int, or None if missing/negative.

    A metric that does not exist yet for a given day comes back as 0 or small
    negative garbage (-3, -7). Negatives → None. Genuine 0 is kept (a real
    zero-activity day); the backfill separately strips leading zeros before a
    metric's first positive value.
    """
    if raw is None:
        return None
    try:
        v = int(raw)
    except (ValueError, TypeError):
        return None
    return None if v < 0 else v


def _parse_insight_value(item: Dict[str, Any]) -> int:
    """Parse an insight item handling both values[] and total_value formats."""
    values = item.get("values", [])
    if values:
        return int(values[-1].get("value") or 0)
    tv = item.get("total_value") or {}
    if isinstance(tv, dict):
        return int(tv.get("value") or 0)
    if isinstance(tv, (int, float)):
        return int(tv)
    return 0


def parse_media_datetime(timestamp: str) -> datetime:
    """Parse ISO 8601 timestamp from Graph API to a BRT datetime."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.astimezone(BRT)
    except Exception:
        return datetime.now(BRT)


def parse_media_date(timestamp: str) -> date:
    """Parse ISO 8601 timestamp from Graph API to a Python date (BRT)."""
    return parse_media_datetime(timestamp).date()
