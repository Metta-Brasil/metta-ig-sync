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
from datetime import date

from . import config
from .instagram import IGClient, parse_media_date
from .sheets import _build_service, posts_overwrite, profile_append

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

    caption_raw = media.get("caption") or ""
    caption = caption_raw[:500]

    # Thumbnail: for images use media_url, for carousels/videos use thumbnail_url
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
    }


def sync_account(svc, account: dict, token: str) -> bool:
    """Sync one Instagram account. Returns True on success, False on failure."""
    name = account["name"]
    user_id = account["user_id"]
    sheet_profile = account["sheet_profile"]
    sheet_posts = account["sheet_posts"]

    log.info("=== Starting sync for account: %s (user_id=%s) ===", name, user_id)

    client = IGClient(token=token, user_id=user_id)

    # --- Profile snapshot ---
    try:
        profile = client.get_profile()
        reach_28d = client.get_reach_28d()
        profile_row = {
            "date": date.today(),
            "followers": profile["followers"],
            "following": profile["following"],
            "posts": profile["posts"],
            "reach_28d": reach_28d,
        }
        log.info(
            "[%s] Profile: followers=%d, following=%d, posts=%d, reach_28d=%d",
            name, profile["followers"], profile["following"],
            profile["posts"], reach_28d,
        )
        profile_append(svc, sheet_profile, profile_row)
    except Exception as exc:
        log.error("[%s] Profile sync failed: %s", name, exc, exc_info=True)
        return False

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
