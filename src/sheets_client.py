"""Sheets operations: filters, templates, dashboard, errors."""

from __future__ import annotations

import logging
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
NEEDS_HUMAN_HEADERS = [
    "Timestamp", "Sender", "Subject", "Body Preview", "Has Attachment",
    "Why Flagged", "Bot Best Guess", "Confidence",
    "Gmail Thread Link", "Reviewed",
]


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
    rng = f"{tab}!A1:Q1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    if resp.get("values"):
        return
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=rng, valueInputOption="RAW",
        body={"values": [DASHBOARD_HEADERS]},
    ).execute()


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


def ensure_needs_human_headers(svc, sheet_id, tab):
    """Create the Needs Human queue tab if missing, then write headers if
    blank. This tab is auto-created (unlike Misc/Inbox Log which rely on
    Apps Script) so the bot can ship the feature without a sheet edit."""
    if not _ensure_tab_exists(svc, sheet_id, tab):
        return
    rng = f"{tab}!A1:J1"
    try:
        resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    except Exception as e:
        log.warning("Could not read %s headers: %s", tab, e)
        return
    if resp.get("values"):
        return
    try:
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=rng, valueInputOption="RAW",
            body={"values": [NEEDS_HUMAN_HEADERS]},
        ).execute()
    except Exception as e:
        log.warning("Could not write %s headers: %s", tab, e)


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


def append_needs_human(svc, sheet_id, tab, row):
    """Append a row to the Needs Human queue. Swallows its own
    exceptions so a missing tab can't break the run."""
    try:
        values = [
            row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            _safe(row.get("sender", "")),
            _safe(row.get("subject", "")),
            _safe(row.get("body_preview", "")),
            "yes" if row.get("has_attachment") else "no",
            _safe(row.get("reason", "")),
            _safe(row.get("bot_guess", "")),
            _safe(row.get("confidence", "")),
            _hyperlink(row.get("gmail_link", "")),
            "",  # Reviewed -- HR fills in
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
        "", "",
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A:Q",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()
