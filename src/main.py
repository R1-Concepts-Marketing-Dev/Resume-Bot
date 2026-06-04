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
    sheets_client.ensure_misc_headers(sheets, cfg.sheet_id, cfg.misc_tab)
    sheets_client.ensure_inbox_log_headers(sheets, cfg.sheet_id, cfg.inbox_log_tab)

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

    if cfg.shadow_mode and msg.was_unread:
        gmail_client.mark_unread(gmail, cfg.gmail_user, msg_id)

    if cfg.shadow_mode and msg.thread_id in seen_thread_ids:
        log.info("  -> shadow mode: thread already in Sheet, skipping.")
        return 0

    # Helper: write one row to the Inbox Log audit tab. Defined up here so
    # the auto-response short-circuit below can use it before the resume-
    # branch logic is reached.
    def _log_inbox(email_type_label: str, action: str):
        sheets_client.append_inbox_log(
            sheets, cfg.sheet_id, cfg.inbox_log_tab,
            {
                "sender": msg.sender,
                "subject": msg.subject,
                "type": email_type_label,
                "action": action,
                "has_attachment": msg.has_resume,
                "gmail_link": msg.thread_link,
            },
        )

    # ----- Pre-filter #1: auto-reply / OOO / newsletter short-circuit -----
    # Header-based detection (Auto-Submitted, List-Unsubscribe, Precedence,
    # X-Autoreply, etc.). Replying to these is at best wasted, at worst a
    # ping-pong loop. Route to misc and skip the classifier call entirely.
    if msg.is_auto_response:
        log.info("  -> auto-response/bulk headers detected; skipping classifier, archiving as misc")
        sheets_client.append_misc(
            sheets, cfg.sheet_id, cfg.misc_tab,
            {
                "sender": msg.sender,
                "subject": msg.subject,
                "filename": "",
                "reasoning": "Auto-reply / OOO / newsletter headers present; bot never replies to these.",
                "gmail_link": msg.thread_link,
            },
        )
        _log_inbox("misc", "auto-response headers; archived to Misc, no reply")
        if not cfg.shadow_mode:
            gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
        return 0

    # ----- Pre-filter #2: thread-state introspection -----
    # Load the set of templates the bot has already sent in this thread.
    # Used below to enforce the "no-duplicate-template + outcome-terminal"
    # rule -- prevents replying with the same template twice or replying
    # after a terminal outcome (denied/paused_match) has already been sent.
    if cfg.shadow_mode:
        sent_in_thread: set[str] = set()
    else:
        sent_in_thread = gmail_client.get_sent_templates_in_thread(
            gmail, cfg.gmail_user, msg.thread_id,
        )
    if sent_in_thread:
        log.info("  -> thread already has bot templates: %s", sorted(sent_in_thread))

    def _can_send(template_key: str) -> tuple[bool, str]:
        """Return (allowed, reason_if_blocked) for sending template_key in
        this thread. Implements the no-duplicate + outcome-terminal rule."""
        if not template_key:
            return False, "no template selected"
        if template_key in sent_in_thread:
            return False, f"duplicate {template_key} (already sent in thread)"
        if sent_in_thread & gmail_client.TERMINAL_TEMPLATE_KEYS:
            terminal = sorted(sent_in_thread & gmail_client.TERMINAL_TEMPLATE_KEYS)
            return False, f"terminal template already sent in thread: {terminal}"
        return True, ""

    # ----- Pre-filter #3: classify the email into one of 4 buckets -----
    # Strip quoted/forwarded text first so the classifier only sees what
    # the current sender typed this turn, not previously-quoted bot output
    # or forwarded headers. Original msg.body_text stays intact for the
    # scorer (which benefits from the full context).
    classifier_body = gmail_client.strip_quoted_text(msg.body_text)
    if classifier_body != msg.body_text:
        log.info("  -> stripped quoted text for classifier (%d -> %d chars)",
                 len(msg.body_text), len(classifier_body))
    email_type = scorer.classify_inbound_email(
        api_key=cfg.anthropic_api_key,
        subject=msg.subject,
        body=classifier_body,
        sender_email=msg.sender_email,
        has_attachment=msg.has_resume,
    )
    log.info("  -> classifier: type=%s has_attachment=%s",
             email_type, msg.has_resume)

    # ----- Misc branch: not a candidate email at all -----
    if email_type == "misc":
        log.info("  -> misc (not candidate-related); logging to %s and skipping",
                 cfg.misc_tab)
        sheets_client.append_misc(
            sheets, cfg.sheet_id, cfg.misc_tab,
            {
                "sender": msg.sender,
                "subject": msg.subject,
                "filename": "",
                "reasoning": "Classifier flagged as non-candidate (newsletter/alert/internal/spam).",
                "gmail_link": msg.thread_link,
            },
        )
        _log_inbox("misc", "archived to Misc, no reply")
        if not cfg.shadow_mode:
            gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
        return 0

    # ----- No-attachment branches: question or application_no_resume -----
    if not msg.has_resume:
        if email_type == "question":
            template_key = "question"
            log_type = "question"
        else:
            template_key = "no_resume"
            log_type = "application_no_resume"
        log.info("  -> no attachment; %s -> template=%s", log_type, template_key)

        if cfg.shadow_mode:
            would_send = template_key if template_key in templates else None
            log.info("  -> shadow: would_have_sent=%s", would_send)
            _log_inbox(log_type, f"shadow: would_have_sent={would_send}")
            return 0

        # Gate: thread-state check (no duplicates, no replies after terminal)
        allowed, block_reason = _can_send(template_key)
        if not allowed:
            log.info("  -> %s suppressed: %s", template_key, block_reason)
            _log_inbox(log_type, f"suppressed ({block_reason})")
            gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
            return 0

        if template_key in templates and msg.sender_email:
            _send_template(
                gmail, cfg, templates[template_key], msg,
                vars_extra={"applicant_name": msg.sender_name or "there"},
            )
            log.info("  -> sent '%s' to %s", template_key, msg.sender_email)
            _log_inbox(log_type, f"replied with {template_key} template")
        else:
            log.info("  -> no template '%s' or no sender; not replying.", template_key)
            _log_inbox(log_type, "no template / no sender, no reply")
        gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)
        return 0

    # ----- Resume branch: has attachment, proceed to scoring -----
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

        # Not-a-resume diversion: skip Drive upload, skip auto-reply, skip
        # Candidates row. Log it to Archive - Misc and move on.
        if result["overall_decision"] == "not_a_resume":
            log.info("  -> not a resume; logging to %s, skipping Drive/reply",
                     cfg.misc_tab)
            sheets_client.append_misc(
                sheets, cfg.sheet_id, cfg.misc_tab,
                {
                    "sender": msg.sender,
                    "subject": msg.subject,
                    "filename": att.filename,
                    "reasoning": result.get("reasoning", ""),
                    "gmail_link": msg.thread_link,
                },
            )
            last_bucket = _better_bucket(last_bucket, "not_a_resume")
            scored += 1
            continue

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

        applied_for = result.get("applied_for_role", "unspecified") or "unspecified"
        top_active = active_matches[0]["role"] if active_matches else ""
        is_cross_fit = (
            applied_for != "unspecified"
            and top_active
            and applied_for != top_active
        )
        cross_fit_flag = "🚨" if is_cross_fit else ""

        # Cross-fit suppression: if the candidate has a strong fit for a
        # different ACTIVE role, never auto-send the rejection.
        if is_cross_fit and template_key == "denied":
            template_key = None
            log.info("  -> cross-fit detected; suppressing 'denied' template")

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

        if is_cross_fit and active_matches:
            top = active_matches[0]
            cross_fit_match = f"{top['role']} ({top['fit_level']})"
        elif applied_for.lower() == "unspecified" and result["best_fit_roles"]:
            top = result["best_fit_roles"][0]
            cross_fit_match = f"{top['role']} ({top['fit_level']})"
        else:
            cross_fit_match = ""

        sheets_client.append_candidate(
            sheets, cfg.sheet_id, cfg.dashboard_tab,
            {
                "candidate_name": result["candidate_name"],
                "email": result["candidate_email"] or msg.sender_email,
                "phone": result["candidate_phone"],
                "filename": att.filename,
                "applied_for": applied_for,
                "cross_fit_match": cross_fit_match,
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
            _log_inbox("resume", f"shadow: scored={bucket}, would_have_sent={would_have_sent}")
        elif template_key and template_key in templates and msg.sender_email:
            allowed, block_reason = _can_send(template_key)
            if not allowed:
                log.info("  -> %s | conf=%.2f | suppressed %s: %s",
                         bucket, result["confidence"], template_key, block_reason)
                _log_inbox("resume",
                           f"scored - {bucket}; reply suppressed ({block_reason})")
            else:
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
                # Track in-process so a second attachment in the same loop
                # won't try to send the same template again.
                sent_in_thread.add(template_key)
                log.info("  -> %s | conf=%.2f | sent '%s' to %s",
                         bucket, result["confidence"], template_key, msg.sender_email)
                _log_inbox("resume", f"scored - {bucket}; replied with {template_key}")
        else:
            log.info("  -> %s | conf=%.2f | no template / no email", bucket, result["confidence"])
            _log_inbox("resume", f"scored - {bucket}; no auto-reply")

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
                    "not_qualified", "unreadable", "not_a_resume")


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
        template_key=template.key,
    )


if __name__ == "__main__":
    sys.exit(run())
