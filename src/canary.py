"""Resume Bot canary -- hourly health check.

Runs a set of deterministic checks against the sheet + Gmail + GitHub
Actions and writes one row per check to the 'Bot Alerts' tab on the
main sheet. If any check fails, emails the configured recipient a
plain-text summary. If everything's green, silent -- no email spam.

Runs hourly during business hours via .github/workflows/canary.yml.
Exit codes:
  0 = ran cleanly (whether checks passed or failed -- we manage alerts
      ourselves, so GitHub notifications only fire when the canary
      itself crashes)
  2 = canary crashed (missing env var, unreachable APIs, etc.)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger("canary")


# ----- Config -----

ALERT_EMAIL_TO_DEFAULT = "benw@r1concepts.com"
CANDIDATES_TAB = "Candidates"
METRICS_TAB = "Metrics"
BOT_ERRORS_TAB = "Bot Errors"
ALERTS_TAB = "Bot Alerts"

# Expected v3 header row on Candidates. Each entry is a case-insensitive
# substring that must appear in the actual header cell. Loose enough
# that HR cosmetically renaming "Application" -> "Application Submitted"
# doesn't alarm, but a real reorder (HR Status moves from R to P) does.
EXPECTED_HEADERS = [
    "timestamp", "candidate name", "email", "phone",
    "application", "original file", "applied for",
    "cross-fit match", "cross-fit", "decision",
    "years", "job hopping", "prior rejection",
    "confidence", "reasoning", "drive",
    "gmail", "hr status", "hr notes", "bot feedback",
]

# HR Status values metrics.py knows how to bucket. Alert on any new one.
KNOWN_HR_STATUSES = {
    "", "In Review", "Contacted", "Interview Scheduled", "Offer Made",
    "On Hold", "Hired", "Rejected", "Not Interested", "Not Selected",
    "Not a fit", "Closed", "Withdrawn", "Unavailable", "No Show", "Saved",
}

ROW_GROWTH_THRESHOLD = 200
BOT_ERRORS_THRESHOLD = 5
METRICS_STALE_HOURS = 25


# ----- Env helpers -----

def _required(k):
    v = os.environ.get(k, "").strip()
    if not v:
        raise RuntimeError("missing " + k)
    return v


def _optional(k, d=""):
    return os.environ.get(k, d)


def _fmt_pt(dt):
    return (dt - timedelta(hours=7)).strftime("%Y-%m-%d %H:%M PT")


# ----- Google credentials -----

def build_creds():
    return Credentials(
        token=None,
        refresh_token=_required("GOOGLE_OAUTH_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_required("GOOGLE_OAUTH_CLIENT_ID"),
        client_secret=_required("GOOGLE_OAUTH_CLIENT_SECRET"),
    )


# ----- Bot Alerts tab bootstrap -----

def ensure_alerts_tab(sheets, sheet_id):
    meta = sheets.spreadsheets().get(spreadsheetId=sheet_id).execute()
    for sh in meta.get("sheets", []):
        if sh.get("properties", {}).get("title") == ALERTS_TAB:
            return
    log.info("Creating '%s' tab", ALERTS_TAB)
    sheets.spreadsheets().batchUpdate(
        spreadsheetId=sheet_id,
        body={"requests": [{"addSheet": {"properties": {
            "title": ALERTS_TAB,
            "gridProperties": {"rowCount": 500, "columnCount": 6},
        }}}]},
    ).execute()
    sheets.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=f"{ALERTS_TAB}!A1:F1",
        valueInputOption="USER_ENTERED",
        body={"values": [["Timestamp (PT)", "Check", "Status",
                          "Detail", "state", "notes"]]},
    ).execute()


# ----- Individual checks. Each returns (name, ok_bool, detail_str). -----

def check_dupes(sheets, sheet_id, state):
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!Q2:Q",
        valueRenderOption="FORMULA",
    ).execute()
    thread_re = re.compile(r"#(?:inbox|all)/([A-Za-z0-9]+)")
    threads = []
    for row in resp.get("values", []):
        if not row:
            continue
        m = thread_re.search(str(row[0]))
        if m:
            threads.append(m.group(1))
    counter = Counter(threads)
    dupes = {tid: n for tid, n in counter.items() if n > 1}
    if not dupes:
        return ("dupe_detector", True,
                f"{len(threads)} threads, 0 duplicates")
    top = sorted(dupes.items(), key=lambda x: -x[1])[:3]
    extras = sum(n - 1 for n in dupes.values())
    return ("dupe_detector", False,
            f"{extras} extra rows across {len(dupes)} duplicated threads; worst: "
            + ", ".join(f"{t}(x{n})" for t, n in top))


def check_headers(sheets, sheet_id):
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!A1:T1",
    ).execute()
    row = (resp.get("values", [[]])[0] + [""] * 20)[:20]
    diffs = []
    for i, exp_substr in enumerate(EXPECTED_HEADERS):
        got = str(row[i] if i < len(row) else "").strip()
        if exp_substr not in got.lower():
            diffs.append(f"col{chr(65 + i)}: expected substring {exp_substr!r}, got {got!r}")
    if not diffs:
        return ("header_drift", True, "20 headers match expected layout")
    return ("header_drift", False, "; ".join(diffs[:5]))


def check_hr_statuses(sheets, sheet_id):
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"{CANDIDATES_TAB}!R2:R",
    ).execute()
    unknown = Counter()
    total = 0
    for row in resp.get("values", []):
        val = ((row[0] if row else "") or "").strip()
        total += 1
        if val and val not in KNOWN_HR_STATUSES:
            unknown[val] += 1
    if not unknown:
        return ("hr_status_sanity", True,
                f"all HR Status values across {total} rows recognized")
    return ("hr_status_sanity", False,
            "unknown HR Status values: "
            + ", ".join(f"{v!r}(x{n})" for v, n in unknown.most_common(5)))


def check_metrics_freshness(sheets, sheet_id, now_utc):
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{METRICS_TAB}!A2",
    ).execute()
    cell = ""
    vals = resp.get("values", [])
    if vals and vals[0]:
        cell = str(vals[0][0])
    m = re.search(r"Last run: (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", cell)
    if not m:
        return ("metrics_freshness", False,
                "could not parse last-run timestamp; is metrics.yml running?")
    try:
        last_pt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
    except Exception:
        return ("metrics_freshness", False, f"unparseable timestamp: {m.group(1)}")
    last_utc = last_pt.replace(tzinfo=timezone.utc) + timedelta(hours=7)
    age_hrs = (now_utc - last_utc).total_seconds() / 3600
    if age_hrs > METRICS_STALE_HOURS:
        return ("metrics_freshness", False,
                f"Metrics tab last updated {age_hrs:.1f} hours ago (threshold {METRICS_STALE_HOURS}h)")
    return ("metrics_freshness", True, f"Metrics tab updated {age_hrs:.1f}h ago")


def check_bot_errors(sheets, sheet_id, now_utc):
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{BOT_ERRORS_TAB}!A2:A",
        ).execute()
    except Exception as e:
        return ("bot_errors_surge", True, f"Bot Errors tab unreadable ({e})")
    recent = 0
    cutoff = now_utc - timedelta(hours=24)
    for row in resp.get("values", []):
        if not row:
            continue
        ts = str(row[0]).strip()
        if not ts:
            continue
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                recent += 1
        except Exception:
            continue
    if recent > BOT_ERRORS_THRESHOLD:
        return ("bot_errors_surge", False,
                f"{recent} errors in last 24h (threshold {BOT_ERRORS_THRESHOLD})")
    return ("bot_errors_surge", True, f"{recent} errors in last 24h")


def check_row_growth(sheets, sheet_id, state):
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{CANDIDATES_TAB}!A:A",
    ).execute()
    current = max(0, len(resp.get("values", [])) - 1)
    state["current_row_count"] = current
    prev = state.get("last_row_count")
    if prev is None:
        return ("row_growth", True, f"baseline {current} rows (first canary run)")
    delta = current - prev
    if delta > ROW_GROWTH_THRESHOLD:
        return ("row_growth", False,
                f"Candidates grew by {delta} rows since last check (threshold {ROW_GROWTH_THRESHOLD}) -- possible duplicate creep or backlog spike")
    if delta < -5:
        return ("row_growth", False,
                f"Candidates SHRANK by {-delta} rows -- unexpected deletion?")
    return ("row_growth", True, f"grew by {delta} rows ({current} total)")


def check_bot_run_health():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return ("bot_run_health", True, "GITHUB_TOKEN not set; skipping")
    url = ("https://api.github.com/repos/R1-Concepts-Marketing-Dev/"
           "Resume-Bot/actions/workflows/run.yml/runs?per_page=5")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        return ("bot_run_health", False, f"GitHub API unreachable: {e}")
    runs = data.get("workflow_runs", [])
    if not runs:
        return ("bot_run_health", True, "no recent run.yml runs")
    concl_latest = runs[0].get("conclusion") or runs[0].get("status")
    fails = sum(1 for r in runs if r.get("conclusion") == "failure")
    if fails >= 3:
        return ("bot_run_health", False,
                f"{fails}/5 recent run.yml runs failed (latest={concl_latest})")
    if concl_latest == "failure":
        return ("bot_run_health", False,
                f"latest run.yml failed ({fails}/5 recent failures)")
    return ("bot_run_health", True, f"latest={concl_latest}, {fails}/5 recent failures")


def check_oauth_token(gmail, gmail_user):
    try:
        gmail.users().labels().list(userId=gmail_user).execute()
        return ("oauth_token", True, "Gmail token healthy")
    except Exception as e:
        return ("oauth_token", False,
                f"Gmail labels.list failed -- OAuth token may need refresh: {e}")


# ----- Alert logging + email -----

def write_alerts_rows(sheets, sheet_id, ts_pt_str, results):
    rows = []
    for name, ok, detail in results:
        status = "OK" if ok else "ALERT"
        rows.append([ts_pt_str, name, status, detail, "", ""])
    sheets.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{ALERTS_TAB}!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()


def send_alert_email(gmail, gmail_user, to, subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    gmail.users().messages().send(
        userId=gmail_user, body={"raw": raw},
    ).execute()


# ----- Persistent state -----

def load_state(sheets, sheet_id):
    state = {"last_row_count": None}
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{ALERTS_TAB}!E1",
        ).execute()
        vals = resp.get("values", [])
        if vals and vals[0]:
            raw = str(vals[0][0]).strip()
            if raw.isdigit():
                state["last_row_count"] = int(raw)
    except Exception:
        pass
    return state


def save_state(sheets, sheet_id, state):
    val = state.get("current_row_count")
    if val is None:
        return
    try:
        sheets.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"{ALERTS_TAB}!E1",
            valueInputOption="USER_ENTERED",
            body={"values": [[str(val)]]},
        ).execute()
    except Exception as e:
        log.warning("save_state failed: %s", e)


# ----- Main -----

def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    sheet_id = _required("SHEET_ID")
    gmail_user = _required("GMAIL_USER")
    to_email = _optional("ALERT_EMAIL_TO", ALERT_EMAIL_TO_DEFAULT)

    creds = build_creds()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)

    ensure_alerts_tab(sheets, sheet_id)
    state = load_state(sheets, sheet_id)
    now = datetime.now(timezone.utc)
    ts_pt = _fmt_pt(now)

    results = []

    def run(fn, *args, **kwargs):
        name = fn.__name__.replace("check_", "")
        try:
            results.append(fn(*args, **kwargs))
        except Exception as e:
            log.exception("check %s crashed", name)
            results.append((name, False, f"check crashed: {e}"))

    run(check_dupes, sheets, sheet_id, state)
    run(check_headers, sheets, sheet_id)
    run(check_hr_statuses, sheets, sheet_id)
    run(check_metrics_freshness, sheets, sheet_id, now)
    run(check_bot_errors, sheets, sheet_id, now)
    run(check_row_growth, sheets, sheet_id, state)
    run(check_bot_run_health)
    run(check_oauth_token, gmail, gmail_user)

    write_alerts_rows(sheets, sheet_id, ts_pt, results)
    save_state(sheets, sheet_id, state)

    failed = [r for r in results if not r[1]]
    if failed:
        subject = f"[Resume Bot] Canary: {len(failed)} alert(s)"
        lines = [
            f"Canary run at {ts_pt}",
            f"{len(failed)} of {len(results)} checks failed:",
            "",
        ]
        for name, _ok, detail in failed:
            lines.append(f"  * {name}")
            lines.append(f"      {detail}")
        lines += [
            "",
            "Sheet: https://docs.google.com/spreadsheets/d/" + sheet_id + "/edit",
            "Full log tab: 'Bot Alerts'",
            "",
            "Passing checks:",
        ]
        for name, ok, detail in results:
            if ok:
                lines.append(f"  - {name}: {detail}")
        try:
            send_alert_email(gmail, gmail_user, to_email, subject,
                             "\n".join(lines))
            log.info("Sent alert email to %s (%d failures)", to_email, len(failed))
        except Exception as e:
            log.error("Failed to send alert email: %s", e)
    else:
        log.info("All %d checks passed", len(results))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("Canary crashed")
        sys.exit(2)
