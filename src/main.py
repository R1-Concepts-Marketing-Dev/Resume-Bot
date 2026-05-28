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


QUALIFIED_LEVELS = scorer.QUALIFIED_LEVELS


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

    if cfg.shadow_mode:
        log.info("SHADOW MODE on -- no Gmail labels, no archive, no auto-replies. Drive uploads + Sheet writes still happen.")
        label_id = ""
        outcome_label_ids = {}
        seen_thread_ids = sheets_client.load_processed_thread_ids(
            sheets, cfg.sheet_id, cfg.dashboard_tab
        )
        log.info("Loaded %d already-processed thread id(s) from Sheet.", len(seen_thread_ids))
        shadow_query = "in:inbox"
        after = gmail_client._format_after_date(cfg.bot_start_date)
        if after:
            shadow_query = f"{shadow_query} after:{after}"
        resp = gmail.users().messages().list(
            userId=cfg.gmail_user, q=shadow_query,
            maxResults=cfg.max_messages_per_run,
        ).execute()
        msg_ids = [m["id"] for m in resp.get("messages", [])]
    else:
        seen_thread_ids = set()
        label_id = gmail_client.ensure_label(gmail, cfg.gmail_user, cfg.processed_label)
        outcome_label_ids = gmail_client.ensure_outcome_labels(gmail, cfg.gmail_user)
        msg_ids = gmail_client.list_unprocessed(
            gmail, cfg.gmail_user, cfg.processed_label,
            cfg.max_messages_per_run, start_date=cfg.bot_start_date,
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
                outcome_label_ids, seen_thread_ids,
            )
        except Exception as e:
            log.exception("Failed to handle message %s.", msg_id)
            errors += 1
            sheets_client.append_error(sheets, cfg.sheet_id, cfg.errors_tab, {
                "msg_id": msg_id,
                "error_type": "uncaught",
                "detail": f"{type(e).__name__}: {e}",
                "bot_action": "skipped this email; continuing run",
            })

    log.info("Run complete. Attachments scored: %d. Errors: %d.", scored, errors)
    return 0 if errors == 0 else 1


