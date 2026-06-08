"""One-shot migration for the Needs Human tab.

Reads existing Needs Human rows (old schema), backs them up to a local
JSON file, dedupes by Gmail thread_id (keeping the earliest entry per
thread), rewrites the tab using the new schema, then applies the new
formatting (column widths, freeze, dropdowns, conditional formatting).

Run once from the repo root:

    python -m scripts.migrate_needs_human

Requires the same env vars the bot needs (GOOGLE_OAUTH_* + SHEET_ID +
NEEDS_HUMAN_TAB_NAME if you've overridden it). Loads them from a
.env-style file if present.

Safe to re-run: if the tab is already in the new schema, the script
detects it and exits without changes.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

from src import config, google_auth, sheets_client

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO,
)
log = logging.getLogger("migrate_needs_human")

# Old schema (what's on disk before this script runs):
#   A Timestamp | B Sender | C Subject | D Body Preview | E Has Attachment
#   F Why Flagged | G Bot Best Guess | H Confidence | I Gmail Thread Link | J Reviewed
OLD_HEADERS = [
    "Timestamp", "Sender", "Subject", "Body Preview", "Has Attachment",
    "Why Flagged", "Bot Best Guess", "Confidence",
    "Gmail Thread Link", "Reviewed",
]

_THREAD_ID_PATTERN = re.compile(r"#inbox/([A-Za-z0-9_-]+)")


def _thread_id_from_link(cell: str) -> str | None:
    if not cell:
        return None
    m = _THREAD_ID_PATTERN.search(str(cell))
    return m.group(1) if m else None


def _guess_reason_type(why_flagged: str) -> str:
    """Map old free-text 'Why Flagged' to one of the new reason_type
    enum values. Falls back to 'manual' if it doesn't match a known
    pattern."""
    s = (why_flagged or "").lower()
    if "loop" in s:
        return "loop"
    if "low confidence" in s or "confidence" in s and "<" in s:
        return "low_confidence"
    if "indeed" in s:
        return "indeed_fetch"
    return "manual"


def _format_pt_timestamp(iso_str: str) -> str:
    """Mirror sheets_client._format_pt_timestamp so the migrated rows
    use the same format as new rows."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except ValueError:
        return iso_str  # keep raw value if unparseable
    from datetime import timedelta
    pt = dt - timedelta(hours=7)
    return pt.strftime("%Y-%m-%d %H:%M PT")


