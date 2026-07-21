"""Resume Bot Metrics -- nightly aggregator (long-term view).

Reads the Candidates tab from the main sheet and writes a nicely
formatted "Metrics" tab back into the SAME sheet with these columns:

    A          B           C..N            O        P
    Metric     This Week   12 months*      YTD      All-Time

* C is the current month, D is last month, ... N is 12 months ago.
  Rolling window advances automatically as time passes.

Sections:
  - HR OUTCOMES (Lisa's headline numbers)
  - BOT DECISIONS (what the bot did with each resume)
  - BOT EFFICIENCY (time / dollars saved)
  - CURRENT PIPELINE (point-in-time HR queue state)

The whole tab is bot-managed: labels, values, AND formatting are
rewritten on every nightly run so manual damage self-heals.

Runs as a standalone GitHub Actions job at 5pm PT each night.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)


# ----------------------------- Configuration --------------------------------

STATUS_INTERVIEW_SCHEDULED = "Interview Scheduled"
STATUS_HIRED = "Hired"
STATUS_REJECTED = "Rejected"
STATUS_NO_SHOW = "No Show"

ACTIVE_HR_STATUSES = {
    "In Review", "Contacted", "Interview Scheduled",
    "Offer Made", "On Hold",
}

DEFAULT_TIME_SAVED_PER_CASE_SEC = 120
try:
    TIME_SAVED_PER_CASE_SEC = int(
        (os.environ.get("TIME_SAVED_PER_CASE_SEC") or "").strip()
        or DEFAULT_TIME_SAVED_PER_CASE_SEC
    )
except ValueError:
    TIME_SAVED_PER_CASE_SEC = DEFAULT_TIME_SAVED_PER_CASE_SEC

DEFAULT_HOURLY_RATE_USD = 20.0

MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

METRICS_ANCHOR_MONTH = os.environ.get("METRICS_ANCHOR_MONTH", "2026-07")
MONTHS_WINDOW = 12


def _required(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ----------------------------- Time helpers ---------------------------------

def parse_timestamp(s: str) -> Optional[datetime]:
    if not s:
        return None
    t = s.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(t)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def month_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def month_label(key: str) -> str:
    year, month = key.split("-")
    return f"{MONTH_NAMES[int(month) - 1]} '{year[-2:]}"


def months_from_anchor(anchor: str, n: int) -> list[str]:
    y, m = (int(x) for x in anchor.split("-"))
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            m = 1
            y += 1
    return out


def in_week(ts: datetime, now: datetime) -> bool:
    return ts >= now - timedelta(days=7)


def in_year(ts: datetime, now: datetime) -> bool:
    return ts.year == now.year


# ----------------------------- Aggregation ----------------------------------

def _new_bucket() -> dict:
    return {
        "resumes_received": 0,
        "sent_to_hr": 0,
        "flagged_for_review": 0,
        "auto_denied": 0,
        "pending_paused": 0,
        "interviews_scheduled": 0,
        "hired": 0,
        "rejected": 0,
        "no_show": 0,
    }


def compute_metrics(rows: list[list[str]], now: datetime,
                    months: list[str]) -> dict:
    week    = _new_bucket()
    ytd     = _new_bucket()
    alltime = _new_bucket()
    by_month = {m: _new_bucket() for m in months}

    active_in_pipeline = 0
    awaiting_triage = 0

    for raw in rows:
        row = (raw + [""] * 20)[:20]
        ts_raw = (row[0] or "").strip()
        decision = (row[9] or "").strip().lower()
        hr_status = (row[17] or "").strip()

        ts = parse_timestamp(ts_raw)
        if ts is None:
            continue

        mk = month_key(ts)
        after_anchor = mk >= METRICS_ANCHOR_MONTH

        buckets = []
        if after_anchor:
            buckets.append(alltime)
            if in_year(ts, now):
                buckets.append(ytd)
        if in_week(ts, now):
            buckets.append(week)
        if mk in by_month:
            buckets.append(by_month[mk])

        for b in buckets:
            b["resumes_received"] += 1
            if decision == "qualified":
                b["sent_to_hr"] += 1
            elif decision == "needs_review":
                b["flagged_for_review"] += 1
            elif decision == "not_qualified":
                b["auto_denied"] += 1
            elif decision == "pending_paused":
                b["pending_paused"] += 1

            if hr_status == STATUS_INTERVIEW_SCHEDULED:
                b["interviews_scheduled"] += 1
            elif hr_status == STATUS_HIRED:
                b["hired"] += 1
            elif hr_status == STATUS_REJECTED:
                b["rejected"] += 1
            elif hr_status == STATUS_NO_SHOW:
                b["no_show"] += 1

        if hr_status in ACTIVE_HR_STATUSES:
            active_in_pipeline += 1
        if not hr_status and decision in {"qualified", "needs_review"}:
            awaiting_triage += 1

    return {
        "week": week, "ytd": ytd, "alltime": alltime,
        "by_month": by_month,
        "active_in_pipeline": active_in_pipeline,
        "awaiting_triage": awaiting_triage,
    }


# ----------------------------- Google API -----------------------------------

def build_credentials() -> Credentials:
    return Credentials(
        token=None,
        refresh_token=_required("GOOGLE_OAUTH_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_required("GOOGLE_OAUTH_CLIENT_ID"),
        client_secret=_required("GOOGLE_OAUTH_CLIENT_SECRET"),
    )


def read_candidates(sheets, sheet_id: str, tab: str) -> list[list[str]]:
    # Hardened 2026-07-20 after a socket-level TimeoutError killed the
    # whole metrics run. Sheets API occasionally hangs the SSL read on
    # large ranges; retry up to 3 times with backoff before giving up.
    # Keeps the scheduled metrics job from failing on transient Google
    # network flakes.
    last_exc = None
    for attempt in range(1, 4):
        try:
            resp = sheets.spreadsheets().values().get(
                spreadsheetId=sheet_id,
                range=f"{tab}!A2:T",
            ).execute()
            if attempt > 1:
                log.info("read_candidates: succeeded on attempt %d", attempt)
            return resp.get("values", [])
        except Exception as e:
            last_exc = e
            log.warning(
                "read_candidates attempt %d/3 failed: %s: %s",
                attempt, type(e).__name__, e,
            )
            if attempt < 3:
                time.sleep(2.0 * attempt)  # 2s, 4s
    log.error("read_candidates: EXHAUSTED retries. Last error: %s", last_exc)
    raise last_exc


def ensure_metrics_tab(sheets, sheet_id: str, tab: str) -> int:
    """Create tab if missing. Return sheetId (numeric gid)."""
    meta = sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sh in meta.get("sheets", []):
        props = sh.get("properties", {})
        if props.get("title") == tab:
            return props["sheetId"]
    log.info("Creating '%s' tab", tab)
    resp = sheets.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {
            "title": tab,
            "gridProperties": {"rowCount": 40, "columnCount": 20},
        }}}]},
    ).execute()
    return resp["replies"][0]["addSheet"]["properties"]["sheetId"]


# ----------------------------- Layout constants -----------------------------
COL_METRIC     = 0
COL_WEEK       = 1
COL_FIRST_MO   = 2
COL_LAST_MO    = COL_FIRST_MO + MONTHS_WINDOW - 1
COL_YTD        = COL_LAST_MO + 1
COL_ALLTIME    = COL_YTD + 1
TOTAL_COLS     = COL_ALLTIME + 1

ROW_TITLE   = 1
ROW_SUBTTL  = 2
ROW_HEADER  = 4

SEC_OUTCOMES_HDR = 5
OUT_RESUMES      = 6
OUT_INTERVIEWS   = 7
OUT_HIRED        = 8
OUT_REJECTED     = 9
OUT_NOSHOW       = 10

SEC_DECISIONS_HDR = 12
DEC_TO_HR        = 13
DEC_FLAGGED      = 14
DEC_DENIED       = 15
DEC_PAUSED       = 16

SEC_EFFICIENCY_HDR = 18
EFF_HOURS = 19
EFF_DOLLARS = 20

SEC_PIPELINE_HDR = 22
PIPE_ACTIVE = 23
PIPE_AWAITING = 24


# ----------------------------- Write + format -------------------------------

def col_letter(i: int) -> str:
    return chr(ord("A") + i)


def write_metrics_tab(sheets, sheet_id: str, sheet_gid: int, tab: str,
                      metrics: dict, months: list[str], now_utc: datetime) -> None:
    pt = now_utc - timedelta(hours=7)
    last_run = (f"Auto-updates nightly at 5pm PT   ·   Last run: "
                f"{pt.strftime('%Y-%m-%d %H:%M PT')}")

    w = metrics["week"]
    y = metrics["ytd"]
    a = metrics["alltime"]
    bm = metrics["by_month"]

    try:
        hourly_rate = float(_optional("HOURLY_RATE_USD",
                                       str(DEFAULT_HOURLY_RATE_USD)))
    except ValueError:
        hourly_rate = DEFAULT_HOURLY_RATE_USD

    def handled(b): return b["sent_to_hr"] + b["auto_denied"] + b["pending_paused"]
    def hrs(b): return round((handled(b) * TIME_SAVED_PER_CASE_SEC) / 3600, 1)
    def dol(b): return f"${hrs(b) * hourly_rate:,.0f}"

    headers = ["Metric", "This Week"] + [month_label(m) for m in months] + ["YTD", "All-Time"]

    def row_values(field):
        return [w[field]] + [bm[m][field] for m in months] + [y[field], a[field]]

    def row_hrs():
        return [hrs(w)] + [hrs(bm[m]) for m in months] + [hrs(y), hrs(a)]

    def row_dol():
        return [dol(w)] + [dol(bm[m]) for m in months] + [dol(y), dol(a)]

    a_labels = [
        ["Resume Bot Metrics"],
        [last_run],
        [""],
        [""],
        ["HR OUTCOMES"],
        ["Resumes received (unique)"],
        ["Scheduled for interviews"],
        ["Hired"],
        ["Rejected"],
        ["No shows"],
        [""],
        ["BOT DECISIONS"],
        ["Sent to HR (qualified)"],
        ["Flagged for review"],
        ["Auto-denied"],
        ["Pending / paused role"],
        [""],
        ["BOT EFFICIENCY"],
        ["HR time saved (hrs)"],
        ["$ saved (@ $%d/hr)" % int(hourly_rate)],
        [""],
        ["CURRENT PIPELINE (point-in-time)"],
        ["Active in HR pipeline"],
        ["Awaiting HR triage"],
    ]

    last_col = col_letter(TOTAL_COLS - 1)
    data_range = lambda row: f"{tab}!B{row}:{last_col}{row}"

    outcomes = {
        OUT_RESUMES:    row_values("resumes_received"),
        OUT_INTERVIEWS: row_values("interviews_scheduled"),
        OUT_HIRED:      row_values("hired"),
        OUT_REJECTED:   row_values("rejected"),
        OUT_NOSHOW:     row_values("no_show"),
    }
    decisions = {
        DEC_TO_HR:    row_values("sent_to_hr"),
        DEC_FLAGGED:  row_values("flagged_for_review"),
        DEC_DENIED:   row_values("auto_denied"),
        DEC_PAUSED:   row_values("pending_paused"),
    }
    efficiency = {
        EFF_HOURS:   row_hrs(),
        EFF_DOLLARS: row_dol(),
    }
    pipe_blank = [""] * (TOTAL_COLS - 2)
    pipeline = {
        PIPE_ACTIVE:   [metrics["active_in_pipeline"]] + pipe_blank,
        PIPE_AWAITING: [metrics["awaiting_triage"]]    + pipe_blank,
    }

    data = [
        {"range": f"{tab}!A1:A24", "values": a_labels},
        {"range": f"{tab}!A4:{last_col}4", "values": [headers]},
    ]
    for row, vals in {**outcomes, **decisions, **efficiency, **pipeline}.items():
        data.append({"range": data_range(row), "values": [vals]})

    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()

    apply_formatting(sheets, sheet_id, sheet_gid)


def apply_formatting(sheets, sheet_id: str, sheet_gid: int) -> None:
    NAVY = {"red": 0.06, "green": 0.14, "blue": 0.22}
    TEAL = {"red": 0.11, "green": 0.45, "blue": 0.58}
    LIGHT_TEAL = {"red": 0.90, "green": 0.94, "blue": 0.96}
    LIGHT_GRAY = {"red": 0.95, "green": 0.95, "blue": 0.96}

    def cell_range(start_row, start_col, end_row, end_col):
        return {
            "sheetId": sheet_gid,
            "startRowIndex": start_row - 1,
            "endRowIndex": end_row,
            "startColumnIndex": start_col,
            "endColumnIndex": end_col,
        }

    requests = []

    requests.append({"repeatCell": {
        "range": cell_range(ROW_TITLE, 0, ROW_TITLE, TOTAL_COLS),
        "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "fontSize": 16,
                           "foregroundColor": NAVY},
            "verticalAlignment": "MIDDLE",
        }},
        "fields": "userEnteredFormat.textFormat,userEnteredFormat.verticalAlignment",
    }})

    requests.append({"repeatCell": {
        "range": cell_range(ROW_SUBTTL, 0, ROW_SUBTTL, TOTAL_COLS),
        "cell": {"userEnteredFormat": {
            "textFormat": {"italic": True, "fontSize": 10,
                           "foregroundColor": {"red": 0.4, "green": 0.42, "blue": 0.48}},
        }},
        "fields": "userEnteredFormat.textFormat",
    }})

    requests.append({"repeatCell": {
        "range": cell_range(ROW_HEADER, 0, ROW_HEADER, TOTAL_COLS),
        "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "fontSize": 10, "foregroundColor": TEAL},
            "backgroundColor": LIGHT_GRAY,
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "borders": {"bottom": {"style": "SOLID_MEDIUM",
                                    "color": {"red": 0.7, "green": 0.75, "blue": 0.8}}},
        }},
        "fields": ("userEnteredFormat.textFormat,userEnteredFormat.backgroundColor,"
                   "userEnteredFormat.horizontalAlignment,"
                   "userEnteredFormat.verticalAlignment,"
                   "userEnteredFormat.borders"),
    }})

    requests.append({"repeatCell": {
        "range": cell_range(ROW_HEADER + 1, 0, 25, 1),
        "cell": {"userEnteredFormat": {
            "textFormat": {"bold": True, "foregroundColor": NAVY, "fontSize": 10},
        }},
        "fields": "userEnteredFormat.textFormat",
    }})

    for section_row in (SEC_OUTCOMES_HDR, SEC_DECISIONS_HDR,
                        SEC_EFFICIENCY_HDR, SEC_PIPELINE_HDR):
        requests.append({"repeatCell": {
            "range": cell_range(section_row, 0, section_row, TOTAL_COLS),
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True, "fontSize": 11,
                               "foregroundColor": TEAL},
                "backgroundColor": LIGHT_TEAL,
            }},
            "fields": "userEnteredFormat.textFormat,userEnteredFormat.backgroundColor",
        }})

    number_ranges = [
        (OUT_RESUMES, OUT_NOSHOW),
        (DEC_TO_HR, DEC_PAUSED),
        (EFF_HOURS, EFF_DOLLARS),
        (PIPE_ACTIVE, PIPE_AWAITING),
    ]
    for r0, r1 in number_ranges:
        requests.append({"repeatCell": {
            "range": cell_range(r0, 1, r1 + 1, TOTAL_COLS),
            "cell": {"userEnteredFormat": {
                "horizontalAlignment": "RIGHT",
                "numberFormat": {"type": "NUMBER", "pattern": "#,##0"},
                "textFormat": {"fontSize": 10},
            }},
            "fields": ("userEnteredFormat.horizontalAlignment,"
                       "userEnteredFormat.numberFormat,"
                       "userEnteredFormat.textFormat"),
        }})

    requests.append({"repeatCell": {
        "range": cell_range(EFF_DOLLARS, 1, EFF_DOLLARS + 1, TOTAL_COLS),
        "cell": {"userEnteredFormat": {
            "numberFormat": {"type": "CURRENCY", "pattern": "$#,##0"},
        }},
        "fields": "userEnteredFormat.numberFormat",
    }})

    requests.append({"updateSheetProperties": {
        "properties": {"sheetId": sheet_gid,
                       "gridProperties": {"frozenRowCount": 4,
                                          "frozenColumnCount": 1}},
        "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
    }})

    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_gid, "dimension": "COLUMNS",
                  "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 230},
        "fields": "pixelSize",
    }})
    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_gid, "dimension": "COLUMNS",
                  "startIndex": 1, "endIndex": TOTAL_COLS},
        "properties": {"pixelSize": 88},
        "fields": "pixelSize",
    }})

    requests.append({"updateDimensionProperties": {
        "range": {"sheetId": sheet_gid, "dimension": "ROWS",
                  "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 34},
        "fields": "pixelSize",
    }})

    sheets.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id, body={"requests": requests},
    ).execute()


# ----------------------------- Entry point ----------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    source_sheet_id = _required("SHEET_ID")
    dashboard_tab = _optional("DASHBOARD_TAB_NAME", "Candidates")
    metrics_tab = _optional("METRICS_TAB_NAME", "Metrics")

    log.info("Building Google API credentials")
    creds = build_credentials()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    log.info("Reading Candidates from %s", source_sheet_id)
    rows = read_candidates(sheets, source_sheet_id, dashboard_tab)
    log.info("Loaded %d candidate rows", len(rows))

    now = datetime.now(timezone.utc)
    months = months_from_anchor(METRICS_ANCHOR_MONTH, MONTHS_WINDOW)
    log.info("Month columns: %s .. %s", months[0], months[-1])
    metrics = compute_metrics(rows, now, months)

    sheet_gid = ensure_metrics_tab(sheets, source_sheet_id, metrics_tab)

    log.info("Writing %s tab on %s (gid %s)", metrics_tab, source_sheet_id, sheet_gid)
    write_metrics_tab(sheets, source_sheet_id, sheet_gid, metrics_tab,
                     metrics, months, now)

    log.info("Done. Week/YTD/All-Time received: %d / %d / %d",
             metrics["week"]["resumes_received"],
             metrics["ytd"]["resumes_received"],
             metrics["alltime"]["resumes_received"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
