"""Resume Bot Metrics — nightly aggregator.

Reads the Candidates tab from the source Resume Bot sheet, rolls up six
forward months of activity starting at the current month, and writes
the result into the Overview tab of the metrics sheet.

Designed to run as a standalone GitHub Actions job at 5pm PT each night.
Self-contained — does NOT import from the rest of the bot package so it
can be invoked independently. The only required env vars are OAuth
credentials and the two sheet IDs.
"""

from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)


# ----------------------------- Configuration --------------------------------

ACTIVE_HR_STATUSES = {
    "In Review",
    "Contacted",
    "Interview Scheduled",
    "Offer Made",
    "On Hold",
}
ENGAGED_HR_STATUSES = ACTIVE_HR_STATUSES | {"Hired"}

# Estimated seconds of HR time saved per resume the bot auto-handled
# (qualified auto-reply, denial, or paused-role notice). Tuning knob.
TIME_SAVED_PER_CASE_SEC = 90

# Fully loaded hourly rate for HR triage work. Used to convert time-saved
# into a dollar figure for the upper-management KPI band. Tunable via the
# HOURLY_RATE_USD env var; defaults to $35/hour.
DEFAULT_HOURLY_RATE_USD = 35.0

MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _required(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val


def _optional(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


# ----------------------------- Time helpers ---------------------------------

def six_month_window(now: datetime) -> list[str]:
    """Return six YYYY-MM keys starting at now's month, going forward."""
    out = []
    for i in range(6):
        m_idx = now.month - 1 + i
        year = now.year + m_idx // 12
        out.append(f"{year:04d}-{(m_idx % 12) + 1:02d}")
    return out


def month_label(key: str) -> str:
    """'2026-05' -> 'May 2026'."""
    year, month = key.split("-")
    return f"{MONTH_NAMES[int(month) - 1]} {year}"


def month_key(timestamp_str: str) -> Optional[str]:
    """Parse a Candidates Timestamp into YYYY-MM. Returns None on failure."""
    if not timestamp_str:
        return None
    s = timestamp_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(s)
    except Exception:
        return None
    return f"{ts.year:04d}-{ts.month:02d}"


# ----------------------------- Aggregation ----------------------------------

def compute_metrics(rows: list[list[str]], months: list[str]) -> dict:
    """Roll up Candidates rows into per-month counters + point-in-time state.

    rows: raw values from Candidates!A2:Q (17 columns A..Q)
    months: list of YYYY-MM keys we want to aggregate over
    """
    counts: dict[str, dict[str, int]] = {m: defaultdict(int) for m in months}
    active_queue = 0
    backlog = 0

    for raw in rows:
        # Pad to 17 columns so missing trailing cells become empty strings
        row = (raw + [""] * 17)[:17]
        timestamp = row[0]
        cross_fit_raw = (row[7] or "").strip()
        # Cross-fit flag was historically "Yes"/"No", now "🚨"/blank.
        # Treat both representations as cross-fit so historical rows still count.
        cross_fit_is_yes = cross_fit_raw == "🚨" or cross_fit_raw.lower() == "yes"
        decision = (row[8] or "").strip().lower()
        hr_status = (row[15] or "").strip()

        # Point-in-time queue state (NOT month-scoped)
        if hr_status in ACTIVE_HR_STATUSES:
            active_queue += 1
        if not hr_status and decision in {"qualified", "needs_review"}:
            backlog += 1

        mkey = month_key(timestamp)
        if not mkey or mkey not in counts:
            continue

        c = counts[mkey]
        c["total"] += 1

        if decision == "qualified":
            c["qualified"] += 1
            if hr_status == "Hired":
                c["qualified_hired"] += 1
        elif decision == "needs_review":
            c["needs_review"] += 1
            if hr_status == "Hired":
                c["needs_review_hired"] += 1
        elif decision == "not_qualified":
            c["not_qualified"] += 1
        elif decision == "pending_paused":
            c["pending_paused"] += 1
        elif decision == "unreadable":
            c["unreadable"] += 1

        if cross_fit_is_yes and hr_status in ENGAGED_HR_STATUSES:
            c["cross_fit_catches"] += 1

        if decision in {"qualified", "not_qualified", "pending_paused"}:
            c["time_saved_sec"] += TIME_SAVED_PER_CASE_SEC

    return {
        "months": months,
        "per_month": counts,
        "active_queue": active_queue,
        "backlog": backlog,
    }


# ----------------------------- Output blocks --------------------------------

def safe_pct(num: int, den: int) -> str:
    if den == 0:
        return ""
    return f"{(num / den) * 100:.1f}%"


def build_blocks(metrics: dict) -> dict:
    """Produce the value blocks for each region of the Overview tab.

    Returns a dict with keys: header (1xN), volume (6xN), outcomes (5xN),
    workflow (3xN). All values are 2D lists ready to drop into a sheet
    range using values.batchUpdate.
    """
    months = metrics["months"]

    def per_month(get):
        return [get(metrics["per_month"][m]) for m in months]

    header = [month_label(m) for m in months]

    volume = [
        per_month(lambda c: c["total"]),
        per_month(lambda c: c["qualified"]),
        per_month(lambda c: c["needs_review"]),
        per_month(lambda c: c["not_qualified"]),
        per_month(lambda c: c["pending_paused"]),
        per_month(lambda c: c["unreadable"]),
    ]

    outcomes = [
        per_month(lambda c: c["qualified_hired"]),
        per_month(lambda c: safe_pct(c["qualified_hired"], c["qualified"])),
        per_month(lambda c: c["needs_review_hired"]),
        per_month(lambda c: safe_pct(c["needs_review_hired"], c["needs_review"])),
        per_month(lambda c: c["cross_fit_catches"]),
    ]

    # Active queue and backlog are point-in-time: only meaningful in the
    # current-month column (idx 0). Estimated time saved IS per-month.
    workflow = [
        [metrics["active_queue"]] + [""] * 5,
        [metrics["backlog"]] + [""] * 5,
        per_month(lambda c: round(c["time_saved_sec"] / 60)),
    ]

    # ----- Upper-management KPI band (current month at-a-glance) -----
    # Reads HOURLY_RATE_USD from env at build time so changes propagate
    # without redeploying the workflow.
    try:
        hourly_rate = float(_optional("HOURLY_RATE_USD",
                                       str(DEFAULT_HOURLY_RATE_USD)))
    except ValueError:
        hourly_rate = DEFAULT_HOURLY_RATE_USD

    this_month = metrics["per_month"][months[0]]
    sec_saved = this_month["time_saved_sec"]
    hours_saved = sec_saved / 3600
    dollars_saved = hours_saved * hourly_rate

    kpi_band = {
        "hours_saved": f"{hours_saved:.1f} hrs",
        "dollars_saved": f"${dollars_saved:,.0f}",
        "resumes_scored": this_month["total"],
        "qualified": this_month["qualified"],
        "cross_fit_catches": this_month["cross_fit_catches"],
        "active_queue": metrics["active_queue"],
        "month_label": month_label(months[0]),
        "hourly_rate": f"${hourly_rate:.0f}/hr assumed",
    }

    return {
        "header": header,
        "volume": volume,
        "outcomes": outcomes,
        "workflow": workflow,
        "kpi_band": kpi_band,
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
        range=f"{tab}!A2:Q",
    ).execute()
    return resp.get("values", [])


def write_overview(sheets, metrics_sheet_id: str, blocks: dict,
                   now_utc: datetime) -> None:
    # Display PT roughly. We're in PDT for most of the year (Mar-Nov);
    # accept a 1h drift in winter rather than hand-rolling DST detection.
    pt = now_utc - timedelta(hours=7)
    last_run = (
        f"Auto-updates nightly at 5pm PT. "
        f"Last run: {pt.strftime('%Y-%m-%d %H:%M PT')}"
    )

    kpi = blocks["kpi_band"]
    # Hero KPI band at the top: five cells across, with a label row above
    # the value row, and a third row of context (current month, assumption).
    # Layout occupies rows 3-5; row 5 stays clear of the existing month
    # header which lives at B5:G5 because the KPI band only uses A-E.
    kpi_labels = [
        f"Hours Saved ({kpi['month_label']})",
        f"$ Saved ({kpi['month_label']})",
        "Resumes Scored",
        "Qualified",
        "Cross-Fit Catches",
    ]
    kpi_values = [
        kpi["hours_saved"],
        kpi["dollars_saved"],
        kpi["resumes_scored"],
        kpi["qualified"],
        kpi["cross_fit_catches"],
    ]

    data = [
        {"range": "Overview!A2", "values": [[last_run]]},
        {"range": "Overview!A3:E3", "values": [kpi_labels]},
        {"range": "Overview!A4:E4", "values": [kpi_values]},
        {"range": "Overview!H3",
         "values": [[f"Assumes {kpi['hourly_rate']}, "
                     f"{TIME_SAVED_PER_CASE_SEC}s saved per auto-handled resume"]]},
        {"range": "Overview!B5:G5", "values": [blocks["header"]]},
        {"range": "Overview!B7:G12", "values": blocks["volume"]},
        {"range": "Overview!B15:G19", "values": blocks["outcomes"]},
        {"range": "Overview!B22:G24", "values": blocks["workflow"]},
    ]
    sheets.spreadsheets().values().batchUpdate(
        spreadsheetId=metrics_sheet_id,
        body={"valueInputOption": "USER_ENTERED", "data": data},
    ).execute()


# ----------------------------- Entry point ----------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    source_sheet_id = _required("SHEET_ID")
    metrics_sheet_id = _required("METRICS_SHEET_ID")
    dashboard_tab = _optional("DASHBOARD_TAB_NAME", "Candidates")

    log.info("Building Google API credentials")
    creds = build_credentials()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    log.info("Reading Candidates from source sheet %s", source_sheet_id)
    rows = read_candidates(sheets, source_sheet_id, dashboard_tab)
    log.info("Loaded %d candidate rows", len(rows))

    now = datetime.now(timezone.utc)
    months = six_month_window(now)
    log.info("Aggregating for months: %s", months)

    metrics = compute_metrics(rows, months)
    blocks = build_blocks(metrics)

    log.info("Writing Overview tab on metrics sheet %s", metrics_sheet_id)
    write_overview(sheets, metrics_sheet_id, blocks, now)

    log.info(
        "Done. active_queue=%d backlog=%d",
        metrics["active_queue"], metrics["backlog"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
