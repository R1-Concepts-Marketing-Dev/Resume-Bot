"""Resume Bot Metrics -- nightly aggregator.

Reads the Candidates tab from the main Resume Bot sheet, aggregates
into three time buckets (This Week / This Month / YTD), and writes a
single "Metrics" tab back into the SAME sheet -- one place for HR to
look, self-contained.

Framing: HR ops metrics + bot efficiency, side by side. The five
headline numbers Lisa asked for (resumes received, interviews
scheduled, hired, rejected, no shows) live at the top; bot decisions
and pipeline state sit below.

Designed to run as a standalone GitHub Actions job at 5pm PT each
night. Self-contained -- does NOT import from the rest of the bot
package so it can be invoked independently.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)


# ----------------------------- Configuration --------------------------------

# HR Status values that count toward the outcome buckets Lisa asked for.
STATUS_INTERVIEW_SCHEDULED = "Interview Scheduled"
STATUS_HIRED = "Hired"
STATUS_REJECTED = "Rejected"
STATUS_NO_SHOW = "No Show"

# HR Status values that count as "active in HR pipeline" (point in
# time, current state). Excludes Hired (terminal).
ACTIVE_HR_STATUSES = {
    "In Review", "Contacted", "Interview Scheduled",
    "Offer Made", "On Hold",
}

# Estimated seconds of HR time saved per auto-handled resume.
DEFAULT_TIME_SAVED_PER_CASE_SEC = 120
try:
    TIME_SAVED_PER_CASE_SEC = int(
        (os.environ.get("TIME_SAVED_PER_CASE_SEC") or "").strip()
        or DEFAULT_TIME_SAVED_PER_CASE_SEC
    )
except ValueError:
    TIME_SAVED_PER_CASE_SEC = DEFAULT_TIME_SAVED_PER_CASE_SEC

DEFAULT_HOURLY_RATE_USD = 20.0


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


def in_week(ts: datetime, now: datetime) -> bool:
    """Timestamp falls in the last 7 days (inclusive)."""
    return ts >= now - timedelta(days=7)


def in_month(ts: datetime, now: datetime) -> bool:
    return ts.year == now.year and ts.month == now.month


def in_year(ts: datetime, now: datetime) -> bool:
    return ts.year == now.year


# ----------------------------- Aggregation ----------------------------------

def compute_metrics(rows: list[list[str]], now: datetime) -> dict:
    """Roll up Candidates rows into Week / Month / YTD / All-time buckets.

    Column layout (v3, from Candidates!A2:T):
      A Timestamp   J Decision   R HR Status
    """
    def new_bucket():
        return {
            "resumes_received": 0,
            "sent_to_hr": 0,        # decision == qualified
            "flagged_for_review": 0, # decision == needs_review
            "auto_denied": 0,        # decision == not_qualified
            "pending_paused": 0,
            "interviews_scheduled": 0,
            "hired": 0,
            "rejected": 0,
            "no_show": 0,
        }

    week  = new_bucket()
    month = new_bucket()
    ytd   = new_bucket()
    alltime = new_bucket()

    # Point-in-time state (not time-bucketed)
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

        buckets = [alltime]
        if in_year(ts, now):
            buckets.append(ytd)
        if in_month(ts, now):
            buckets.append(month)
        if in_week(ts, now):
            buckets.append(week)

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

        # Point-in-time HR state (from current row, not time-bucketed)
        if hr_status in ACTIVE_HR_STATUSES:
            active_in_pipeline += 1
        if not hr_status and decision in {"qualified", "needs_review"}:
            awaiting_triage += 1

    return {
        "week": week, "month": month, "ytd": ytd, "alltime": alltime,
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
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{tab}!A2:T",
    ).execute()
    return resp.get("values", [])


def ensure_metrics_tab(sheets, sheet_id: str, tab: str) -> None:
    """Create the Metrics tab if it doesn't already exist. Idempotent."""
    meta = sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sh in meta.get("sheets", []):
        if sh.get("properties", {}).get("title") == tab:
            return
    log.info("Creating '%s' tab", tab)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab,
                                                        "gridProperties": {
                                                            "rowCount": 40,
                                                            "columnCount": 6,
                                                        }}}}]},
    ).execute()


