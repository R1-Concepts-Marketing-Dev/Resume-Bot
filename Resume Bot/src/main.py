"""Orchestrator. Runs once per GitHub Actions invocation:

  1. Build Google API clients (service account impersonating jobs@).
  2. Load active filters from the Filters tab in the Google Sheet.
     (Falls back to filters.yaml if the Sheet tab is empty.)
  3. Find Gmail messages with attachments that aren't labeled "processed".
  4. For each message:
       - download each PDF/DOCX attachment
       - extract text (with OCR fallback for scanned PDFs)
       - score with Claude
       - upload original file to the appropriate Drive folder
       - append a row to the Candidates dashboard
       - apply the "processed" Gmail label

The "processed" label is the bot's only state — re-runs are idempotent.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys

import yaml

from . import config, drive_client, gmail_client, google_auth, resume_parser, scorer, sheets_client


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("resume-bot")


BUCKET_TO_FOLDER = {
    "qualified": "folder_qualified",
    "not_qualified": "folder_not_qualified",
    "needs_review": "folder_review",
}


def _load_seed_filters() -> list[sheets_client.Filter]:
    """Load filters.yaml from repo root as a fallback."""
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    yaml_path = repo_root / "filters.yaml"
    if not yaml_path.exists():
        return []
    data = yaml.safe_load(yaml_path.read_text())
    return [
        sheets_client.Filter(
            role=f["role"],
            requirement=f["requirement"],
            job_hopping=f.get("job_hopping", ""),
            active=f.get("active", True),
        )
        for f in (data.get("filters") or [])
        if f.get("active", True)
    ]


def run() -> int:
    cfg = config.load()
    log.info("Booting resume bot. Gmail user=%s, sheet=%s", cfg.gmail_user, cfg.sheet_id)

    gmail = google_auth.gmail(cfg.service_account_info, cfg.gmail_user)
    drive = google_auth.drive(cfg.service_account_info, cfg.gmail_user)
    sheets = google_auth.sheets(cfg.service_account_info, cfg.gmail_user)

    # Filters: prefer Sheet (HR-editable), fall back to YAML seed.
    sheets_client.ensure_dashboard_headers(sheets, cfg.sheet_id, cfg.dashboard_tab)
    filters = sheets_client.load_filters(sheets, cfg.sheet_id, cfg.filters_tab)
    if not filters:
        log.warning("Filters tab is empty — using filters.yaml as seed.")
        filters = _load_seed_filters()
    if not filters:
        log.error("No active filters found. Aborting.")
        return 2
    log.info("Loaded %d active filter(s).", len(filters))

    # Ensure the "processed" Gmail label exists, capture its ID.
    label_id = gmail_client.ensure_label(gmail, cfg.gmail_user, cfg.processed_label)

    msg_ids = gmail_client.list_unprocessed(
        gmail, cfg.gmail_user, cfg.processed_label, cfg.max_messages_per_run
    )
    if not msg_ids:
        log.info("No new messages with attachments. Nothing to do.")
        return 0
    log.info("Found %d unprocessed message(s).", len(msg_ids))

    handled = 0
    errors = 0
    for msg_id in msg_ids:
        try:
            handled += _handle_one(cfg, gmail, drive, sheets, filters, msg_id, label_id)
        except Exception:
            log.exception("Failed to handle message %s — leaving unlabeled for retry next run.", msg_id)
            errors += 1

    log.info("Run complete. Attachments scored: %d. Message-level errors: %d.", handled, errors)
    return 0 if errors == 0 else 1


def _handle_one(cfg, gmail, drive, sheets, filters, msg_id, label_id) -> int:
    msg = gmail_client.fetch(gmail, cfg.gmail_user, msg_id)
    if not msg.attachments:
        # Nothing to score, but still mark processed so we don't re-scan.
        gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
        return 0

    log.info("msg=%s subj=%r attachments=%d", msg_id, msg.subject[:60], len(msg.attachments))

    scored = 0
    for att in msg.attachments:
        text, used_ocr = resume_parser.extract(att.filename, att.mime_type, att.data)
        result = scorer.score(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            resume_text=text,
            filters=filters,
            used_ocr=used_ocr,
        )

        bucket = result["bucket"]
        folder_attr = BUCKET_TO_FOLDER[bucket]
        folder_id = getattr(cfg, folder_attr)
        drive_link = drive_client.upload(
            drive, att.filename, att.data, att.mime_type or "application/pdf", folder_id
        )

        sheets_client.append_candidate(
            sheets, cfg.sheet_id, cfg.dashboard_tab,
            {
                "candidate_name": result["candidate_name"],
                "email": result["candidate_email"],
                "phone": result["candidate_phone"],
                "filename": att.filename,
                "best_fit_roles": result["best_fit_roles"],
                "decision": bucket,
                "years_relevant_experience": result["years_relevant_experience"],
                "job_hopping_flag": result["job_hopping_flag"],
                "confidence": result["confidence"],
                "reasoning": result["reasoning"],
                "drive_link": drive_link,
                "gmail_link": msg.thread_link,
            },
        )
        scored += 1
        log.info(
            "  → %s | %s | conf=%.2f | %s",
            att.filename, bucket, result["confidence"], result["candidate_name"],
        )

    # All attachments handled successfully → mark the message processed.
    gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
    return scored


if __name__ == "__main__":
    sys.exit(run())
