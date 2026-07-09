"""Weekly audit -- harvest HR-vs-bot disagreements into Bot Learning Log.

For each Candidates row where:
  - the bot's Decision + HR's HR Status point in opposite directions, AND
  - HR left HR Notes explaining why,
append a row to Bot Learning Log with Approve as Training = TRUE.

Skips rows already in the log (dedup by the row's original Candidates
timestamp, which we store in col K of Bot Learning Log). Idempotent.

Runs weekly via .github/workflows/audit.yml (Fri 4pm PT).

Downstream: each bot run reads up to 8 approved entries from Bot
Learning Log via sheets_client.load_learning_examples() and passes them
to scorer.score() as few-shot context.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from src import sheets_client

log = logging.getLogger("audit")


CANDIDATES_TAB_DEFAULT = "Candidates"
LEARNING_TAB_DEFAULT = "Bot Learning Log"

# HR statuses that mean HR moved the candidate FORWARD past the auto-archive
# gate. If the bot said not_qualified but HR ended up here, that's a
# valuable correction -- the bot was too strict.
HR_ENGAGED = {
    "In Review", "Contacted", "Interview Scheduled",
    "Offer Made", "On Hold", "Hired",
}
# HR statuses that mean HR closed the candidate NEGATIVELY. If the bot
# said qualified/needs_review/pending_paused but HR ended up here,
# that's the other flavor of correction -- the bot was too permissive.
HR_REJECTED = {"Rejected", "Not Interested", "Not Selected", "Not a fit"}

POSITIVE_BOT_DECISIONS = {"qualified", "needs_review", "pending_paused"}
NEGATIVE_BOT_DECISIONS = {"not_qualified"}


def _required(k):
    v = os.environ.get(k, "").strip()
    if not v:
        raise RuntimeError("missing " + k)
    return v


def _optional(k, d=""):
    return os.environ.get(k, d)


def build_creds():
    return Credentials(
        token=None,
        refresh_token=_required("GOOGLE_OAUTH_REFRESH_TOKEN"),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_required("GOOGLE_OAUTH_CLIENT_ID"),
        client_secret=_required("GOOGLE_OAUTH_CLIENT_SECRET"),
    )


def already_logged_timestamps(sheets, sheet_id, learning_tab):
    """Return the set of original_timestamps already in Bot Learning Log.

    Dedup key = the original Candidates row's Timestamp (col A), which
    we mirror into col K of the learning log.
    """
    try:
        resp = sheets.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{learning_tab}!K2:K",
        ).execute()
    except Exception as e:
        log.warning("Could not read learning log timestamps: %s", e)
        return set()
    out = set()
    for row in resp.get("values", []) or []:
        if row and row[0]:
            out.add(str(row[0]).strip())
    return out


def classify_disagreement(decision, hr_status):
    """Return 'over_permissive', 'over_strict', or None."""
    dec = (decision or "").strip().lower()
    hr = (hr_status or "").strip()
    if not hr or not dec:
        return None
    if dec in POSITIVE_BOT_DECISIONS and hr in HR_REJECTED:
        return "over_permissive"
    if dec in NEGATIVE_BOT_DECISIONS and hr in HR_ENGAGED:
        return "over_strict"
    return None


def _excerpt(text, limit=500):
    text = (text or "").strip().replace("\n", " ")
    if len(text) > limit:
        return text[:limit].rstrip() + " ..."
    return text


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    sheet_id = _required("SHEET_ID")
    dashboard_tab = _optional("DASHBOARD_TAB_NAME", CANDIDATES_TAB_DEFAULT)
    learning_tab = _optional("LEARNING_LOG_TAB_NAME", LEARNING_TAB_DEFAULT)

    creds = build_creds()
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)

    try:
        sheets_client.ensure_learning_log_headers(sheets, sheet_id, learning_tab)
    except Exception as e:
        log.warning("ensure_learning_log_headers: %s", e)

    seen = already_logged_timestamps(sheets, sheet_id, learning_tab)
    log.info("Bot Learning Log already contains %d entries", len(seen))

    resp = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=f"{dashboard_tab}!A2:T",
    ).execute()
    rows = resp.get("values", []) or []
    log.info("Scanning %d Candidates rows for disagreements", len(rows))

    over_perm = 0
    over_strict = 0
    added = 0
    skipped_no_notes = 0
    skipped_already_logged = 0
    errors = 0

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for r in rows:
        row = (r + [""] * 20)[:20]
        timestamp = str(row[0]).strip()          # A
        name = str(row[1]).strip()               # B
        position = str(row[6]).strip()           # G Applied For
        decision = str(row[9]).strip()           # J
        reasoning = str(row[14]).strip()         # O AI Reasoning
        hr_status = str(row[17]).strip()         # R
        hr_notes = str(row[18]).strip()          # S

        kind = classify_disagreement(decision, hr_status)
        if kind is None:
            continue
        if kind == "over_permissive":
            over_perm += 1
        else:
            over_strict += 1

        if not hr_notes:
            skipped_no_notes += 1
            continue
        if timestamp and timestamp in seen:
            skipped_already_logged += 1
            continue

        try:
            sheets_client.append_learning_entry(
                sheets, sheet_id, learning_tab, {
                    "audit_date": now,
                    "candidate_name": name,
                    "position": position,
                    "bot_decision": decision,
                    "hr_outcome": hr_status,
                    "hr_notes": _excerpt(hr_notes, 500),
                    "ai_reasoning": _excerpt(reasoning, 500),
                    "resume_excerpt": "",
                    "gmail_link": "",
                    "original_timestamp": timestamp,
                }
            )
            added += 1
            if timestamp:
                seen.add(timestamp)
        except Exception as e:
            errors += 1
            log.warning("append_learning_entry failed for %r (%s): %s",
                        name, timestamp, e)

    log.info(
        "Disagreements found: over_permissive=%d, over_strict=%d",
        over_perm, over_strict,
    )
    log.info(
        "Result: added=%d skipped_no_notes=%d skipped_already_logged=%d errors=%d",
        added, skipped_no_notes, skipped_already_logged, errors,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log.exception("audit crashed")
        sys.exit(2)
