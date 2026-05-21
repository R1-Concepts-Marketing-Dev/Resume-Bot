"""Orchestrator. Runs once per GitHub Actions invocation."""

from __future__ import annotations

import logging
import pathlib
import re
import sys

import yaml

from . import config, drive_client, gmail_client, google_auth, resume_parser, scorer, sheets_client


logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("resume-bot")


QUALIFIED_THRESHOLD = 60


def _load_seed_filters() -> list[sheets_client.Filter]:
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
    ]


def _sanitize_filename_tag(roles: list[str]) -> str:
    if not roles:
        return ""
    cleaned = []
    for r in roles:
        r2 = re.sub(r'[\\/<>:|?*"]', "", r).strip()
        if len(r2) > 40:
            r2 = r2[:37] + "..."
        cleaned.append(r2)
    return f"[{', '.join(cleaned)}] "


def run() -> int:
    cfg = config.load()
    log.info("Booting resume bot. Gmail user=%s, sheet=%s", cfg.gmail_user, cfg.sheet_id)

    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token
    )
    gmail = google_auth.gmail(creds)
    drive = google_auth.drive(creds)
    sheets = google_auth.sheets(creds)

    sheets_client.ensure_dashboard_headers(sheets, cfg.sheet_id, cfg.dashboard_tab)
    sheets_client.ensure_templates_seeded(sheets, cfg.sheet_id, cfg.templates_tab)

    all_filters = sheets_client.load_filters(sheets, cfg.sheet_id, cfg.filters_tab)
    if not all_filters:
        log.warning("Filters tab is empty - using filters.yaml seed.")
        all_filters = _load_seed_filters()
    if not all_filters:
        log.error("No filters at all. Aborting.")
        return 2

    active_role_names = {f.role for f in all_filters if f.active}
    paused_role_names = {f.role for f in all_filters if not f.active}
    log.info("Loaded %d filter(s): %d active, %d paused.",
             len(all_filters), len(active_role_names), len(paused_role_names))

    templates = sheets_client.load_templates(sheets, cfg.sheet_id, cfg.templates_tab)
    log.info("Loaded %d active template(s): %s", len(templates), list(templates.keys()))

    label_id = gmail_client.ensure_label(gmail, cfg.gmail_user, cfg.processed_label)
    msg_ids = gmail_client.list_unprocessed(
        gmail, cfg.gmail_user, cfg.processed_label, cfg.max_messages_per_run
    )
    if not msg_ids:
        log.info("No unprocessed inbox messages. Nothing to do.")
        return 0
    log.info("Found %d unprocessed message(s).", len(msg_ids))

    scored = 0
    errors = 0
    for msg_id in msg_ids:
        try:
            scored += _handle_one(
                cfg, gmail, drive, sheets, all_filters, templates,
                active_role_names, paused_role_names, msg_id, label_id,
            )
        except Exception:
            log.exception("Failed to handle message %s.", msg_id)
            errors += 1

    log.info("Run complete. Attachments scored: %d. Errors: %d.", scored, errors)
    return 0 if errors == 0 else 1


def _handle_one(cfg, gmail, drive, sheets, all_filters, templates,
                active_role_names, paused_role_names, msg_id, label_id) -> int:
    msg = gmail_client.fetch(gmail, cfg.gmail_user, msg_id)
    log.info("msg=%s subj=%r from=%s attachments=%d",
             msg_id, msg.subject[:60], msg.sender_email, len(msg.attachments))

    if not msg.has_resume:
        if msg.sender_email and "no_resume" in templates:
            _send_template(
                gmail, cfg, templates["no_resume"], msg,
                vars_extra={"applicant_name": msg.sender_name or "there"},
            )
            log.info("  -> no resume; sent 'no_resume' reply to %s", msg.sender_email)
        else:
            log.info("  -> no resume / no sender / no template; skipped.")
        gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
        return 0

    scored = 0
    for att in msg.attachments:
        text, used_ocr = resume_parser.extract(att.filename, att.mime_type, att.data)
        result = scorer.score(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            resume_text=text,
            filters=all_filters,
            email_subject=msg.subject,
            email_body=msg.body_text,
            used_ocr=used_ocr,
        )

        qualifying = [r for r in result["best_fit_roles"] if r["fit_score"] >= QUALIFIED_THRESHOLD]
        active_matches = [r for r in qualifying if r["role"] in active_role_names]
        paused_matches = [r for r in qualifying if r["role"] in paused_role_names]

        if result["overall_decision"] == "needs_review":
            bucket = "needs_review"
            folder_id = cfg.folder_review
            template_key = None
        elif active_matches:
            bucket = "qualified"
            folder_id = cfg.folder_qualified
            template_key = None
        elif paused_matches:
            bucket = "pending_paused"
            folder_id = cfg.folder_pending
            template_key = "paused_match"
        else:
            bucket = "not_qualified"
            folder_id = cfg.folder_not_qualified
            template_key = "denied"

        tag_roles = active_matches or paused_matches or qualifying
        tag = _sanitize_filename_tag([r["role"] for r in tag_roles])
        tagged_name = f"{tag}{att.filename}" if tag else att.filename
        drive_link = drive_client.upload(
            drive, tagged_name, att.data, att.mime_type or "application/pdf", folder_id
        )

        best_fit_with_scores = [f"{r['role']} ({r['fit_score']})" for r in result["best_fit_roles"]]
        sheets_client.append_candidate(
            sheets, cfg.sheet_id, cfg.dashboard_tab,
            {
                "candidate_name": result["candidate_name"],
                "email": result["candidate_email"] or msg.sender_email,
                "phone": result["candidate_phone"],
                "filename": att.filename,
                "best_fit_with_scores": best_fit_with_scores,
                "decision": bucket,
                "years_relevant_experience": result["years_relevant_experience"],
                "job_hopping_flag": result["job_hopping_flag"],
                "confidence": result["confidence"],
                "reasoning": result["reasoning"],
                "drive_link": drive_link,
                "gmail_link": msg.thread_link,
            },
        )

        if template_key and template_key in templates and msg.sender_email:
            applicant_name = result["candidate_name"] or msg.sender_name or "there"
            primary_role = (paused_matches[0]["role"] if paused_matches
                            else (qualifying[0]["role"] if qualifying else ""))
            _send_template(
                gmail, cfg, templates[template_key], msg,
                vars_extra={
                    "applicant_name": applicant_name,
                    "role": primary_role,
                    "best_fit_roles": ", ".join([r["role"] for r in qualifying]),
                },
            )
            log.info("  -> %s | conf=%.2f | sent '%s' to %s",
                     bucket, result["confidence"], template_key, msg.sender_email)
        else:
            log.info("  -> %s | conf=%.2f | no email", bucket, result["confidence"])

        scored += 1

    gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
    return scored


def _send_template(gmail, cfg, template, msg, *, vars_extra: dict) -> None:
    vars_ = {"company_name": cfg.company_name, **vars_extra}
    subject, body = sheets_client.render_template(template, vars_)
    msg_full = gmail.users().messages().get(
        userId=cfg.gmail_user, id=msg.id, format="metadata",
        metadataHeaders=["Message-ID"],
    ).execute()
    in_reply_to = ""
    for h in msg_full.get("payload", {}).get("headers", []):
        if h["name"].lower() == "message-id":
            in_reply_to = h["value"]
            break
    gmail_client.send_reply(
        gmail, cfg.gmail_user,
        to=msg.sender_email,
        subject=subject,
        body=body,
        thread_id=msg.thread_id,
        in_reply_to_msg_id=in_reply_to,
    )


if __name__ == "__main__":
    sys.exit(run())
