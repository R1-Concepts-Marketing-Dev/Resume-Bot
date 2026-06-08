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
    "Timestamp", "Candidate Name", "Email", "Phone", "Original Filename",
    "Applied For", "Cross-Fit Match", "Cross-Fit Flag", "Decision",
    "Years Relevant Exp", "Job Hopping", "Confidence", "AI Reasoning",
    "Drive File Link", "Gmail Thread Link", "HR Status", "HR Notes",
    "Recruiter/Agency",
]


MISC_HEADERS = [
    "Timestamp", "Sender", "Subject", "Original Filename",
    "Why Not A Resume", "Gmail Thread Link",
]


INBOX_LOG_HEADERS = [
    "Timestamp", "Sender", "Subject", "Type", "Action Taken",
    "Has Attachment", "Gmail Thread Link",
]


# Queue for emails the bot routed to human review (low classifier
# confidence, loop detection, etc.). HR works this queue manually --
# bot does NOT auto-reply or archive these messages.
# NEW SCHEMA (2026-06): added Status + Reason Type, dropped Has Attachment.
# Migration script (scripts/migrate_needs_human.py) rewrites existing rows
# into this shape and dedupes by thread_id.
NEEDS_HUMAN_HEADERS = [
    "Timestamp",     # A - human-readable PT timestamp e.g. "2026-06-04 15:56 PT"
    "Status",        # B - dropdown: Open / Investigating / Resolved / Ignored
    "Reason Type",   # C - dropdown: loop / low_confidence / indeed_fetch / manual
    "Why Flagged",   # D - full reason text
    "Sender",        # E
    "Subject",       # F
    "Body Preview",  # G - quoted text stripped, capped at 150 chars
    "Bot Guess",     # H - classifier label or empty
    "Confidence",    # I - 0.0-1.0 or empty
    "Gmail Thread",  # J - HYPERLINK
]

# Status dropdown options. "Open" is the default for new flags.
NEEDS_HUMAN_STATUSES = ["Open", "Investigating", "Resolved", "Ignored"]
NEEDS_HUMAN_REASON_TYPES = ["loop", "low_confidence", "indeed_fetch", "manual"]
NEEDS_HUMAN_OPEN_STATUSES = {"Open", "Investigating", ""}


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
    """Ensure the Candidates dashboard has the full header row.

    On first run: writes all headers into A1:R1. On subsequent runs:
    checks whether the existing header row already covers every column we
    know about (DASHBOARD_HEADERS) and, if it's short, extends just the
    missing trailing cells. This means adding a new column (e.g.
    Recruiter/Agency) to an existing sheet is a no-op header refresh --
    existing data rows stay put, the new column appears at the end."""
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
        # Empty sheet -- write the full header row.
        end_col = _col_letter(len(DASHBOARD_HEADERS))
        write_rng = f"{tab}!A1:{end_col}1"
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=write_rng, valueInputOption="RAW",
            body={"values": [DASHBOARD_HEADERS]},
        ).execute()
        return
    if len(existing) >= len(DASHBOARD_HEADERS):
        # Already at or beyond expected width -- nothing to do.
        return
    # Existing sheet but missing trailing columns (likely a fresh deploy
    # that added new columns). Extend just the missing tail.
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
    """1 -> A, 26 -> Z, 27 -> AA. We only need single-letter range for
    the foreseeable future but handle two-letter just in case."""
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _ensure_tab_exists(svc, sheet_id, tab) -> bool:
    """Create the tab if it's missing. Returns True if the tab exists (or
    was just created), False if creation failed."""
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
    """Idempotently write headers to the Archive - Misc tab. Tolerates a
    missing tab -- swallows the API error so the run doesn't crash."""
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


