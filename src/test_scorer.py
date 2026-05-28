"""Standalone test scorer - triggered manually via the 'Test scorer' GitHub
Actions workflow. Reads pasted resume text from env vars, scores it against
the current filters from the Sheet, and appends one row to a 'Test Results'
tab so HR can iterate on filter wording fast without sending real emails.

The tab is auto-created with headers on first run."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from . import config, google_auth, scorer, sheets_client


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("test-scorer")


TEST_TAB = "Test Results"
TEST_HEADERS = [[
    "Timestamp", "Test Name", "Decision", "Best-Fit Roles & Scores",
    "Confidence", "Years Relevant Exp", "AI Reasoning", "Email Subject",
    "Resume Preview",
]]


def ensure_test_tab(svc, sheet_id: str) -> None:
    """Create the Test Results tab if missing, then ensure its header row."""
    meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
    tab_exists = any(
        s.get("properties", {}).get("title") == TEST_TAB
        for s in meta.get("sheets", [])
    )

    if not tab_exists:
        log.info("Creating '%s' tab in the Sheet.", TEST_TAB)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": TEST_TAB}}}]},
        ).execute()

    head = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{TEST_TAB}!A1:I1"
    ).execute()
    if not head.get("values"):
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=f"{TEST_TAB}!A1:I1",
            valueInputOption="RAW", body={"values": TEST_HEADERS},
        ).execute()


def append_test_row(svc, sheet_id: str, values: list) -> None:
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=f"{TEST_TAB}!A:I",
        valueInputOption="RAW", insertDataOption="INSERT_ROWS",
        body={"values": [values]},
    ).execute()


def run() -> int:
    resume_text = os.environ.get("RESUME_TEXT", "").strip()
    if not resume_text:
        log.error("RESUME_TEXT is empty - paste resume text in the workflow input.")
        return 2

    email_subject = os.environ.get("EMAIL_SUBJECT", "")
    email_body = os.environ.get("EMAIL_BODY", "")
    test_name = (
        os.environ.get("TEST_NAME", "").strip()
        or resume_text.split("\n", 1)[0][:50]
        or "(unnamed test)"
    )

    cfg = config.load()
    log.info("Test scorer starting. Test name=%r", test_name)

    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token
    )
    sheets = google_auth.sheets(creds)

    ensure_test_tab(sheets, cfg.sheet_id)

    all_filters = sheets_client.load_filters(sheets, cfg.sheet_id, cfg.filters_tab)
    if not all_filters:
        log.error("No filters loaded from the Sheet - cannot score.")
        return 3
    log.info("Loaded %d filter(s) for scoring.", len(all_filters))

    result = scorer.score(
        api_key=cfg.anthropic_api_key,
        model=cfg.anthropic_model,
        resume_text=resume_text,
        filters=all_filters,
        email_subject=email_subject,
        email_body=email_body,
    )

    best_fit_with_scores = [
        f"{r['role']} ({r['fit_level']})" for r in result["best_fit_roles"]
    ]

    append_test_row(sheets, cfg.sheet_id, [
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        test_name,
        result["overall_decision"],
        ", ".join(best_fit_with_scores),
        result["confidence"],
        result["years_relevant_experience"],
        result["reasoning"],
        email_subject,
        resume_text[:200].replace("\n", " "),
    ])

    log.info(
        "Test complete. Decision=%s | conf=%.2f | roles=%s",
        result["overall_decision"], result["confidence"], best_fit_with_scores,
    )
    return 0


if __name__ == "__main__":
    sys.exit(run())