def write_metrics_tab(sheets, sheet_id: str, tab: str,
                      metrics: dict, now_utc: datetime) -> None:
    """Write the Metrics tab layout. Column widths and formatting are
    left for HR to tweak; the bot only writes values + labels."""
    pt = now_utc - timedelta(hours=7)
    last_run = f"Auto-updates nightly at 5pm PT · Last run: {pt.strftime('%Y-%m-%d %H:%M PT')}"

    w, m, y = metrics["week"], metrics["month"], metrics["ytd"]

    def row3(field):
        return [w[field], m[field], y[field]]

    # Efficiency computed from ALL auto-handled cases in YTD
    ytd_bucket = metrics["ytd"]
    ytd_handled = (ytd_bucket["sent_to_hr"] + ytd_bucket["auto_denied"]
                   + ytd_bucket["pending_paused"])
    ytd_seconds_saved = ytd_handled * TIME_SAVED_PER_CASE_SEC
    ytd_hours_saved = ytd_seconds_saved / 3600
    try:
        hourly_rate = float(_optional("HOURLY_RATE_USD",
                                       str(DEFAULT_HOURLY_RATE_USD)))
    except ValueError:
        hourly_rate = DEFAULT_HOURLY_RATE_USD
    ytd_dollars_saved = ytd_hours_saved * hourly_rate

    m_handled = m["sent_to_hr"] + m["auto_denied"] + m["pending_paused"]
    m_hours_saved = (m_handled * TIME_SAVED_PER_CASE_SEC) / 3600
    m_dollars_saved = m_hours_saved * hourly_rate

    w_handled = w["sent_to_hr"] + w["auto_denied"] + w["pending_paused"]
    w_hours_saved = (w_handled * TIME_SAVED_PER_CASE_SEC) / 3600
    w_dollars_saved = w_hours_saved * hourly_rate

    # A-column labels (fully self-managed by the bot)
    a_labels = [
        ["Resume Bot Metrics"],                              # A1
        [last_run],                                          # A2
        [""],                                                # A3
        [""],                                                # A4 (headers written to A4:D4)
        ["HR OUTCOMES"],                                     # A5
        ["Resumes received (unique)"],                       # A6
        ["Scheduled for interviews"],                        # A7
        ["Hired"],                                           # A8
        ["Rejected"],                                        # A9
        ["No shows"],                                        # A10
        [""],                                                # A11
        ["BOT DECISIONS"],                                   # A12
        ["Sent to HR (qualified)"],                          # A13
        ["Flagged for review"],                              # A14
        ["Auto-denied"],                                     # A15
        ["Pending / paused role"],                           # A16
        [""],                                                # A17
        ["BOT EFFICIENCY"],                                  # A18
        ["HR time saved (hrs)"],                             # A19
        ["$ saved (@ $%d/hr)" % int(hourly_rate)],           # A20
        [""],                                                # A21
        ["CURRENT PIPELINE (point-in-time)"],                # A22
        ["Active in HR pipeline"],                           # A23
        ["Awaiting HR triage"],                              # A24
    ]

    # Column headers on row 4
    col_headers = [["Metric", "This Week", "This Month", "YTD"]]

    # Data rows
    outcome_data = [
        row3("resumes_received"),
        row3("interviews_scheduled"),
        row3("hired"),
        row3("rejected"),
        row3("no_show"),
    ]
    decision_data = [
        row3("sent_to_hr"),
        row3("flagged_for_review"),
        row3("auto_denied"),
        row3("pending_paused"),
    ]
    efficiency_data = [
        [round(w_hours_saved, 1), round(m_hours_saved, 1), round(ytd_hours_saved, 1)],
        [f"${w_dollars_saved:.0f}", f"${m_dollars_saved:.0f}", f"${ytd_dollars_saved:.0f}"],
    ]
    pipeline_data = [
        [metrics["active_in_pipeline"], "", ""],
        [metrics["awaiting_triage"], "", ""],
    ]

    data = [
        {"range": f"{tab}!A1:A24", "values": a_labels},
        {"range": f"{tab}!A4:D4",  "values": col_headers},
        {"range": f"{tab}!B6:D10", "values": outcome_data},
        {"range": f"{tab}!B13:D16","values": decision_data},
        {"range": f"{tab}!B19:D20","values": efficiency_data},
        {"range": f"{tab}!B23:D24","values": pipeline_data},
    ]

    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
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
    metrics = compute_metrics(rows, now)

    ensure_metrics_tab(sheets, source_sheet_id, metrics_tab)

    log.info("Writing %s tab on %s", metrics_tab, source_sheet_id)
    write_metrics_tab(sheets, source_sheet_id, metrics_tab, metrics, now)

    log.info(
        "Done. Received (week/month/YTD): %d / %d / %d",
        metrics["week"]["resumes_received"],
        metrics["month"]["resumes_received"],
        metrics["ytd"]["resumes_received"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