def _get_sheet_id_for_tab(svc, spreadsheet_id, tab_name) -> int | None:
    """Return the integer sheetId (NOT the spreadsheet id) for a tab name.
    Needed for batchUpdate formatting requests which work on sheetId."""
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
    """Idempotently set up the Needs Human queue tab: create if missing,
    write headers, apply formatting (column widths, freeze row 1, Status
    dropdown, conditional formatting).

    Safe to call on every run -- only writes headers if blank, and the
    formatting batchUpdate is naturally idempotent (replaces dimension
    properties, refreshes validation, etc.)."""
    if not _ensure_tab_exists(svc, sheet_id, tab):
        return
    # 1. Headers
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
        # Old schema detected. Leave it alone -- the migration script handles
        # this case. Log a hint so the operator knows what's up.
        log.info(
            "%s headers don't match new schema -- run scripts/migrate_needs_human.py "
            "before relying on dedup. Existing headers: %s",
            tab, existing_headers,
        )
        # Don't apply formatting either; the migration script will set it up
        # cleanly after rewriting the data.
        return

    # 2. Formatting (only if headers were written or already correct)
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


# Pixel widths for each Needs Human column. Tuned so the most-important
# columns (Reason Type, Why Flagged, Sender, Subject) are readable at a
# glance and the auxiliary columns (Bot Guess, Confidence) stay narrow.
_NEEDS_HUMAN_COL_WIDTHS = {
    0: 140,   # A Timestamp
    1: 120,   # B Status
    2: 110,   # C Reason Type
    3: 280,   # D Why Flagged
    4: 220,   # E Sender
    5: 200,   # F Subject
    6: 360,   # G Body Preview
    7: 160,   # H Bot Guess
    8: 90,    # I Confidence
    9: 80,    # J Gmail Thread
}


