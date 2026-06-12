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
"""

import logging
import os
import sys
from datetime import date, datetime, timedelta

from . import config
from .instagram import BRT, IGClient, parse_media_date, parse_media_datetime
from .sheets import (
    _build_service,
    posts_overwrite,
    profile_upsert,
    profile_upsert_partial,
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

    log.info("=== Done: %s — %d posts synced ===", name, len(post_rows))
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

    any_failed = False
    for account in config.ACCOUNTS:
        success = sync_account(svc, account, token)
        if not success:
            any_failed = True

    if any_failed:
        log.error("One or more accounts failed to sync.")
        return 1

    log.info("All accounts synced successfully.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
