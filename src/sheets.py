"""Google Sheets writer for metta-ig-sync.

Handles:
- Service account auth (raw JSON or base64)
- Sheet auto-creation if missing (addSheet via batchUpdate)
- profile_append: append one row per day, deduplicated by date serial
- posts_overwrite: clear + rewrite complete dataset
"""

import base64
import json
import logging
import os
from datetime import date as _date
from typing import Any, Dict, List, Optional, Tuple

from google.oauth2 import service_account
from googleapiclient.discovery import build

from . import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Google Sheets / Lotus 1-2-3 epoch
SHEETS_EPOCH = _date(1899, 12, 30)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _col_letter(idx_zero: int) -> str:
    """Convert 0-based column index to spreadsheet letter notation."""
    n = idx_zero
    out = ""
    while True:
        out = chr(ord("A") + (n % 26)) + out
        n = n // 26 - 1
        if n < 0:
            break
    return out


def _to_sheet_serial(d: _date) -> int:
    """Convert a Python date to a Lotus epoch serial (integer)."""
    return (d - SHEETS_EPOCH).days


def _to_sheet_value(key: str, v: Any) -> Any:
    """Coerce a value to what Sheets expects for its column type."""
    if v is None:
        return ""
    if key == "date" and isinstance(v, _date):
        return _to_sheet_serial(v)
    return v


def _row_values(row: Dict[str, Any], columns: List[Dict[str, str]]) -> List[Any]:
    return [_to_sheet_value(c["key"], row.get(c["key"])) for c in columns]


def _parse_sa_json(raw: str) -> Dict[str, Any]:
    """Parse service account JSON, accepting raw JSON or base64-encoded JSON."""
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON env var is required")

    s = raw.strip().lstrip("﻿")  # strip whitespace and BOM

    if not s.startswith("{"):
        try:
            s = base64.b64decode(s, validate=False).decode("utf-8").strip()
        except Exception:
            pass

    try:
        return json.loads(s)
    except json.JSONDecodeError as exc:
        head = s[:40].replace("\n", "\\n")
        tail = s[-40:].replace("\n", "\\n")
        raise RuntimeError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON (parser said: {exc}). "
            f"Length={len(s)} bytes, starts with {head!r}, ends with {tail!r}."
        ) from exc


# ---------------------------------------------------------------------------
# Service and sheet management
# ---------------------------------------------------------------------------

def _build_service():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    info = _parse_sa_json(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _get_or_create_sheet(
    svc,
    spreadsheet_id: str,
    name: str,
    headers: List[str],
) -> Tuple[int, bool]:
    """Return (sheet_id, existed_already).

    If the sheet does not exist, create it via addSheet and write the header row.
    """
    meta = svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties",
    ).execute()

    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        if props.get("title") == name:
            return int(props["sheetId"]), True

    # Sheet does not exist — create it
    log.info("Sheet '%s' not found, creating it.", name)
    resp = svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": name}}}]},
    ).execute()
    new_props = resp["replies"][0]["addSheet"]["properties"]
    sheet_id = int(new_props["sheetId"])

    # Write header row
    rng = f"{name}!A1:{_col_letter(len(headers) - 1)}1"
    svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=rng,
        valueInputOption="USER_ENTERED",
        body={"values": [headers]},
    ).execute()
    log.info("Created sheet '%s' with %d header columns.", name, len(headers))
    return sheet_id, False


def _apply_column_formats(
    svc,
    spreadsheet_id: str,
    sheet_id: int,
    columns: List[Dict[str, str]],
    start_row_zero: int,
    num_data_rows: int,
) -> None:
    if num_data_rows <= 0:
        return

    def _fmt(col_format: str) -> Dict[str, Any]:
        if col_format == "@":
            return {"type": "TEXT"}
        if col_format == "dd/MM/yyyy":
            return {"type": "DATE", "pattern": "dd/MM/yyyy"}
        if col_format == "0":
            return {"type": "NUMBER", "pattern": "0"}
        if col_format == "0.00":
            return {"type": "NUMBER", "pattern": "0.00"}
        return {"type": "NUMBER"}

    requests_body = []
    end_row = start_row_zero + num_data_rows
    for idx, col in enumerate(columns):
        requests_body.append({
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": start_row_zero,
                    "endRowIndex": end_row,
                    "startColumnIndex": idx,
                    "endColumnIndex": idx + 1,
                },
                "cell": {
                    "userEnteredFormat": {
                        "numberFormat": _fmt(col["format"])
                    }
                },
                "fields": "userEnteredFormat.numberFormat",
            }
        })
    svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests_body},
    ).execute()


