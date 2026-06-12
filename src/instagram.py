"""Instagram Graph API client for metta-ig-sync."""

import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from . import config

log = logging.getLogger(__name__)

# Metrics to request per media type
_COMMON_INSIGHT_METRICS = "views,reach,saved,shares"
_VIDEO_EXTRA_METRICS = ",likes,comments,reposts,reels_skip_rate"


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
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                wait = config.RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                log.warning(
                    "Request error for %s (attempt %d/%d): %s, retrying in %ds",
                    path, attempt, config.RETRY_MAX_ATTEMPTS, exc, wait,
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
            # Take the last (most recent) value
            return int(values[-1].get("value", 0))
        except Exception as exc:
            log.warning("get_reach_28d parse error for user %s: %s", self._user_id, exc)
            return 0

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

            # Pagination
            paging = data.get("paging", {})
            next_url = paging.get("next")
            if not next_url or len(collected) >= max_posts:
                break

            # Extract cursor from next URL for next page call
            cursors = paging.get("cursors", {})
            after = cursors.get("after")
            if after:
                params["after"] = after
            else:
                break

        return collected[:max_posts]

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
                    # API may return as fraction (0.35) or percentage (35.0)
                    f = float(val or 0)
                    result["skip_rate"] = round(f * 100 if f <= 1.0 else f, 2)
        except Exception as exc:
            log.warning("get_post_insights parse error for %s: %s", media_id, exc)

        return result


def parse_media_date(timestamp: str) -> date:
    """Parse ISO 8601 timestamp from Graph API to a Python date."""
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).date()
    except Exception:
        return date.today()