def _build_needs_human_format_requests(inner_id: int) -> list:
    """Build the batchUpdate requests that style the Needs Human tab.
    Run on every ensure_* call so re-running heals any drift."""
    requests = []

    # Freeze row 1 (headers)
    requests.append({
        "updateSheetProperties": {
            "properties": {
                "sheetId": inner_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    })

    # Bold + background for header row
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

    # Column widths
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

    # Wrap text in Why Flagged + Body Preview columns
    for col_index in (3, 6):  # D Why Flagged, G Body Preview
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

    # Status column (B) -- dropdown validation
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

    # Reason Type column (C) -- dropdown validation
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

    # Conditional formatting: Resolved rows -> light grey, italic
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

    # Conditional formatting: Ignored -> very faint grey
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


def _format_pt_timestamp(iso_or_dt=None) -> str:
    """Format a UTC ISO string or datetime as 'YYYY-MM-DD HH:MM PT'.
    Accepts None (uses current time). PT here is UTC-7 with ~1h winter
    drift (PST), which is fine for human-readable display."""
    if iso_or_dt is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(iso_or_dt, str):
        try:
            dt = datetime.fromisoformat(iso_or_dt.replace("Z", "+00:00"))
        except ValueError:
            return iso_or_dt  # give up gracefully
    else:
        dt = iso_or_dt
    pt = dt - timedelta(hours=7)
    return pt.strftime("%Y-%m-%d %H:%M PT")


# Body previews are stored on the queue purely for at-a-glance triage --
# the full message is one click away via the Gmail Thread link.
_BODY_PREVIEW_MAX_CHARS = 150


def _clean_body_preview(body: str) -> str:
    """Strip quoted reply chains, collapse whitespace, cap length."""
    if not body:
        return ""
    try:
        # Reuse the bot's existing quote-stripper if available
        from . import gmail_client
        cleaned = gmail_client.strip_quoted_text(body)
    except Exception:
        cleaned = body
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > _BODY_PREVIEW_MAX_CHARS:
        cleaned = cleaned[:_BODY_PREVIEW_MAX_CHARS - 1].rstrip() + "\u2026"
    return cleaned


# Match Gmail thread URLs in HYPERLINK formulas:
#   =HYPERLINK("https://mail.google.com/mail/u/0/#inbox/<id>", "Link")
_THREAD_ID_PATTERN = re.compile(r"#inbox/([A-Za-z0-9_-]+)")


def _thread_id_from_link(cell_value: str) -> str | None:
    """Extract the Gmail thread_id from a HYPERLINK formula cell."""
    if not cell_value:
        return None
    m = _THREAD_ID_PATTERN.search(str(cell_value))
    return m.group(1) if m else None


def load_open_needs_human_threads(svc, sheet_id, tab) -> set[str]:
    """Return Gmail thread_ids currently sitting in the Needs Human queue
    with an open-ish status (Open / Investigating / blank). Used by
    main.py to suppress duplicate flag-attempts so a stuck conversation
    doesn't accumulate 30 identical rows."""
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
    threads: set[str] = set()
    for row in rows:
        # row indexes are relative to B2:J -- so col B = row[0] (Status),
        # col J = row[8] (Gmail Thread). Some rows may be short if
        # trailing columns are blank.
        status = (row[0] if len(row) > 0 else "").strip()
        if status and status not in NEEDS_HUMAN_OPEN_STATUSES:
            continue
        link_cell = row[8] if len(row) > 8 else ""
        tid = _thread_id_from_link(link_cell)
        if tid:
            threads.add(tid)
    return threads


def append_needs_human(svc, sheet_id, tab, row):
    """Append a row to the Needs Human queue using the new schema:
    Timestamp | Status | Reason Type | Why Flagged | Sender | Subject |
    Body Preview | Bot Guess | Confidence | Gmail Thread.

    Caller is expected to have already checked dedup via
    load_open_needs_human_threads(). Swallows its own exceptions so a
    missing tab can't break the run."""
    try:
        reason_type = (row.get("reason_type") or "manual").strip().lower()
        if reason_type not in NEEDS_HUMAN_REASON_TYPES:
            reason_type = "manual"
        timestamp = row.get("timestamp")
        values = [
            _format_pt_timestamp(timestamp),
            "Open",  # default status -- HR can change via dropdown
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


def load_known_candidate_emails(svc, sheet_id, tab) -> set[str]:
    """Set of emails already on the Candidates dashboard. Used to suppress
    auto-replies to candidates HR is already engaged with."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!C2:C",
        ).execute()
    except Exception as e:
        log.warning("Could not load known candidate emails: %s", e)
        return set()
    out: set[str] = set()
    for row in resp.get("values", []):
        if not row:
            continue
        val = str(row[0]).strip().lower()
        if val.startswith("'"):
            val = val[1:]
        if val and "@" in val:
            out.add(val)
    return out


def load_recent_inbox_senders(svc, sheet_id, tab,
                               hours_back: int = 24) -> dict:
    """Count messages per sender in the Inbox Log within the past N hours.
    Used by main.py to detect loops (same sender 3+ times in 24h ->
    escalate to Needs Human queue).

    Returns dict of {lower-cased sender email: count}. Tolerant: returns
    empty dict if the tab can't be read."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!A2:B",
        ).execute()
    except Exception as e:
        log.warning("Could not load Inbox Log for loop detection: %s", e)
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    counts: dict[str, int] = {}
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
        # Extract just the email address from "Name <email@domain>".
        import re
        m = re.search(r"[\w.+-]+@[\w.-]+", sender)
        if m:
            sender = m.group(0)
        if sender:
            counts[sender] = counts.get(sender, 0) + 1
    return counts


def load_processed_thread_ids(svc, sheet_id, tab) -> set[str]:
    import re
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!O2:O",
            valueRenderOption="FORMULA",
        ).execute()
    except Exception:
        return set()
    ids: set[str] = set()
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


def append_candidate(svc, sheet_id, tab, row):
    """Append a candidate row to the Candidates dashboard.

    Recruiter/Agency column (column R): if row['recruiter_agency'] is
    missing or empty, defaults to "N/A". When the scorer detects the
    email came from a third-party recruiter on the candidate's behalf,
    it populates this field with the agency or recruiter name."""
    recruiter_agency = (row.get("recruiter_agency") or "N/A").strip() or "N/A"
    values = [
        row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        _safe(row.get("candidate_name", "")),
        _safe(row.get("email", "")),
        _safe(row.get("phone", "")),
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
        "",  # HR Status (HR fills in)
        "",  # HR Notes (HR fills in)
        _safe(recruiter_agency),
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A:R",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()