# ---------------------------------------------------------------------------
# Public write functions
# ---------------------------------------------------------------------------

def profile_append(
    svc,
    sheet_name: str,
    row_dict: Dict[str, Any],
) -> None:
    """Append a profile snapshot row if no row with the same date serial already exists.

    The date is stored as a Lotus epoch serial (integer) in column A.
    Deduplication: reads existing col A values and skips if serial already present.
    """
    columns = config.PROFILE_COLUMNS
    headers = [c["header"] for c in columns]
    sheet_id, existed = _get_or_create_sheet(svc, config.SPREADSHEET_ID, sheet_name, headers)

    date_serial = _to_sheet_serial(row_dict["date"]) if isinstance(row_dict.get("date"), _date) else row_dict.get("date")

    # Read existing date column (col A)
    rng_dates = f"{sheet_name}!A:A"
    existing = svc.spreadsheets().values().get(
        spreadsheetId=config.SPREADSHEET_ID,
        range=rng_dates,
    ).execute()
    existing_values = existing.get("values", [])

    for cell_row in existing_values:
        if cell_row and str(cell_row[0]) == str(date_serial):
            log.info(
                "profile_append: skipping %s/%s — date serial %s already present.",
                sheet_name, row_dict.get("date"), date_serial,
            )
            return

    # Find next empty row
    next_row = len(existing_values) + 1

    row_vals = _row_values(row_dict, columns)
    end_col = _col_letter(len(columns) - 1)
    rng = f"{sheet_name}!A{next_row}:{end_col}{next_row}"

    if config.DRY_RUN:
        log.info("[DRY_RUN] profile_append: would write row %d to %s: %s", next_row, sheet_name, row_vals)
        return

    svc.spreadsheets().values().update(
        spreadsheetId=config.SPREADSHEET_ID,
        range=rng,
        valueInputOption="USER_ENTERED",
        body={"values": [row_vals]},
    ).execute()

    # Apply formats to the new data row (0-based: next_row - 1)
    _apply_column_formats(
        svc, config.SPREADSHEET_ID, sheet_id,
        columns=columns,
        start_row_zero=next_row - 1,
        num_data_rows=1,
    )
    log.info("profile_append: wrote row %d to %s (date=%s).", next_row, sheet_name, row_dict.get("date"))


def posts_overwrite(
    svc,
    sheet_name: str,
    rows: List[Dict[str, Any]],
) -> None:
    """Overwrite all post rows: clear data area then rewrite header + all rows."""
    columns = config.POSTS_COLUMNS
    headers = [c["header"] for c in columns]
    sheet_id, _ = _get_or_create_sheet(svc, config.SPREADSHEET_ID, sheet_name, headers)
    num_cols = len(columns)
    end_col = _col_letter(num_cols - 1)

    if config.DRY_RUN:
        log.info("[DRY_RUN] posts_overwrite: would write %d rows to %s.", len(rows), sheet_name)
        return

    # Clear everything from row 1 down
    svc.spreadsheets().values().clear(
        spreadsheetId=config.SPREADSHEET_ID,
        range=f"{sheet_name}!A1:{end_col}",
        body={},
    ).execute()

    if not rows:
        log.warning("posts_overwrite: no rows to write for %s.", sheet_name)
        return

    data_values = [_row_values(r, columns) for r in rows]
    body_values = [headers] + data_values
    end_row = len(body_values)

    svc.spreadsheets().values().update(
        spreadsheetId=config.SPREADSHEET_ID,
        range=f"{sheet_name}!A1:{end_col}{end_row}",
        valueInputOption="USER_ENTERED",
        body={"values": body_values},
    ).execute()

    # Apply formats to data rows only (row index 1 onward, 0-based)
    _apply_column_formats(
        svc, config.SPREADSHEET_ID, sheet_id,
        columns=columns,
        start_row_zero=1,
        num_data_rows=len(data_values),
    )
    log.info("posts_overwrite: wrote %d rows to %s.", len(data_values), sheet_name)
