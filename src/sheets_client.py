"""Sheets operations: filters, templates, dashboard, errors."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

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
    "Applied For", "Best-Fit Roles & Scores", "Cross-Fit Flag", "Decision",
    "Years Relevant Exp", "Job Hopping", "Confidence", "AI Reasoning",
    "Drive File Link", "Gmail Thread Link", "HR Status", "HR Notes",
]


MISC_HEADERS = [
    "Timestamp", "Sender", "Subject", "Original Filename",
    "Why Not A Resume", "Gmail Thread Link",
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
    """Escape values that Sheets USER_ENTERED would parse as formulas/operators.
    Phone numbers starting with + are the most common culprit."""
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


def ensure_misc_headers(svc, sheet_id, tab):
    """Idempotently write headers to the Archive - Misc tab.

    Tolerates the tab not existing yet -- a missing tab raises a Sheets API
    400, which we swallow because the Apps Script side is expected to have
    created the tab. The next run will succeed once it's there."""
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


def append_misc(svc, sheet_id, tab, row):
    """Append a row to the Archive - Misc tab for emails the bot decided
    were not candidate resumes (newsletters, alerts, internal comms, etc.).
    Swallows its own exceptions so a missing tab doesn't break the run."""
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


def load_processed_thread_ids(svc, sheet_id, tab) -> set[str]:
    """Read column O (Gmail Thread Link) from the dashboard and return the
    set of Gmail thread IDs we've already logged.

    Uses valueRenderOption=FORMULA so HYPERLINK formula text is returned
    (instead of the displayed label "Link"). The thread ID is embedded
    in the URL inside the formula."""
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
    """Append a row to the Bot Errors tab. Swallows its own exceptions."""
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
    best_fit_str = ", ".join(row.get("best_fit_with_scores") or [])
    values = [
        row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        _safe(row.get("candidate_name", "")),
        _safe(row.get("email", "")),
        _safe(row.get("phone", "")),
        _safe(row.get("filename", "")),
        _safe(row.get("applied_for", "")),
        _safe(best_fit_str),
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
