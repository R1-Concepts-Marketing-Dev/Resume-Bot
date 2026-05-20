"""Sheets operations:
- Load active filters from the Filters tab (HR edits this via GitHub Pages UI).
- Append a row to the Candidates dashboard tab.

Expected Filters tab schema (row 1 is headers):
    A: Role            B: Minimum requirement   C: Job hopping rule   D: Active
"""

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


# Dashboard column order — keep in sync with README and the Sheet headers.
DASHBOARD_HEADERS = [
    "Timestamp",
    "Candidate Name",
    "Email",
    "Phone",
    "Original Filename",
    "Best-Fit Role(s)",
    "Decision",
    "Years Relevant Exp",
    "Job Hopping",
    "Confidence",
    "AI Reasoning",
    "Drive File Link",
    "Gmail Thread Link",
    "HR Status",
    "HR Notes",
]


def _is_truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in {"true", "yes", "y", "on", "1", "x", "✓", "✔"}


def load_filters(svc, sheet_id: str, tab: str) -> list[Filter]:
    """Read all rows from the Filters tab, skip header, return active filters."""
    rng = f"{tab}!A2:D"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=rng
    ).execute()
    rows = resp.get("values", [])
    out: list[Filter] = []
    for r in rows:
        # Pad short rows
        r = (r + [""] * 4)[:4]
        role, req, hop, active = r
        if not role.strip() or not req.strip():
            continue
        out.append(
            Filter(
                role=role.strip(),
                requirement=req.strip(),
                job_hopping=hop.strip() or "Average tenure > 1 year = positive",
                active=_is_truthy(active),
            )
        )
    return [f for f in out if f.active]


def ensure_dashboard_headers(svc, sheet_id: str, tab: str) -> None:
    """If row 1 of the dashboard is empty, write our header row."""
    rng = f"{tab}!A1:O1"
    resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=rng).execute()
    if resp.get("values"):
        return
    svc.spreadsheets().values().update(
        spreadsheetId=sheet_id,
        range=rng,
        valueInputOption="RAW",
        body={"values": [DASHBOARD_HEADERS]},
    ).execute()


def append_candidate(svc, sheet_id: str, tab: str, row: dict) -> None:
    """Append one candidate row. `row` keys match DASHBOARD_HEADERS (HR Status
    and HR Notes are left blank for HR to fill in)."""
    values = [
        row.get("timestamp") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        row.get("candidate_name", ""),
        row.get("email", ""),
        row.get("phone", ""),
        row.get("filename", ""),
        ", ".join(row.get("best_fit_roles") or []),
        row.get("decision", ""),
        row.get("years_relevant_experience", ""),
        row.get("job_hopping_flag", ""),
        row.get("confidence", ""),
        row.get("reasoning", ""),
        row.get("drive_link", ""),
        row.get("gmail_link", ""),
        "",  # HR Status
        "",  # HR Notes
    ]
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A:O",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()
