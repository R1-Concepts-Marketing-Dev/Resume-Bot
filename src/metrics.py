"""Resume Bot Metrics -- nightly aggregator.

Reads the Candidates tab from the source Resume Bot sheet, rolls up six
forward months of activity starting at the current month, and writes
the result into the Overview tab of the metrics sheet.

The framing is INTERVIEW-STAGE FILTER ACCURACY, not hire prediction:
the bot's job is to decide whether HR should look at a candidate; we
measure that by whether HR engaged (moved the candidate to In Review,
Contacted, Interview Scheduled, Offer Made, On Hold, or Hired) -- not
by whether they were ultimately hired. Hire outcomes depend on too
many factors the bot doesn't see (interview performance, offers,
salary, references).

Designed to run as a standalone GitHub Actions job at 5pm PT each night.
Self-contained -- does NOT import from the rest of the bot package so it
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

# Statuses where HR is currently working a candidate (point-in-time queue
# count). Excludes Hired, which is terminal.
ACTIVE_HR_STATUSES = {
    "In Review",
    "Contacted",
    "Interview Scheduled",
    "Offer Made",
    "On Hold",
}
# All statuses where HR moved a candidate past the auto-archive stage --
# i.e. the bot's "qualified" decision was validated by an HR action.
# This is what we count for pass-through rate.
ENGAGED_HR_STATUSES = ACTIVE_HR_STATUSES | {"Hired"}

# Estimated seconds of HR time saved per resume the bot auto-handled
# (qualified auto-reply, denial, or paused-role notice). Tuning knob.
DEFAULT_TIME_SAVED_PER_CASE_SEC = 120
try:
    TIME_SAVED_PER_CASE_SEC = int(
        (os.environ.get("TIME_SAVED_PER_CASE_SEC") or "").strip()
        or DEFAULT_TIME_SAVED_PER_CASE_SEC
    )
except ValueError:
    TIME_SAVED_PER_CASE_SEC = DEFAULT_TIME_SAVED_PER_CASE_SEC

# Fully loaded hourly rate for HR triage work. Used to convert time-saved
# into a dollar figure for the upper-management KPI band. Tunable via the
# HOURLY_RATE_USD env var; defaults to $20/hour.
DEFAULT_HOURLY_RATE_USD = 20.0

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

    rows: raw values from Candidates!A2:T (20 columns A..T, post v3 layout)
    months: list of YYYY-MM keys we want to aggregate over

    Column map (v3 layout 2026-06-25 -- Prior Rejection moved to M,
    Bot Feedback added at T; HR-input cols cluster at the end):
      A Timestamp        | F Original Filename | K Years Exp        | P Drive File
      B Candidate Name   | G Applied For       | L Job Hopping      | Q Gmail Thread
      C Email            | H Cross-Fit Match   | M Prior Rejection  | R HR Status
      D Phone            | I Cross-Fit Flag    | N Confidence       | S HR Notes
      E Application Sub. | J Decision          | O AI Reasoning     | T Bot Feedback

    Engagement framing: any time we measure "the bot's decision was
    validated by HR," we check hr_status in ENGAGED_HR_STATUSES, which
    includes all engaged statuses (In Review through Hired). Hire-only
    is no longer used as a success metric because the bot's job is to
    filter to interview, not to predict hire.
    """
    counts: dict[str, dict[str, int]] = {m: defaultdict(int) for m in months}
    active_queue = 0
    backlog = 0

    for raw in rows:
        # Pad to 20 columns (v3 layout) so missing trailing cells become empty strings
        row = (raw + [""] * 20)[:20]
        timestamp = row[0]
        cross_fit_raw = (row[8] or "").strip()  # col I = Cross-Fit Flag
        # Cross-fit flag was historically "Yes"/"No", now an emoji.
        # Treat both representations as cross-fit so historical rows still count.
        cross_fit_is_yes = cross_fit_raw == "\U0001F6A8" or cross_fit_raw.lower() == "yes"
        decision = (row[9] or "").strip().lower()       # col J = Decision
        prior_rejection = (row[12] or "").strip()       # col M = Prior Rejection (v3)
        hr_status = (row[17] or "").strip()             # col R = HR Status (v3)

        # Point-in-time queue state (NOT month-scoped):
        #   active_queue = candidates HR is currently working
        #   backlog = bot output that HR hasn't touched yet
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
            if hr_status in ENGAGED_HR_STATUSES:
                c["qualified_engaged"] += 1
        elif decision == "needs_review":
            c["needs_review"] += 1
            if hr_status in ENGAGED_HR_STATUSES:
                c["needs_review_engaged"] += 1
        elif decision == "not_qualified":
            c["not_qualified"] += 1
        elif decision == "pending_paused":
            c["pending_paused"] += 1
        elif decision == "unreadable":
            c["unreadable"] += 1

        # Cross-fit catches: HR engaged with a candidate the bot flagged
        # as a fit for a DIFFERENT role than they applied to.
        if cross_fit_is_yes and hr_status in ENGAGED_HR_STATUSES:
            c["cross_fit_catches"] += 1

        # Prior-rejection catches: the bot spotted a re-applicant whose
        # earlier app was already Rejected (Lisa's duplicate-detect ask).
        # Any non-empty value in col S means the flag fired on this row.
        if prior_rejection:
            c["prior_rejection_catches"] += 1

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

    Returns a dict with keys:
      header   (1xN): month labels
      volume   (6xN): per-month volume counts
      outcomes (6xN): per-month filter accuracy (engagement-framed)
      workflow (3xN): point-in-time HR pipeline + per-month time saved
      kpi_band (dict): hero KPI tile values for the top of the sheet

    All values are 2D lists ready to drop into a sheet range using
    values.batchUpdate, except kpi_band (dict consumed by write_overview).
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

    # FILTER ACCURACY: engagement-framed (bot decision validated by HR
    # moving the candidate past auto-archive). Six rows, matches sheet
    # labels at A15:A20.
    outcomes = [
        per_month(lambda c: c["qualified_engaged"]),
        per_month(lambda c: safe_pct(c["qualified_engaged"], c["qualified"])),
        per_month(lambda c: c["needs_review_engaged"]),
        per_month(lambda c: safe_pct(c["needs_review_engaged"], c["needs_review"])),
        per_month(lambda c: c["cross_fit_catches"]),
        per_month(lambda c: c["prior_rejection_catches"]),
    ]

    # HR PIPELINE: row order matches sheet labels at A23:A25.
    #   Row 23: Awaiting HR action       -> backlog (point-in-time)
    #   Row 24: Active in HR pipeline    -> active_queue (point-in-time)
    #   Row 25: Estimated HR time saved  -> per-month, in HOURS (not min)
    workflow = [
        [metrics["backlog"]] + [""] * 5,
        [metrics["active_queue"]] + [""] * 5,
        per_month(lambda c: round(c["time_saved_sec"] / 3600, 1)),
    ]

    # ----- Upper-management KPI band (top of sheet, current-month focus) -----
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

    # Pass-through rate: of candidates the bot said "qualified," what %
    # did HR validate by engaging? Empty string if no qualified yet --
    # avoids a misleading "0.0%" on day 1.
    pass_through_pct = safe_pct(this_month["qualified_engaged"],
                                this_month["qualified"]) or "--"

    kpi_band = {
        "hours_saved":       f"{hours_saved:.1f} hrs",
        "dollars_saved":     f"${dollars_saved:,.0f}",
        "resumes_scored":    this_month["total"],
        "pass_through":      pass_through_pct,
        "cross_fit_catches": this_month["cross_fit_catches"],
        "month_label":       month_label(months[0]),
        "hourly_rate":       f"${hourly_rate:.0f}/hr assumed",
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
        range=f"{tab}!A2:T",
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
    # Hero KPI band at the top, five cells across. Tile #4 is the bot's
    # pass-through rate (qualified -> HR engaged) -- the headline accuracy
    # number for upper management. Replaces the old "Qualified" count,
    # which was redundant with the Volume block.
    kpi_labels = [
        f"Hours Saved ({kpi['month_label']})",
        f"$ Saved ({kpi['month_label']})",
        "Resumes Scored",
        "Pass-through Rate",
        "Cross-Fit Catches",
    ]
    kpi_values = [
        kpi["hours_saved"],
        kpi["dollars_saved"],
        kpi["resumes_scored"],
        kpi["pass_through"],
        kpi["cross_fit_catches"],
    ]

    # Row labels in col A are written by the bot so the sheet is fully
    # self-managed. HR moving / renaming rows can't desync the layout.
    # Using ASCII "->" instead of arrow glyphs to keep the file safe for
    # OneDrive sync (it has eaten unicode arrows mid-string before).
    row_labels_a = [
        ["Resume Bot Metrics"],                       # A1
        [last_run],                                   # A2
        [""], [""],                                   # A3, A4 (KPI band)
        [""],                                         # A5 (filler)
        ["VOLUME"],                                   # A6
        ["Resumes processed"],                        # A7
        ["Auto-approved (qualified)"],                # A8
        ["Sent for review"],                          # A9
        ["Auto-denied (not_qualified)"],              # A10
        ["Pending paused"],                           # A11
        ["Unreadable"],                               # A12
        [""],                                         # A13
        ["FILTER ACCURACY"],                          # A14
        ["Qualified -> HR engaged"],                  # A15
        ["Qualified pass-through rate %"],            # A16
        ["Needs-review -> HR engaged"],               # A17
        ["Needs-review pass-through rate %"],         # A18
        ["Cross-fit catches (HR engaged)"],           # A19
        ["Prior-rejection catches"],                  # A20 (NEW)
        [""],                                         # A21
        ["HR PIPELINE"],                              # A22
        ["Awaiting HR action (current)"],             # A23
        ["Active in HR pipeline (current)"],          # A24
        ["Estimated HR time saved (hrs / month)"],    # A25
    ]

    data = [
        {"range": "Overview!A1:A25", "values": row_labels_a},
        {"range": "Overview!A3:E3", "values": [kpi_labels]},
        {"range": "Overview!A4:E4", "values": [kpi_values]},
        {"range": "Overview!H3",
         "values": [[f"Assumes {kpi['hourly_rate']}, "
                     f"{TIME_SAVED_PER_CASE_SEC}s saved per auto-handled resume. "
                     "Pass-through rate stabilizes as HR works through pipeline."]]},
        {"range": "Overview!B5:G5", "values": [blocks["header"]]},
        {"range": "Overview!B7:G12", "values": blocks["volume"]},
        {"range": "Overview!B15:G20", "values": blocks["outcomes"]},
        {"range": "Overview!B23:G25", "values": blocks["workflow"]},
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