def _handle_one(cfg, gmail, drive, sheets, all_filters, templates,
                active_role_names, paused_role_names, msg_id, label_id,
                outcome_label_ids, seen_thread_ids) -> int:
    msg = gmail_client.fetch(gmail, cfg.gmail_user, msg_id)
    log.info("msg=%s subj=%r from=%s attachments=%d",
             msg_id, msg.subject[:60], msg.sender_email, len(msg.attachments))

    if cfg.shadow_mode:
        gmail_client.mark_unread(gmail, cfg.gmail_user, msg_id)

    if cfg.shadow_mode and msg.thread_id in seen_thread_ids:
        log.info("  -> shadow mode: thread already in Sheet, skipping.")
        return 0

    if not msg.has_resume:
        # No resume attachment. Treat as a question / forgot-to-attach / etc.
        # In live mode, send the no_resume redirect template once per thread
        # and mark processed. In shadow mode, log what we WOULD have sent
        # but stay silent.
        if cfg.shadow_mode:
            would_send = "no_resume" if "no_resume" in templates else None
            log.info("  -> no resume; shadow: would_have_sent=%s", would_send)
            return 0
        if "no_resume" in templates and msg.sender_email:
            _send_template(
                gmail, cfg, templates["no_resume"], msg,
                vars_extra={"applicant_name": msg.sender_name or "there"},
            )
            log.info("  -> no resume; sent 'no_resume' redirect to %s", msg.sender_email)
        else:
            log.info("  -> no resume; no template or no sender; not replying.")
        gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
        return 0

    scored = 0
    last_bucket = None
    for att in msg.attachments:
        text, used_ocr = resume_parser.extract(att.filename, att.mime_type, att.data)

        if not text.strip():
            log.warning("  -> could not extract text from %s; labeling Unreadable",
                        att.filename)
            last_bucket = "unreadable"
            sheets_client.append_error(sheets, cfg.sheet_id, cfg.errors_tab, {
                "msg_id": msg_id,
                "sender_email": msg.sender_email,
                "filename": att.filename,
                "error_type": "parse_failed",
                "detail": f"resume_parser.extract returned empty text for mime={att.mime_type!r}",
                "bot_action": "labeled Unreadable",
                "gmail_link": msg.thread_link,
            })
            continue

        result = scorer.score(
            api_key=cfg.anthropic_api_key,
            model=cfg.anthropic_model,
            resume_text=text,
            filters=all_filters,
            email_subject=msg.subject,
            email_body=msg.body_text,
            used_ocr=used_ocr,
        )

        try:
            _conf = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            _conf = 0.0
        if _conf == 0.0:
            sheets_client.append_error(sheets, cfg.sheet_id, cfg.errors_tab, {
                "msg_id": msg_id,
                "sender_email": msg.sender_email,
                "filename": att.filename,
                "error_type": "scorer_failed",
                "detail": str(result.get("reasoning", ""))[:500],
                "bot_action": "needs_review (scorer fallback)",
                "gmail_link": msg.thread_link,
            })

        qualifying = [r for r in result["best_fit_roles"] if r["fit_level"] in QUALIFIED_LEVELS]
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

        last_bucket = _better_bucket(last_bucket, bucket)

        tag_roles = active_matches or paused_matches or qualifying
        tag = _sanitize_filename_tag([r["role"] for r in tag_roles])
        tagged_name = f"{tag}{att.filename}" if tag else att.filename
        try:
            drive_link = drive_client.upload(
                drive, tagged_name, att.data, att.mime_type or "application/pdf", folder_id
            )
        except Exception as e:
            log.exception("Drive upload failed for %s", att.filename)
            drive_link = ""
            sheets_client.append_error(sheets, cfg.sheet_id, cfg.errors_tab, {
                "msg_id": msg_id,
                "sender_email": msg.sender_email,
                "filename": att.filename,
                "error_type": "drive_failed",
                "detail": f"{type(e).__name__}: {e}",
                "bot_action": "row written, drive link empty",
                "gmail_link": msg.thread_link,
            })

        best_fit_with_scores = [f"{r['role']} ({r['fit_level']})" for r in result["best_fit_roles"]]

        applied_for = result.get("applied_for_role", "unspecified") or "unspecified"
        top_active = active_matches[0]["role"] if active_matches else ""
        cross_fit_flag = (
            "Yes"
            if (applied_for != "unspecified" and top_active and applied_for != top_active)
            else "No"
        )

        sheets_client.append_candidate(
            sheets, cfg.sheet_id, cfg.dashboard_tab,
            {
                "candidate_name": result["candidate_name"],
                "email": result["candidate_email"] or msg.sender_email,
                "phone": result["candidate_phone"],
                "filename": att.filename,
                "applied_for": applied_for,
                "best_fit_with_scores": best_fit_with_scores,
                "cross_fit_flag": cross_fit_flag,
                "decision": bucket,
                "years_relevant_experience": result["years_relevant_experience"],
                "job_hopping_flag": result["job_hopping_flag"],
                "confidence": result["confidence"],
                "reasoning": result["reasoning"],
                "drive_link": drive_link,
                "gmail_link": msg.thread_link,
            },
        )

        if cfg.shadow_mode:
            would_have_sent = template_key if template_key in templates else None
            log.info("  -> %s | conf=%.2f | shadow: would_have_sent=%s",
                     bucket, result["confidence"], would_have_sent)
        elif template_key and template_key in templates and msg.sender_email:
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

    final_bucket = last_bucket or "unreadable"
    if cfg.shadow_mode:
        log.info("  -> email outcome=%s, shadow mode (no Gmail label changes)", final_bucket)
        return scored

    outcome_id = outcome_label_ids.get(final_bucket)
    if outcome_id:
        gmail_client.archive_with_outcome(
            gmail, cfg.gmail_user, msg_id,
            processed_label_id=label_id,
            outcome_label_id=outcome_id,
        )
    else:
        gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
        log.warning("  -> no outcome label found for bucket=%r; email left in inbox",
                    final_bucket)
    log.info("  -> email outcome=%s, archived", final_bucket)
    return scored


_BUCKET_PRIORITY = ("qualified", "pending_paused", "needs_review",
                    "not_qualified", "unreadable")


def _better_bucket(current, new):
    if current is None:
        return new
    if new is None:
        return current
    return min(current, new, key=lambda b: _BUCKET_PRIORITY.index(b)
               if b in _BUCKET_PRIORITY else len(_BUCKET_PRIORITY))


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
