"""Weekly audit: read HR's recent decisions on the Candidates sheet, find
disagreements between the bot's call and HR's outcome, and write each
disagreement to the Bot Learning Log tab so the scorer can use them as
few-shot context on subsequent runs.

Runs on its own GitHub Actions cron schedule (see .github/workflows/audit.yml).
"""

from __future__ import annotations

import base64
import logging
import sys
from datetime import datetime, timezone

from . import config, google_auth, sheets_client


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("resume-bot-audit")


# Map HR Status values (column Q on Candidates) to a coarse outcome label
# that the scorer can reason about. Anything not listed is "uncategorized"
# and ignored by the audit.
HR_TERMINAL_STATUSES = {
    # Positive outcomes -- HR moved forward / hired
    "Hired":             "hired",
    "Move forward":      "moved_forward",
    "Interviewing":      "moved_forward",
    "Offer Extended":    "moved_forward",

    # Negative outcomes -- HR rejected
    "Rejected":          "rejected",
    "Not a fit":         "rejected",
    "Declined":          "rejected",

    # Neutral / withdrawn
    "Closed":            "closed",
    "Withdrawn":         "withdrawn",
}

# Bot decisions (column J on Candidates) that count as "the bot thought
# this candidate was a fit". When the bot says one of these and HR
# rejects/closes the candidate, that's a learning opportunity.
BOT_POSITIVE = {"qualified"}
BOT_NEGATIVE = {"not_qualified"}
BOT_NEUTRAL  = {"needs_review", "pending_paused"}


def _is_disagreement(bot_decision: str, hr_outcome: str) -> bool:
    """True if the bot's decision and HR's outcome contradict each other.

    Disagreement cases (each one is a learning signal):
      - bot=qualified, HR=rejected         -> bot was too generous
      - bot=qualified, HR=closed/withdrawn -> minor (candidate left)
      - bot=not_qualified, HR=hired        -> bot was too harsh
      - bot=not_qualified, HR=moved_forward -> bot was too harsh

    Not disagreements (skip):
      - bot=needs_review, HR=*             -> bot punted to human; no
                                              contradiction either way
      - bot=qualified, HR=hired            -> agreement (positive)
      - bot=not_qualified, HR=rejected     -> agreement (negative)
    """
    if not bot_decision or not hr_outcome:
        return False
    bd = bot_decision.lower()
    if bd in BOT_NEUTRAL:
        return False
    if bd in BOT_POSITIVE and hr_outcome in {"rejected"}:
        return True
    if bd in BOT_NEGATIVE and hr_outcome in {"hired", "moved_forward"}:
        return True
    return False


def _drive_resume_excerpt(drive_svc, drive_link: str, max_chars: int = 1200) -> str:
    """Best-effort: pull a chunk of the resume text from Drive for the
    learning entry. The Candidates row stores the Drive file link as
    a HYPERLINK formula; we extract the file ID, download via Drive
    export, and trim to max_chars. Silently returns "" if anything fails
    (the learning entry still has bot reasoning + HR notes, which are
    the most actionable parts -- the resume itself is supplementary).
    """
    if not drive_link:
        return ""
    # The cell stores =HYPERLINK("https://drive.google.com/.../file_id/...","Link")
    # so we may receive the URL string directly OR a formula. Pull file ID.
    import re
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", drive_link)
    if not m:
        return ""
    file_id = m.group(1)
    try:
        meta = drive_svc.files().get(fileId=file_id, fields="mimeType").execute()
        mime = meta.get("mimeType", "")
    except Exception as e:
        log.warning("Could not load Drive metadata for %s: %s", file_id, e)
        return ""
    try:
        if mime == "application/pdf":
            data = drive_svc.files().get_media(fileId=file_id).execute()
        elif mime.startswith("application/vnd.openxmlformats") or mime == "application/msword":
            data = drive_svc.files().get_media(fileId=file_id).execute()
        else:
            return ""
    except Exception as e:
        log.warning("Could not download Drive file %s: %s", file_id, e)
        return ""
    # Just attempt a quick text extract; if it fails, fall back to empty.
    try:
        from . import resume_parser
        text, _ = resume_parser.extract("resume", mime, data)
        return text[:max_chars]
    except Exception as e:
        log.warning("resume_parser.extract failed during audit: %s", e)
        return ""


def run() -> int:
    cfg = config.load()
    log.info("Audit starting. Sheet=%s, dashboard=%s",
             cfg.sheet_id, cfg.dashboard_tab)

    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token
    )
    sheets = google_auth.sheets(creds)
    drive = google_auth.drive(creds)

    learning_log_tab = getattr(cfg, "learning_log_tab", "Bot Learning Log")
    sheets_client.ensure_learning_log_headers(sheets, cfg.sheet_id, learning_log_tab)

    days_back = int(getattr(cfg, "audit_window_days", 7))
    rows = sheets_client.load_recent_hidden_candidates(
        sheets, cfg.sheet_id, cfg.dashboard_tab,
        days_back=days_back,
        terminal_statuses=set(HR_TERMINAL_STATUSES.keys()),
    )
    log.info("Found %d candidates with terminal HR status in past %d day(s)",
             len(rows), days_back)

    # Dedup: skip rows already logged (match by original timestamp).
    existing_timestamps = _load_logged_timestamps(sheets, cfg.sheet_id, learning_log_tab)
    log.info("Already-logged: %d entries", len(existing_timestamps))

    written = 0
    skipped_agreement = 0
    skipped_no_notes = 0
    skipped_dupe = 0

    for row in rows:
        ts = row["timestamp"]
        if ts in existing_timestamps:
            skipped_dupe += 1
            continue

        hr_status = row["hr_status"]
        hr_outcome = HR_TERMINAL_STATUSES.get(hr_status, "")
        if not hr_outcome:
            continue

        if not _is_disagreement(row["decision"], hr_outcome):
            skipped_agreement += 1
            continue

        hr_notes = row.get("hr_notes", "").strip()
        if not hr_notes:
            # No HR explanation -> nothing to learn from. Skip.
            skipped_no_notes += 1
            continue

        # Optional: pull the resume excerpt from Drive for richer context.
        # Quietly degrades to empty if Drive read fails.
        excerpt = _drive_resume_excerpt(drive, row.get("gmail_link") or "")

        sheets_client.append_learning_entry(
            sheets, cfg.sheet_id, learning_log_tab,
            {
                "audit_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "candidate_name": row.get("candidate_name", ""),
                "position": row.get("applied_for", ""),
                "bot_decision": row.get("decision", ""),
                "hr_outcome": hr_status,
                "hr_notes": hr_notes,
                "ai_reasoning": row.get("ai_reasoning", ""),
                "resume_excerpt": excerpt,
                "gmail_link": row.get("gmail_link", ""),
                "original_timestamp": ts,
            },
        )
        written += 1

    log.info(
        "Audit complete. New learning entries written: %d (skipped: %d "
        "agreement, %d no-notes, %d already-logged)",
        written, skipped_agreement, skipped_no_notes, skipped_dupe,
    )
    return 0


def _load_logged_timestamps(svc, sheet_id, tab) -> set:
    """Return the set of original_timestamp values already in the
    Bot Learning Log so we don't log the same disagreement twice on
    successive weekly runs."""
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!K2:K",
        ).execute()
    except Exception as e:
        log.warning("Could not load existing learning timestamps: %s", e)
        return set()
    out = set()
    for r in resp.get("values", []) or []:
        if not r:
            continue
        val = str(r[0]).strip()
        if val:
            out.add(val)
    return out


if __name__ == "__main__":
    sys.exit(run())
