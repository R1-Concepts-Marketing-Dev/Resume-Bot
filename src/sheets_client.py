"""Sheets operations: filters, templates, dashboard."""

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


SEED_TEMPLATES: list[Template] = [
    Template(
        key="no_resume",
        subject="Please resend with your resume attached",
        body=(
            "Hi {applicant_name},\n\n"
            "Thanks for reaching out about a position at {company_name}. "
            "We didn't see a resume attached to your message - could you "
            "reply with your resume as a PDF or Word attachment? Once we "
            "have it we'll get back to you.\n\n"
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


def load_processed_thread_ids(svc, sheet_id, tab) -> set[str]:
    """Read column O (Gmail Thread Link) from the dashboard and return the
    set of Gmail thread IDs we've already logged. Used by shadow mode to
    dedup: in shadow mode we can't apply a Gmail label, so the Sheet is
    the source of truth for what we've already processed."""
    import re
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{tab}!O2:O",
        ).execute()
    except Exception:
        return set()
    ids: set[str] = set()
    for row in resp.get("values", []):
        if not row:
            continue
        url = str(row[0])
        # Thread links look like https://mail.google.com/.../#inbox/<threadId>
        m = re.search(r"#inbox/([A-Za-z0-9]+)", url)
        if m:
            ids.add(m.group(1))
    return ids


def append_candidate(svc, sheet_id, tab, row):
    best_fit_str = ", ".join(row.get("best_fit_with_scores") or [])
    values = [
        row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        row.get("candidate_name", ""),
        row.get("email", ""),
        row.get("phone", ""),
        row.get("filename", ""),
        row.get("applied_for", ""),
        best_fit_str,
        row.get("cross_fit_flag", ""),
        row.get("decision", ""),
        row.get("years_relevant_experience", ""),
        row.get("job_hopping_flag", ""),
        row.get("confidence", ""),
        row.get("reasoning", ""),
        row.get("drive_link", ""),
        row.get("gmail_link", ""),
        "", "",
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A:Q",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()
