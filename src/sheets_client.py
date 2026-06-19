"""Sheets operations: filters, templates, dashboard, errors."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


@dataclass
class Filter:
    role: str
    requirement: str
    job_hopping: str
    active: bool


@dataclass
class Template:
    key: str
    subject: str
    body: str
    active: bool


DASHBOARD_HEADERS = [
    "Timestamp", "Candidate Name", "Email", "Phone",
    "Application Submitted",  # E -- Email / Indeed / Craigslist / "Recruiter/Agency - {name}"
    "Original Filename",
    "Applied For", "Cross-Fit Match", "Cross-Fit Flag", "Decision",
    "Years Relevant Exp", "Job Hopping", "Confidence", "AI Reasoning",
    "Drive File Link", "Gmail Thread Link", "HR Status", "HR Notes",
    "Prior Rejection",  # S -- "🚩 Previously rejected" when same-name row already has terminal HR Status
]


MISC_HEADERS = [
    "Timestamp", "Sender", "Subject", "Original Filename",
    "Why Not A Resume", "Gmail Thread Link",
]


INBOX_LOG_HEADERS = [
    "Timestamp", "Sender", "Subject", "Type", "Action Taken",
    "Has Attachment", "Gmail Thread Link",
]


NEEDS_HUMAN_HEADERS = [
    "Timestamp", "Status", "Reason Type", "Why Flagged", "Sender",
    "Subject", "Body Preview", "Bot Guess", "Confidence", "Gmail Thread",
]

NEEDS_HUMAN_STATUSES = ["Open", "Investigating", "Resolved", "Ignored"]
NEEDS_HUMAN_REASON_TYPES = ["loop", "low_confidence", "indeed_fetch", "manual"]
NEEDS_HUMAN_OPEN_STATUSES = {"Open", "Investigating", ""}


# Compact action worklist for Indeed candidates. The bot dual-writes
# every Indeed candidate into both Candidates (full record) and this
# tab (just what HR needs to action it inside Indeed's own dashboard).
# HR ticks the "Indeed Application Closed" checkbox once they've moved
# the candidate in Indeed; a filter view hides closed rows.
# HR Status column is a VLOOKUP into Candidates by Timestamp so any HR
# Status edits on Candidates flow through automatically.
INDEED_QUEUE_HEADERS = [
    "Candidate Name", "Position", "Fit Quality", "AI Recommendation",
    "HR Status", "Indeed Application Closed", "Timestamp",
]


# Decision -> human-readable Fit Quality used on the Indeed Queue.
_INDEED_FIT_QUALITY = {
    "qualified":      "Strong",
    "needs_review":   "Needs review",
    "not_qualified":  "Not a fit",
    "pending_paused": "Hold - role paused",
    "unreadable":     "Unreadable resume",
}


# Decision -> recommended Indeed-platform action. HR uses this to decide
# what to click inside Indeed's employer dashboard for each candidate.
_INDEED_AI_RECOMMENDATION = {
    "qualified":      "Move to interview stage",
    "needs_review":   "Review resume + decide",
    "not_qualified":  "Decline / Not a fit",
    "pending_paused": "Hold - role paused",
    "unreadable":     "Review manually",
}


SEED_TEMPLATES: list[Template] = [
    Template(
        key="no_resume",
        subject="Please resend with your resume attached",
        body=(
            "Hi {applicant_name},\n\n"
            "Thanks for your interest in {company_name}. It looks like "
            "your resume didn't come through with your email -- could "
            "you reply with your resume attached as a PDF or Word "
            "document? Once we have it we'll review and get back to "
            "you.\n\n"
            "Thanks,\n"
            "{company_name} HR"
        ),
        active=True,
    ),
    Template(
        key="question",
        subject="Thanks for reaching out to {company_name}",
        body=(
            "Hi {applicant_name},\n\n"
            "Thanks for reaching out to {company_name}.\n\n"
            "For questions about open positions, pay, scheduling, or "
            "the application process, please contact our HR team "
            "directly at [HR contact email here] and we'll be happy "
            "to help.\n\n"
            "If you'd like to apply for a position, please reply to "
            "this email with your resume attached as a PDF or Word "
            "document.\n\n"
            "Thanks,\n"
            "{company_name} HR"
        ),
        active=True,
    ),
    Template(
        key="denied",
        subject="Thank you for your interest in {company_name}",
        body=(
            "Hi {applicant_name},\n\n"
            "Thank you for applying to {company_name}. After reviewing "
            "your background against the role's requirements, we don't "
            "have a current match. We'll keep your resume on file in "
            "case anything opens up.\n\n"
            "Best wishes in your search,\n"
            "{company_name} HR"
        ),
        active=True,
    ),
    Template(
        key="paused_match",
        subject="We'll keep your resume on file for {role}",
        body=(
            "Hi {applicant_name},\n\n"
            "Thanks for applying to {company_name}. Your background "
            "looks like a strong fit for {role}, but that role is "
            "currently on hold - we're not actively interviewing for "
            "it right now. We've added your resume to our pending file "
            "and will reach back out when that role re-opens.\n\n"
            "Thanks for your patience,\n"
            "{company_name} HR"
        ),
        active=True,
    ),
]


TEMPLATES_HEADERS = [["Template Key", "Subject", "Body", "Active"]]


def _is_truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"true", "yes", "y", "on", "1", "x"}


def _safe(v) -> str:
    if v is None:
        return ""
    s = str(v)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def _hyperlink(url: str, label: str = "Link") -> str:
    if not url:
        return ""
    escaped = url.replace('"', '""')
    return f'=HYPERLINK("{escaped}","{label}")'


def load_filters(svc, sheet_id, tab):
    rng = f"{tab}!A2:D"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    rows = resp.get("values", [])
    out = []
    for r in rows:
        r = (r + [""] * 4)[:4]
        role, req, hop, active = r
        if not role.strip() or not req.strip():
            continue
        out.append(Filter(
            role=role.strip(),
            requirement=req.strip(),
            job_hopping=hop.strip() or "Average tenure > 1 year = positive",
            active=_is_truthy(active),
        ))
    return out


def ensure_templates_seeded(svc, sheet_id, tab):
    head_rng = f"{tab}!A1:D1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=head_rng).execute()
    if resp.get("values"):
        return
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=head_rng, valueInputOption="RAW",
        body={"values": TEMPLATES_HEADERS},
    ).execute()
    rows = [[t.key, t.subject, t.body, "TRUE" if t.active else "FALSE"] for t in SEED_TEMPLATES]
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=f"{tab}!A2:D{1 + len(rows)}",
        valueInputOption="RAW", body={"values": rows},
    ).execute()


def load_templates(svc, sheet_id, tab):
    rng = f"{tab}!A2:D"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    rows = resp.get("values", [])
    out = {}
    for r in rows:
        r = (r + [""] * 4)[:4]
        key, subject, body, active = r
        if not key.strip() or not _is_truthy(active):
            continue
        out[key.strip()] = Template(
            key=key.strip(),
            subject=subject.strip(),
            body=body.replace("\\n", "\n"),
            active=True,
        )
    return out


def render_template(tmpl, vars):
    def fill(s):
        for k, v in vars.items():
            s = s.replace("{" + k + "}", str(v))
        return s
    return fill(tmpl.subject), fill(tmpl.body)


def ensure_dashboard_headers(svc, sheet_id, tab):
    """Idempotently set/extend the Candidates header row to DASHBOARD_HEADERS."""
    rng = f"{tab}!1:1"
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=rng,
        ).execute()
    except Exception as e:
        log.warning("Could not read %s headers: %s", tab, e)
        return
    existing = (resp.get("values") or [[]])[0]
    if not existing:
        end_col = _col_letter(len(DASHBOARD_HEADERS))
        write_rng = f"{tab}!A1:{end_col}1"
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=write_rng, valueInputOption="RAW",
            body={"values": [DASHBOARD_HEADERS]},
        ).execute()
        return
    if len(existing) >= len(DASHBOARD_HEADERS):
        return
    start_col = _col_letter(len(existing) + 1)
    end_col = _col_letter(len(DASHBOARD_HEADERS))
    write_rng = f"{tab}!{start_col}1:{end_col}1"
    new_cells = DASHBOARD_HEADERS[len(existing):]
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=write_rng, valueInputOption="RAW",
            body={"values": [new_cells]},
        ).execute()
        log.info("Extended %s headers with new columns: %s", tab, new_cells)
    except Exception as e:
        log.warning("Could not extend %s headers: %s", tab, e)


def _col_letter(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _ensure_tab_exists(svc, sheet_id, tab) -> bool:
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets.properties.title",
        ).execute()
    except Exception as e:
        log.warning("Could not load sheet metadata: %s", e)
        return False
    existing = [s["properties"]["title"]
                for s in meta.get("sheets", []) if s.get("properties")]
    if tab in existing:
        return True
    try:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
        return True
    except Exception as e:
        log.warning("Could not create tab %r: %s", tab, e)
        return False


def ensure_misc_headers(svc, sheet_id, tab):
    rng = f"{tab}!A1:F1"
    try:
        resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    except Exception as e:
        log.warning("Could not read %s headers (tab may not exist yet): %s", tab, e)
        return
    if resp.get("values"):
        return
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=rng, valueInputOption="RAW",
            body={"values": [MISC_HEADERS]},
        ).execute()
    except Exception as e:
        log.warning("Could not write %s headers: %s", tab, e)


def ensure_inbox_log_headers(svc, sheet_id, tab):
    rng = f"{tab}!A1:G1"
    try:
        resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    except Exception as e:
        log.warning("Could not read %s headers (tab may not exist yet): %s", tab, e)
        return
    if resp.get("values"):
        return
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=rng, valueInputOption="RAW",
            body={"values": [INBOX_LOG_HEADERS]},
        ).execute()
    except Exception as e:
        log.warning("Could not write %s headers: %s", tab, e)


def _get_sheet_id_for_tab(svc, spreadsheet_id, tab_name):
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=spreadsheet_id,
            fields="sheets.properties",
        ).execute()
    except Exception as e:
        log.warning("Could not load sheet metadata for tab lookup: %s", e)
        return None
    for s in meta.get("sheets", []):
        props = s.get("properties") or {}
        if props.get("title") == tab_name:
            return props.get("sheetId")
    return None


def ensure_needs_human_headers(svc, sheet_id, tab):
    if not _ensure_tab_exists(svc, sheet_id, tab):
        return
    rng = f"{tab}!A1:J1"
    try:
        resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    except Exception as e:
        log.warning("Could not read %s headers: %s", tab, e)
        return
    existing_headers = (resp.get("values") or [[]])[0]
    if not existing_headers:
        try:
            svc.spreadsheets().values().update(
                spreadsheetId=sheet_id, range=rng, valueInputOption="RAW",
                body={"values": [NEEDS_HUMAN_HEADERS]},
            ).execute()
        except Exception as e:
            log.warning("Could not write %s headers: %s", tab, e)
            return
    elif existing_headers != NEEDS_HUMAN_HEADERS:
        log.info(
            "%s headers don't match new schema -- run scripts/migrate_needs_human.py "
            "before relying on dedup. Existing headers: %s",
            tab, existing_headers,
        )
        return
    inner_id = _get_sheet_id_for_tab(svc, sheet_id, tab)
    if inner_id is None:
        return
    requests = _build_needs_human_format_requests(inner_id)
    if requests:
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=sheet_id, body={"requests": requests},
            ).execute()
        except Exception as e:
            log.warning("Could not apply %s formatting: %s", tab, e)


_NEEDS_HUMAN_COL_WIDTHS = {
    0: 140, 1: 120, 2: 110, 3: 280, 4: 220,
    5: 200, 6: 360, 7: 160, 8: 90, 9: 80,
}


def _build_needs_human_format_requests(inner_id):
    requests = []
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": inner_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })
    requests.append({
        "repeatCell": {
            "range": {
                "sheetId": inner_id,
                "startRowIndex": 0, "endRowIndex": 1,
                "startColumnIndex": 0, "endColumnIndex": len(NEEDS_HUMAN_HEADERS),
            },
            "cell": {
                "userEnteredFormat": {
                    "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                    "textFormat": {"bold": True},
                    "verticalAlignment": "MIDDLE",
                }
            },
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
        }
    })
    for col_index, width in _NEEDS_HUMAN_COL_WIDTHS.items():
        requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": inner_id, "dimension": "COLUMNS",
                    "startIndex": col_index, "endIndex": col_index + 1,
                },
                "properties": {"pixelSize": width},
                "fields": "pixelSize",
            }
        })
    for col_index in (3, 6):
        requests.append({
            "repeatCell": {
                "range": {
                    "sheetId": inner_id,
                    "startRowIndex": 1,
                    "startColumnIndex": col_index, "endColumnIndex": col_index + 1,
                },
                "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat.wrapStrategy",
            }
        })
    requests.append({
        "setDataValidation": {
            "range": {
                "sheetId": inner_id,
                "startRowIndex": 1,
                "startColumnIndex": 1, "endColumnIndex": 2,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": s} for s in NEEDS_HUMAN_STATUSES],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    })
    requests.append({
        "setDataValidation": {
            "range": {
                "sheetId": inner_id,
                "startRowIndex": 1,
                "startColumnIndex": 2, "endColumnIndex": 3,
            },
            "rule": {
                "condition": {
                    "type": "ONE_OF_LIST",
                    "values": [{"userEnteredValue": v} for v in NEEDS_HUMAN_REASON_TYPES],
                },
                "showCustomUi": True,
                "strict": False,
            },
        }
    })
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": inner_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": len(NEEDS_HUMAN_HEADERS),
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": "=$B2=\"Resolved\""}],
                    },
                    "format": {
                        "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
                        "textFormat": {"italic": True, "foregroundColor": {"red": 0.5, "green": 0.5, "blue": 0.5}},
                    },
                },
            },
            "index": 0,
        }
    })
    requests.append({
        "addConditionalFormatRule": {
            "rule": {
                "ranges": [{
                    "sheetId": inner_id,
                    "startRowIndex": 1,
                    "startColumnIndex": 0, "endColumnIndex": len(NEEDS_HUMAN_HEADERS),
                }],
                "booleanRule": {
                    "condition": {
                        "type": "CUSTOM_FORMULA",
                        "values": [{"userEnteredValue": "=$B2=\"Ignored\""}],
                    },
                    "format": {
                        "backgroundColor": {"red": 0.97, "green": 0.97, "blue": 0.97},
                        "textFormat": {"foregroundColor": {"red": 0.6, "green": 0.6, "blue": 0.6}},
                    },
                },
            },
            "index": 0,
        }
    })
    return requests


def append_inbox_log(svc, sheet_id, tab, row):
    try:
        values = [
            row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            _safe(row.get("sender", "")),
            _safe(row.get("subject", "")),
            _safe(row.get("type", "")),
            _safe(row.get("action", "")),
            "yes" if row.get("has_attachment") else "no",
            _hyperlink(row.get("gmail_link", "")),
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab}!A:G",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
    except Exception as e:
        log.warning("Failed to log to %s tab: %s", tab, e)


def append_misc(svc, sheet_id, tab, row):
    try:
        values = [
            row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            _safe(row.get("sender", "")),
            _safe(row.get("subject", "")),
            _safe(row.get("filename", "")),
            _safe(row.get("reasoning", "")),
            _hyperlink(row.get("gmail_link", "")),
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab}!A:F",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
    except Exception as e:
        log.warning("Failed to log to %s tab: %s", tab, e)


def _format_pt_timestamp(iso_or_dt=None):
    if iso_or_dt is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(iso_or_dt, str):
        try:
            dt = datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
        except ValueError:
            return iso_or_dt
    else:
        dt = iso_or_dt
    pt = dt - timedelta(hours=7)
    return pt.strftime("%Y-%m-%d %H:%M PT")


_BODY_PREVIEW_MAX_CHARS = 150


def _clean_body_preview(body):
    if not body:
        return ""
    try:
        from . import gmail_client
        cleaned = gmail_client.strip_quoted_text(body)
    except Exception:
        cleaned = body
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > _BODY_PREVIEW_MAX_CHARS:
        cleaned = cleaned[:_BODY_PREVIEW_MAX_CHARS - 1].rstrip() + "…"
    return cleaned


_THREAD_ID_PATTERN = re.compile(r"#inbox/([A-Za-z0-9_-]+)")


def _thread_id_from_link(cell_value):
    if not cell_value:
        return None
    m = _THREAD_ID_PATTERN.search(str(cell_value))
    return m.group(1) if m else None


def load_open_needs_human_threads(svc, sheet_id, tab):
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!B2:J",
            valueRenderOption="FORMULA",
        ).execute()
    except Exception as e:
        log.warning("Could not load open Needs Human threads: %s", e)
        return set()
    rows = resp.get("values", []) or []
    threads = set()
    for row in rows:
        status = (row[0] if len(row) > 0 else "").strip()
        if status and status not in NEEDS_HUMAN_OPEN_STATUSES:
            continue
        link_cell = row[8] if len(row) > 8 else ""
        tid = _thread_id_from_link(link_cell)
        if tid:
            threads.add(tid)
    return threads


def append_needs_human(svc, sheet_id, tab, row):
    try:
        reason_type = (row.get("reason_type") or "manual").strip().lower()
        if reason_type not in NEEDS_HUMAN_REASON_TYPES:
            reason_type = "manual"
        timestamp = row.get("timestamp")
        values = [
            _format_pt_timestamp(timestamp),
            "Open",
            reason_type,
            _safe(row.get("reason", "")),
            _safe(row.get("sender", "")),
            _safe(row.get("subject", "")),
            _safe(_clean_body_preview(row.get("body_preview", ""))),
            _safe(row.get("bot_guess", "")),
            _safe(row.get("confidence", "")),
            _hyperlink(row.get("gmail_link", "")),
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab}!A:J",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
    except Exception as e:
        log.warning("Failed to log to %s tab: %s", tab, e)


def load_known_candidate_emails(svc, sheet_id, tab):
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!C2:C",
        ).execute()
    except Exception as e:
        log.warning("Could not load known candidate emails: %s", e)
        return set()
    out = set()
    for row in resp.get("values", []):
        if not row:
            continue
        val = str(row[0]).strip().lower()
        if val.startswith("'"):
            val = val[1:]
        if val and "@" in val:
            out.add(val)
    return out


def load_recent_inbox_senders(svc, sheet_id, tab, hours_back=24):
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!A2:B",
        ).execute()
    except Exception as e:
        log.warning("Could not load Inbox Log for loop detection: %s", e)
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    counts = {}
    for r in resp.get("values", []):
        if len(r) < 2:
            continue
        ts_str, sender_raw = r[0], r[1]
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        sender = str(sender_raw).strip().lower()
        if sender.startswith("'"):
            sender = sender[1:]
        m = re.search(r"[\w.+-]+@[\w.-]+", sender)
        if m:
            sender = m.group(0)
        if sender:
            counts[sender] = counts.get(sender, 0) + 1
    return counts


def load_processed_thread_ids(svc, sheet_id, tab):
    # Gmail Thread Link lives in column P (was column O before the
    # 2026-06-18 restructure that added Application Submitted at E).
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!P2:P",
            valueRenderOption="FORMULA",
        ).execute()
    except Exception:
        return set()
    ids = set()
    for row in resp.get("values", []):
        if not row:
            continue
        cell = str(row[0])
        m = re.search(r"#inbox/([A-Za-z0-9]+)", cell)
        if m:
            ids.add(m.group(1))
    return ids


def append_error(svc, sheet_id, tab, row):
    try:
        values = [
            row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            row.get("msg_id", ""),
            row.get("sender_email", ""),
            row.get("filename", ""),
            row.get("error_type", ""),
            str(row.get("detail", ""))[:1000],
            row.get("bot_action", ""),
            row.get("gmail_link", ""),
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab}!A:H",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
    except Exception as e:
        log.warning("Failed to log to %s tab: %s", tab, e)


_PRIOR_REJECTION_FLAG = "🚩 Previously rejected"
_REJECTED_STATUSES = ("Rejected", "Not Selected", "Not a fit")


def _has_prior_rejection(svc, sheet_id, candidate_name,
                         tab="Candidates"):
    """Return True if any existing Candidates row has the same name AND
    a terminal-rejection HR Status. Match is case-insensitive on trimmed
    name. Reads B (name) and Q (HR Status)."""
    if not candidate_name or not str(candidate_name).strip():
        return False
    target = str(candidate_name).strip().lower()
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{tab}!B2:Q",
        ).execute()
    except Exception as e:
        log.warning("prior-rejection lookup failed for %s: %s", candidate_name, e)
        return False
    for r in resp.get("values", []) or []:
        r = (r + [""] * 16)[:16]
        row_name = str(r[0] or "").strip().lower()
        # B is offset 0 in the slice; Q is offset 15 (B..Q = 16 cols).
        hr_status = str(r[15] or "").strip()
        if row_name == target and hr_status in _REJECTED_STATUSES:
            return True
    return False


def append_candidate(svc, sheet_id, tab, row):
    """Append a candidate row to the Candidates dashboard (columns A:R).

    Application Submitted (E): Where the application came from. One of
    "Email", "Indeed", "Craigslist", or "Recruiter/Agency - {name}"
    (with the actual agency name extracted by the scorer when known).
    Computed in main.py from sender + scorer recruiter_agency signal +
    body keyword match for Craigslist.

    The Recruiter/Agency column, Indeed boolean, and Indeed Action Done
    columns from the prior layout were collapsed into this single
    Application Submitted column on 2026-06-18 -- the per-source detail
    they carried is now either in Application Submitted itself (recruiter
    name) or available via filter on Application Submitted=Indeed
    (Indeed Queue tab) so the dedicated boolean/checkbox columns were
    redundant.
    """
    application_submitted = (row.get("application_submitted") or "Email").strip() or "Email"
    prior_flag = _PRIOR_REJECTION_FLAG if _has_prior_rejection(svc, sheet_id, row.get("candidate_name", ""), tab) else ""
    values = [
        row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        _safe(row.get("candidate_name", "")),
        _safe(row.get("email", "")),
        _safe(row.get("phone", "")),
        _safe(application_submitted),  # E: Application Submitted
        _safe(row.get("filename", "")),
        _safe(row.get("applied_for", "")),
        _safe(row.get("cross_fit_match", "")),
        row.get("cross_fit_flag", ""),
        row.get("decision", ""),
        row.get("years_relevant_experience", ""),
        _safe(row.get("job_hopping_flag", "")),
        row.get("confidence", ""),
        _safe(row.get("reasoning", "")),
        _hyperlink(row.get("drive_link", "")),
        _hyperlink(row.get("gmail_link", "")),
        "",  # Q: HR Status (HR fills in)
        "",  # R: HR Notes (HR fills in)
        prior_flag,  # S: Prior Rejection (🚩 flag for duplicate-rejected applicants)
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A:S",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()


def append_indeed_queue(svc, sheet_id, tab, row):
    """Append a row to the Indeed Queue tab. Bot calls this for each
    Indeed candidate in addition to the regular Candidates write.

    Columns: Candidate Name | Position | Fit Quality | AI Recommendation |
    HR Status (formula) | Closed (checkbox FALSE default) | Timestamp
    (join key).

    HR Status column gets a per-row VLOOKUP formula that pulls the HR
    Status cell from the matching Candidates row by timestamp. Edits to
    HR Status on Candidates therefore propagate here automatically with
    no further bot intervention.
    """
    timestamp = row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    candidate_name = _safe(row.get("candidate_name") or "")
    position = _safe(row.get("applied_for") or "")
    decision = row.get("decision", "")
    fit_quality = _INDEED_FIT_QUALITY.get(decision, decision)
    ai_rec = _INDEED_AI_RECOMMENDATION.get(decision, "Review manually")
    # VLOOKUP by timestamp -> HR Status column on Candidates.
    # Candidates layout (post 2026-06-18 restructure):
    # A=Timestamp ... Q=HR Status (column 17 in A:Q).
    hr_status_formula = (
        f'=IFERROR(VLOOKUP("{timestamp}",Candidates!A:Q,17,FALSE),"")'
    )
    values = [
        candidate_name, position, _safe(fit_quality), _safe(ai_rec),
        hr_status_formula, False, timestamp,
    ]
    try:
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab}!A:G",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
    except Exception as e:
        log.warning("Failed to append to %s tab: %s", tab, e)


# ============================================================
# Bot Learning Log -- audit feedback for scorer few-shot context
# ============================================================
#
# Weekly audit (src/audit.py) writes rows here when the scorer's
# decision disagrees with HR's eventual outcome (and HR left notes
# explaining why). Each bot run loads the most recent N approved
# rows from this tab and passes them to scorer.score() so the model
# can see real corrections from HR. HR can flip "Approve as Training"
# to FALSE on any row to remove it from the few-shot context.

LEARNING_LOG_HEADERS = [
    "Audit Date", "Approve as Training", "Candidate Name", "Position",
    "Bot Decision", "HR Outcome", "HR Notes", "AI Reasoning",
    "Resume Excerpt", "Gmail Thread Link", "Original Timestamp",
]


def ensure_learning_log_headers(svc, sheet_id, tab):
    """Idempotently create the Bot Learning Log tab + header row."""
    if not _ensure_tab_exists(svc, sheet_id, tab):
        return
    rng = f"{tab}!A1:K1"
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=rng,
        ).execute()
    except Exception as e:
        log.warning("Could not read %s headers: %s", tab, e)
        return
    if resp.get("values"):
        return
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=rng, valueInputOption="RAW",
            body={"values": [LEARNING_LOG_HEADERS]},
        ).execute()
    except Exception as e:
        log.warning("Could not write %s headers: %s", tab, e)


def append_learning_entry(svc, sheet_id, tab, row):
    """Append a learning entry (one row of bot/HR disagreement)."""
    try:
        values = [
            row.get("audit_date") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            True,  # Approve as Training -- default on; HR un-checks to exclude.
            _safe(row.get("candidate_name", "")),
            _safe(row.get("position", "")),
            _safe(row.get("bot_decision", "")),
            _safe(row.get("hr_outcome", "")),
            _safe(row.get("hr_notes", "")),
            _safe(row.get("ai_reasoning", "")),
            _safe(row.get("resume_excerpt", "")),
            _hyperlink(row.get("gmail_link", "")),
            _safe(row.get("original_timestamp", "")),
        ]
        svc.spreadsheets().values().append(
            spreadsheetId=sheet_id,
            range=f"{tab}!A:K",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [values]},
        ).execute()
    except Exception as e:
        log.warning("Failed to append to %s tab: %s", tab, e)


def load_learning_examples(svc, sheet_id, tab, max_examples=8):
    """Read approved learning entries from the Bot Learning Log.

    Returns a list of dicts (most-recent first) with keys:
    position, bot_decision, hr_outcome, hr_notes, resume_excerpt,
    ai_reasoning. Quietly returns [] if the tab doesn't exist or
    has nothing approved -- the bot then runs without few-shot
    context (its current behavior pre-audit-shipment).
    """
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!A2:K",
        ).execute()
    except Exception as e:
        log.warning("Could not load %s tab (may not exist yet): %s", tab, e)
        return []
    rows = resp.get("values", []) or []
    out = []
    # Walk newest-first (rows arrive in insertion order; reverse).
    for r in reversed(rows):
        r = (r + [""] * 11)[:11]
        approve_raw = str(r[1]).strip().lower()
        if approve_raw not in {"true", "yes", "y", "1", "x"}:
            continue
        out.append({
            "position": r[3],
            "bot_decision": r[4],
            "hr_outcome": r[5],
            "hr_notes": r[6],
            "ai_reasoning": r[7],
            "resume_excerpt": r[8],
        })
        if len(out) >= max_examples:
            break
    return out


def load_recent_hidden_candidates(svc, sheet_id, tab, days_back=7,
                                  terminal_statuses=None):
    """Audit helper: read all Candidates rows whose Timestamp is in the
    past N days AND whose HR Status is a terminal value (Hired, Rejected,
    etc. -- caller supplies the set). Used by audit.py to find recently
    actioned candidates.

    Returns a list of dicts with keys matching the Candidates layout
    fields the audit cares about: timestamp, candidate_name,
    application_submitted, applied_for, decision, ai_reasoning,
    gmail_link, hr_status, hr_notes.
    """
    if terminal_statuses is None:
        terminal_statuses = {"Hired", "Rejected", "Not a fit",
                             "Closed", "Withdrawn", "Declined"}
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!A2:R",
        ).execute()
    except Exception as e:
        log.warning("Could not load %s for audit: %s", tab, e)
        return []
    rows = resp.get("values", []) or []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    out = []
    for r in rows:
        r = (r + [""] * 18)[:18]
        ts_str = r[0]
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts < cutoff:
            continue
        hr_status = (r[16] or "").strip()
        if hr_status not in terminal_statuses:
            continue
        out.append({
            "timestamp": ts_str,
            "candidate_name": r[1],
            "application_submitted": r[4],
            "filename": r[5],
            "applied_for": r[6],
            "decision": r[9],
            "confidence": r[12],
            "ai_reasoning": r[13],
            "gmail_link": r[15],
            "hr_status": hr_status,
            "hr_notes": (r[17] or "").strip(),
        })
    return out