def _parse_old_iso(iso_str: str) -> datetime:
    """Parse old ISO timestamp for sorting (earliest-wins dedup).
    Falls back to epoch on parse failure so unparseable rows sort last."""
    try:
        return datetime.fromisoformat((iso_str or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def _clip(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:n - 1].rstrip() + "…" if len(s) > n else s


def migrate(cfg: config.Config, dry_run: bool = False) -> int:
    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token
    )
    svc = google_auth.sheets(creds)

    tab = cfg.needs_human_tab
    log.info("Migrating Needs Human tab %r in spreadsheet %s", tab, cfg.sheet_id)

    # 1. Pull all existing rows (use FORMULA to preserve HYPERLINK URLs)
    try:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=cfg.sheet_id,
            range=f"{tab}!A:J",
            valueRenderOption="FORMULA",
        ).execute()
    except Exception as e:
        log.error("Failed to read tab: %s", e)
        return 2

    rows = resp.get("values", []) or []
    if not rows:
        log.info("Tab is empty -- nothing to migrate. Applying new schema headers.")
        if not dry_run:
            sheets_client.ensure_needs_human_headers(svc, cfg.sheet_id, tab)
        return 0

    header_row = rows[0] if rows else []
    data_rows = rows[1:] if len(rows) > 1 else []

    if header_row == sheets_client.NEEDS_HUMAN_HEADERS:
        log.info("Tab is already in the new schema. No migration needed.")
        # Still re-apply formatting in case it drifted.
        if not dry_run:
            sheets_client.ensure_needs_human_headers(svc, cfg.sheet_id, tab)
        return 0

    if header_row != OLD_HEADERS:
        log.warning(
            "Unexpected header row -- not migrating. Expected old schema %r OR new schema %r, got: %r",
            OLD_HEADERS, sheets_client.NEEDS_HUMAN_HEADERS, header_row,
        )
        return 3

    log.info("Found %d data rows to dedupe", len(data_rows))

    # 2. Back up raw data to JSON so we can roll back if needed
    backup_path = Path(f"needs_human_backup_{int(datetime.now(timezone.utc).timestamp())}.json")
    backup_path.write_text(json.dumps({
        "header_row": header_row,
        "data_rows": data_rows,
        "backed_up_at": datetime.now(timezone.utc).isoformat(),
        "spreadsheet_id": cfg.sheet_id,
        "tab": tab,
    }, indent=2))
    log.info("Backed up raw data to %s", backup_path.resolve())

    # 3. Dedupe: keep earliest-timestamp row per thread_id
    by_thread: OrderedDict[str, list] = OrderedDict()
    no_thread_rows: list[list] = []
    for row in data_rows:
        # Pad short rows
        padded = list(row) + [""] * (len(OLD_HEADERS) - len(row))
        link = padded[8]  # column I = Gmail Thread Link
        tid = _thread_id_from_link(link)
        if not tid:
            no_thread_rows.append(padded)
            continue
        existing = by_thread.get(tid)
        if existing is None:
            by_thread[tid] = padded
            continue
        # Keep the earlier of the two
        if _parse_old_iso(padded[0]) < _parse_old_iso(existing[0]):
            by_thread[tid] = padded

    log.info(
        "Deduped %d rows -> %d unique threads + %d rows with no parseable thread_id",
        len(data_rows), len(by_thread), len(no_thread_rows),
    )

    # 4. Transform old rows -> new schema
    # New: Timestamp | Status | Reason Type | Why Flagged | Sender | Subject |
    #      Body Preview | Bot Guess | Confidence | Gmail Thread
    new_rows = []
    for row in list(by_thread.values()) + no_thread_rows:
        old_ts, old_sender, old_subj, old_body, _has_att, old_reason, \
            old_bot_guess, old_conf, old_link, old_reviewed = row[:10]
        status = "Open"
        # If HR had typed anything in the old Reviewed column, treat as Resolved
        if old_reviewed and old_reviewed.strip():
            status = "Resolved"
        reason_type = _guess_reason_type(old_reason)
        new_rows.append([
            _format_pt_timestamp(old_ts),
            status,
            reason_type,
            old_reason,
            old_sender,
            old_subj,
            _clip(old_body, 150),
            old_bot_guess,
            old_conf,
            old_link,  # preserve the HYPERLINK formula
        ])

    log.info("Built %d new rows in the new schema", len(new_rows))

    if dry_run:
        log.info("DRY RUN -- not writing changes. First 3 new rows:")
        for r in new_rows[:3]:
            log.info("  %s", r)
        return 0

    # 5. Clear the tab, write new headers + new rows
    log.info("Clearing %s and writing new schema...", tab)
    svc.spreadsheets().values().clear(
        spreadsheetId=cfg.sheet_id,
        range=f"{tab}!A:J",
        body={},
    ).execute()

    # Write headers + data in one batch
    svc.spreadsheets().values().update(
        spreadsheetId=cfg.sheet_id,
        range=f"{tab}!A1:J{1 + len(new_rows)}",
        valueInputOption="USER_ENTERED",
        body={"values": [sheets_client.NEEDS_HUMAN_HEADERS] + new_rows},
    ).execute()

    # 6. Apply the new formatting (column widths, freeze, validation, etc.)
    log.info("Applying new formatting to %s", tab)
    sheets_client.ensure_needs_human_headers(svc, cfg.sheet_id, tab)

    log.info(
        "DONE. Migrated %d old rows -> %d new rows. Backup at %s",
        len(data_rows), len(new_rows), backup_path.resolve(),
    )
    return 0


def main() -> int:
    # Allow --dry-run to preview without writing
    dry_run = "--dry-run" in sys.argv
    cfg = config.load()
    return migrate(cfg, dry_run=dry_run)


if __name__ == "__main__":
    sys.exit(main())
