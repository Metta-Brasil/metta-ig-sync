"""One-shot backfill of historical daily account metrics into ig_*_perfil.

Recovers history the hourly sync cannot reach on its own:
  - Reach daily series from 2025-01-01 (API keeps ~2 years), fetched in ~28-day
    windows and mapped by end_time date.
  - Views / total_interactions / accounts_engaged from 2025-08-01 via one
    total_value window per day (single-day windows map unambiguously to the day,
    matching the hourly reconciliation path).

Both stop at D-2 (the rolling reconciliation window owns D-1 and D; today is
written by the hourly full upsert). Cells with no data yet are written EMPTY:
negatives and leading zeros before a metric's first positive value are dropped,
so views/interactions stay blank before ~aug/2025 and accounts_engaged before
its first active day.

Merge is non-destructive: existing rows keep their A:E snapshot and any non-empty
F:I value; the backfill only fills blanks/zeros. The whole tab is then rewritten
deduped + sorted by date. Idempotent — safe to re-run.

Usage:
    INSTAGRAM_ACCESS_TOKEN=... GOOGLE_SERVICE_ACCOUNT_JSON=... \
        python -m scripts.backfill_profile [--account metta|tiago] [--dry-run]
"""

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from src import config
from src.instagram import BRT, IGClient, _sanitize
from src.sheets import (
    _apply_column_formats,
    _build_service,
    _col_letter,
    _to_sheet_serial,
    _to_sheet_value,
    ensure_headers,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("backfill")

SLEEP_BETWEEN_CALLS = 0.3
REACH_WINDOW_DAYS = 28

# Daily column keys in F,G,H,I order (matches config.PROFILE_DAILY_KEYS).
DAILY_KEYS = config.PROFILE_DAILY_KEYS  # [alcance_dia, contas_engajadas_dia, interacoes_dia, views_dia]
# Parity-checkable (additive) metrics. reach / accounts_engaged are de-duplicated
# unique counts → NOT additive, so excluded from the sum-vs-window check.
PARITY_METRICS = {
    "views_dia": "views",
    "interacoes_dia": "total_interactions",
}
TV_API_NAME = {
    "views": "views_dia",
    "accounts_engaged": "contas_engajadas_dia",
    "total_interactions": "interacoes_dia",
}


def _daterange(start: date, end_inclusive: date):
    d = start
    while d <= end_inclusive:
        yield d
        d += timedelta(days=1)


def fetch_reach_history(client: IGClient, start: date, end_inclusive: date) -> Dict[date, Optional[int]]:
    """Fetch daily reach via ~28-day windows. Maps each point by end_time date."""
    out: Dict[date, Optional[int]] = {}
    win_start = start
    while win_start <= end_inclusive:
        # until is EXCLUSIVE; cap so the last point is end_inclusive.
        win_until = min(win_start + timedelta(days=REACH_WINDOW_DAYS), end_inclusive + timedelta(days=1))
        data = client._get_safe(
            f"{client._user_id}/insights",
            params={
                "metric": "reach",
                "period": "day",
                "since": win_start.isoformat(),
                "until": win_until.isoformat(),
            },
        )
        for item in data.get("data", []):
            if item.get("name") != "reach":
                continue
            for v in item.get("values", []):
                # end_time is "YYYY-MM-DDT..."; take the date part directly.
                # (datetime.fromisoformat on 3.9 can't parse the "+0000" offset.)
                et = v.get("end_time", "")
                try:
                    day = date.fromisoformat(et[:10])
                except (ValueError, TypeError):
                    continue
                if start <= day <= end_inclusive:
                    out[day] = _sanitize(v.get("value"))
        time.sleep(SLEEP_BETWEEN_CALLS)
        win_start = win_until  # next window starts where this one ended (exclusive)
    return out


def fetch_tv_day(client: IGClient, day: date) -> Dict[str, Optional[int]]:
    """One total_value window for a single day → {views_dia, contas_engajadas_dia, interacoes_dia}."""
    since = day.isoformat()
    until = (day + timedelta(days=1)).isoformat()
    out: Dict[str, Optional[int]] = {k: None for k in TV_API_NAME.values()}
    data = client._get_safe(
        f"{client._user_id}/insights",
        params={
            "metric": "views,accounts_engaged,total_interactions",
            "period": "day",
            "metric_type": "total_value",
            "since": since,
            "until": until,
        },
    )
    for item in data.get("data", []):
        key = TV_API_NAME.get(item.get("name"))
        if not key:
            continue
        tv = item.get("total_value") or {}
        raw = tv.get("value") if isinstance(tv, dict) else tv
        out[key] = _sanitize(raw)
    return out


def strip_leading_zeros(by_day: Dict[date, Dict[str, Optional[int]]], metric_key: str) -> int:
    """Set a metric to None for all days before its first strictly-positive value.

    Kills the pre-availability 0 garbage (e.g. accounts_engaged before ~nov/2025)
    while keeping genuine zero-activity days that occur after the metric exists.
    Returns how many leading days were blanked.
    """
    days_sorted = sorted(by_day.keys())
    first_pos: Optional[date] = None
    for d in days_sorted:
        v = by_day[d].get(metric_key)
        if v is not None and v > 0:
            first_pos = d
            break
    if first_pos is None:
        # metric never positive in range — blank it entirely
        for d in days_sorted:
            by_day[d][metric_key] = None
        return len(days_sorted)
    blanked = 0
    for d in days_sorted:
        if d < first_pos and by_day[d].get(metric_key) is not None:
            by_day[d][metric_key] = None
            blanked += 1
    return blanked


def build_backfill(client: IGClient, last_day: date) -> Dict[date, Dict[str, Optional[int]]]:
    """Return {day: {alcance_dia, contas_engajadas_dia, interacoes_dia, views_dia}}."""
    log.info("Fetching reach history %s → %s ...", config.BACKFILL_REACH_START, last_day)
    reach = fetch_reach_history(client, config.BACKFILL_REACH_START, last_day)
    log.info("  reach: %d days", len(reach))

    by_day: Dict[date, Dict[str, Optional[int]]] = {}
    for d, r in reach.items():
        by_day.setdefault(d, {k: None for k in DAILY_KEYS})["alcance_dia"] = r

    tv_days = list(_daterange(config.BACKFILL_TV_START, last_day))
    log.info("Fetching total_value (views/interações/contas) for %d days ...", len(tv_days))
    for i, d in enumerate(tv_days, 1):
        tv = fetch_tv_day(client, d)
        slot = by_day.setdefault(d, {k: None for k in DAILY_KEYS})
        for k, v in tv.items():
            slot[k] = v
        if i % 50 == 0 or i == len(tv_days):
            log.info("  total_value: %d/%d", i, len(tv_days))
        time.sleep(SLEEP_BETWEEN_CALLS)

    # Drop leading-zero garbage per metric.
    for mk in DAILY_KEYS:
        n = strip_leading_zeros(by_day, mk)
        if n:
            log.info("  stripped %d leading empty/zero days for %s", n, mk)
    return by_day


def parity_check(client: IGClient, by_day: Dict[date, Dict[str, Optional[int]]]) -> None:
    """Compare a closed month's window total_value vs the sum of daily cells."""
    month_start, month_end = date(2025, 9, 1), date(2025, 9, 30)
    until = (month_end + timedelta(days=1)).isoformat()
    data = client._get_safe(
        f"{client._user_id}/insights",
        params={
            "metric": "views,accounts_engaged,total_interactions",
            "period": "day",
            "metric_type": "total_value",
            "since": month_start.isoformat(),
            "until": until,
        },
    )
    window: Dict[str, int] = {}
    for item in data.get("data", []):
        key = TV_API_NAME.get(item.get("name"))
        if key:
            tv = item.get("total_value") or {}
            window[key] = int((tv.get("value") if isinstance(tv, dict) else tv) or 0)

    log.info("--- PARIDADE set/2025 (janela única vs soma diária) ---")
    for metric_key in PARITY_METRICS:
        daily_sum = sum(
            (by_day.get(d, {}).get(metric_key) or 0)
            for d in _daterange(month_start, month_end)
        )
        win = window.get(metric_key, 0)
        diff = daily_sum - win
        pct = (abs(diff) / win * 100) if win else (0.0 if daily_sum == 0 else 100.0)
        verdict = "PASS" if pct <= 1.0 else "CHECK"
        log.info("  %-18s janela=%d soma=%d diff=%d (%.2f%%) [%s]",
                 metric_key, win, daily_sum, diff, pct, verdict)


def _read_existing(svc, sheet_name: str, ncols: int) -> Dict[int, List[Any]]:
    """Read tab → {date_serial: padded 9-col row}, last-match-wins."""
    end_col = _col_letter(ncols - 1)
    resp = svc.spreadsheets().values().get(
        spreadsheetId=config.SPREADSHEET_ID,
        range=f"{sheet_name}!A:{end_col}",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()
    rows = resp.get("values", [])
    out: Dict[int, List[Any]] = {}
    for i, row in enumerate(rows):
        if i == 0:
            continue  # header
        if not row:
            continue
        try:
            serial = int(float(row[0]))
        except (ValueError, TypeError):
            continue
        padded = list(row) + [""] * (ncols - len(row))
        out[serial] = padded[:ncols]
    return out


def _cell_present(cell: Any) -> bool:
    """True if a cell holds real data (non-empty AND not a literal 0)."""
    if cell == "" or cell is None:
        return False
    if isinstance(cell, (int, float)) and cell == 0:
        return False  # 0 written by pre-fix code = treat as no-data
    return True


def write_account(svc, account: Dict[str, Any], by_day: Dict[date, Dict[str, Optional[int]]]) -> None:
    sheet_name = account["sheet_profile"]
    columns = config.PROFILE_COLUMNS
    headers = [c["header"] for c in columns]
    ncols = len(columns)
    first_daily_idx = ncols - len(DAILY_KEYS)  # F = index 5

    sheet_id = ensure_headers(
        svc, config.SPREADSHEET_ID, sheet_name, headers,
        renames=config.PROFILE_HEADER_RENAMES,
    )

    existing = _read_existing(svc, sheet_name, ncols)
    log.info("[%s] existing rows: %d", sheet_name, len(existing))

    backfill_serials = {_to_sheet_serial(d): d for d in by_day}
    all_serials = sorted(set(existing.keys()) | set(backfill_serials.keys()))

    out_rows: List[List[Any]] = []
    filled_cells = 0
    for serial in all_serials:
        row = existing.get(serial, [serial] + [""] * (ncols - 1))[:]
        row = list(row) + [""] * (ncols - len(row))
        row[0] = serial
        day = backfill_serials.get(serial)
        if day is not None:
            metrics = by_day[day]
            for j, key in enumerate(DAILY_KEYS):
                col = first_daily_idx + j
                if _cell_present(row[col]):
                    continue  # keep real existing value
                val = metrics.get(key)
                new_cell = _to_sheet_value(key, val)  # None → ""
                if new_cell != "" :
                    filled_cells += 1
                row[col] = new_cell
        out_rows.append(row[:ncols])

    log.info("[%s] writing %d rows (%d daily cells filled by backfill)", sheet_name, len(out_rows), filled_cells)

    if config.DRY_RUN:
        log.info("[DRY_RUN] [%s] would clear A:%s and write header + %d rows.",
                 sheet_name, _col_letter(ncols - 1), len(out_rows))
        _preview(headers, out_rows)
        return

    end_col = _col_letter(ncols - 1)
    svc.spreadsheets().values().clear(
        spreadsheetId=config.SPREADSHEET_ID,
        range=f"{sheet_name}!A1:{end_col}",
        body={},
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=config.SPREADSHEET_ID,
        range=f"{sheet_name}!A1:{end_col}{len(out_rows) + 1}",
        valueInputOption="USER_ENTERED",
        body={"values": [headers] + out_rows},
    ).execute()
    _apply_column_formats(
        svc, config.SPREADSHEET_ID, sheet_id,
        columns=columns, start_row_zero=1, num_data_rows=len(out_rows),
    )
    log.info("[%s] done — %d rows written.", sheet_name, len(out_rows))


def _preview(headers: List[str], rows: List[List[Any]]) -> None:
    log.info("  header: %s", headers)
    for r in rows[:3]:
        log.info("  first : %s", r)
    for r in rows[-3:]:
        log.info("  last  : %s", r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", choices=["metta", "tiago"], help="default: both")
    ap.add_argument("--dry-run", action="store_true", help="no writes; also honors DRY_RUN env")
    args = ap.parse_args()

    if args.dry_run:
        config.DRY_RUN = True
    if config.DRY_RUN:
        log.info("DRY_RUN — no writes to Google Sheets.")

    token = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
    if not token:
        log.error("INSTAGRAM_ACCESS_TOKEN env var is required.")
        return 1

    svc = _build_service()
    today = datetime.now(BRT).date()
    # Cover through D-1: today's full row is owned by the hourly upsert; D-1/D-2
    # are also refined hourly by the reconciliation window, so seeding them here
    # leaves no gap between backfill history and the live sync.
    last_day = today - timedelta(days=1)
    log.info("today(BRT)=%s  backfill last_day=%s", today, last_day)

    accounts = [a for a in config.ACCOUNTS if not args.account or a["name"] == args.account]
    for account in accounts:
        log.info("==================== %s ====================", account["name"])
        client = IGClient(token=token, user_id=account["user_id"])
        by_day = build_backfill(client, last_day)
        parity_check(client, by_day)
        write_account(svc, account, by_day)

    log.info("Backfill complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
