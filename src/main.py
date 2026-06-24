"""Orchestrator. Runs once per GitHub Actions invocation."""

from __future__ import annotations

import logging
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone

import yaml

from . import config, drive_client, gmail_client, google_auth, indeed_fetcher, resume_parser, scorer, sheets_client


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


def _is_internal_sender(sender_email: str, internal_domains: tuple) -> bool:
    if not sender_email or "@" not in sender_email:
        return False
    domain = sender_email.rsplit("@", 1)[-1].strip().lower()
    return domain in internal_domains


def _is_blocklisted_sender(sender_email: str, blocklist: tuple) -> bool:
    if not sender_email or not blocklist:
        return False
    se = sender_email.strip().lower()
    if "@" not in se:
        return False
    domain = se.rsplit("@", 1)[-1]
    for entry in blocklist:
        e = entry.strip().lower().lstrip("@")
        if not e:
            continue
        if e == se:
            return True
        if e == domain:
            return True
    return False


_JOB_BOARD_DOMAINS = (
    "indeed.com",
    "indeedemail.com",
    "ziprecruiter.com",
    "glassdoor.com",
    "monster.com",
    "careerbuilder.com",
    "snagajob.com",
)


def _is_job_board_alias(email: str) -> bool:
    if not email or "@" not in email:
        return False
    domain = email.strip().lower().rsplit("@", 1)[-1]
    return any(domain == d or domain.endswith("." + d) for d in _JOB_BOARD_DOMAINS)


_EMAIL_REGEX = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _pick_candidate_email(scorer_email: str, fallback_sender: str,
                          resume_text: str = "", email_body: str = "") -> str:
    se = (scorer_email or "").strip()
    if se and not _is_job_board_alias(se):
        return se
    fb = (fallback_sender or "").strip()
    if fb and not _is_job_board_alias(fb):
        return fb
    for source in (resume_text, email_body):
        if not source:
            continue
        for match in _EMAIL_REGEX.findall(source):
            addr = match.strip().rstrip(".,;:")
            if addr and not _is_job_board_alias(addr):
                return addr
    return ""


def _is_business_hours(now_utc: datetime, start_pt: int, end_pt: int) -> bool:
    pt = now_utc - timedelta(hours=7)
    if pt.weekday() >= 5:
        return False
    return start_pt <= pt.hour < end_pt


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
    sheets_client.ensure_needs_human_headers(sheets, cfg.sheet_id, cfg.needs_human_tab)

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

    known_emails = sheets_client.load_known_candidate_emails(
        sheets, cfg.sheet_id, cfg.dashboard_tab,
    )
    log.info("Loaded %d known candidate email(s) for dup-reply suppression",
             len(known_emails))

    open_needs_human = sheets_client.load_open_needs_human_threads(
        sheets, cfg.sheet_id, cfg.needs_human_tab,
    )
    log.info("Loaded %d open Needs Human thread(s) for dedup",
             len(open_needs_human))

    recent_sender_counts = sheets_client.load_recent_inbox_senders(
        sheets, cfg.sheet_id, cfg.inbox_log_tab,
        hours_back=cfg.loop_window_hours,
    )
    log.info("Loaded loop-detection counts for %d sender(s) in past %dh",
             len(recent_sender_counts), cfg.loop_window_hours)

    learning_examples = sheets_client.load_learning_examples(
        sheets, cfg.sheet_id, getattr(cfg, "learning_log_tab", "Bot Learning Log"),
        max_examples=getattr(cfg, "learning_examples_max", 8),
    )
    log.info("Loaded %d learning example(s) for scorer few-shot",
             len(learning_examples))

    now_utc = datetime.now(timezone.utc)
    in_business_hours = _is_business_hours(
        now_utc, cfg.business_hours_start_pt, cfg.business_hours_end_pt,
    )
    log.info("Run at %s UTC; business hours Mon-Fri %d-%d PT -> in_window=%s",
             now_utc.isoformat(timespec="seconds"),
             cfg.business_hours_start_pt, cfg.business_hours_end_pt,
             in_business_hours)

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
        # Sweep: archive anything HR labeled "For HR/Closed" while triaging.
        # Runs every bot tick so HR's marks disappear from inbox within
        # 10 minutes without HR having to click archive on each thread.
        hr_closed_id = outcome_label_ids.get("hr_closed", "")
        if hr_closed_id:
            n_closed = gmail_client.archive_hr_closed_threads(
                gmail, cfg.gmail_user, hr_closed_id)
            if n_closed:
                log.info("Archived %d HR-closed thread(s) from inbox.", n_closed)
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
                outcome_label_ids, seen_thread_ids, known_emails,
                recent_sender_counts, in_business_hours,
                learning_examples=learning_examples,
                open_needs_human_threads=open_needs_human,
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
                outcome_label_ids, seen_thread_ids, known_emails,
                recent_sender_counts, in_business_hours,
                *, learning_examples: list | None = None,
                open_needs_human_threads: set | None = None) -> int:
    msg = gmail_client.fetch(gmail, cfg.gmail_user, msg_id)
    log.info("msg=%s subj=%r from=%s attachments=%d",
             msg_id, msg.subject[:60], msg.sender_email, len(msg.attachments))

    if cfg.shadow_mode and msg.was_unread:
        gmail_client.mark_unread(gmail, cfg.gmail_user, msg_id)

    if cfg.shadow_mode and msg.thread_id in seen_thread_ids:
        log.info("  -> shadow mode: thread already in Sheet, skipping.")
        return 0

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

    def _archive_as_misc(reasoning: str, log_action: str):
        sheets_client.append_misc(
            sheets, cfg.sheet_id, cfg.misc_tab,
            {
                "sender": msg.sender,
                "subject": msg.subject,
                "filename": "",
                "reasoning": reasoning,
                "gmail_link": msg.thread_link,
            },
        )
        _log_inbox("misc", log_action)
        if not cfg.shadow_mode:
            gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)

    def _flag_needs_human(reason: str, reason_type: str = "manual",
                          bot_guess: str = "", confidence: str = "",
                          mark_done: bool = True):
        """Route to the For HR queue: Gmail labels (umbrella + reason
        sub-label) + sheet row (dual-write transition) + Inbox Log."""
        # Needs Human sheet tab retired in favor of Gmail labels (For HR/*).
        # We still log to Inbox Log so the audit trail is preserved.
        if open_needs_human_threads and msg.thread_id in open_needs_human_threads:
            log.info("  -> thread %s already flagged For HR; skipping duplicate log",
                     msg.thread_id)
            _log_inbox("needs_human", f"duplicate suppressed ({reason})")
        else:
            _log_inbox("needs_human", f"flagged For HR ({reason_type}): {reason}")
            if open_needs_human_threads is not None:
                open_needs_human_threads.add(msg.thread_id)

        if cfg.shadow_mode:
            return
        nh_label = outcome_label_ids.get("needs_human", "")
        # Pick the "For HR/<reason>" sub-label that matches this routing
        # decision so HR can see WHY a thread is stuck at a glance.
        sub_label_key = gmail_client.REASON_TYPE_TO_HR_SUBLABEL.get(reason_type, "")
        sub_label_id = outcome_label_ids.get(sub_label_key, "") if sub_label_key else ""
        if nh_label:
            gmail_client.flag_needs_human(
                gmail, cfg.gmail_user, msg_id,
                needs_human_label_id=nh_label,
                processed_label_id=label_id if mark_done else "",
                extra_label_ids=[sub_label_id] if sub_label_id else None,
            )
        elif mark_done:
            gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)

    def _archive_as_closed(reasoning: str):
        body_preview = (msg.body_text or "").replace("\n", " ")[:300]
        _log_inbox("conversation_closed",
                   f"silently closed by candidate reply ({reasoning[:120]})")
        if cfg.shadow_mode:
            return
        closed_label = outcome_label_ids.get("closed", "")
        if closed_label:
            gmail_client.archive_with_outcome(
                gmail, cfg.gmail_user, msg_id,
                processed_label_id=label_id,
                outcome_label_id=closed_label,
            )
        else:
            gmail_client.mark_processed(gmail, cfg.gmail_user, msg_id, label_id)

    if _is_blocklisted_sender(msg.sender_email, cfg.blocklist_senders):
        log.info("  -> sender on blocklist; archiving as misc")
        _archive_as_misc(
            "Sender domain/address on configured blocklist.",
            "sender on blocklist; archived to Misc, no reply",
        )
        return 0

    if msg.has_calendar_invite:
        log.info("  -> calendar invite detected; archiving as misc")
        _archive_as_misc(
            "Email is a calendar invite (text/calendar or .ics attachment).",
            "calendar invite; archived to Misc, no reply",
        )
        return 0

    if msg.is_auto_response or msg.subject_indicates_ooo:
        log.info("  -> auto-response or OOO-subject detected; archiving as misc")
        _archive_as_misc(
            "Auto-reply / OOO / newsletter headers or subject; bot never replies.",
            "auto-response detected; archived to Misc, no reply",
        )
        return 0

    internal_forward = _is_internal_sender(msg.sender_email, cfg.internal_domains)
    if internal_forward:
        log.info("  -> internal sender %s; will classify but suppress outbound reply",
                 msg.sender_email)
        if msg.has_resume:
            scored = _process_resume_attachments(
                cfg, gmail, drive, sheets, all_filters, templates,
                active_role_names, paused_role_names, msg, msg_id, label_id,
                outcome_label_ids, known_emails,
                is_internal_forward=True,
                in_business_hours=in_business_hours,
                log_inbox=_log_inbox,
                learning_examples=learning_examples,
            )
            return scored

    sender_lc = (msg.sender_email or "").strip().lower()
    if not _is_job_board_alias(msg.sender_email):
        sender_count = recent_sender_counts.get(sender_lc, 0)
        if sender_count >= cfg.loop_threshold:
            log.info("  -> sender %s has %d emails in past %dh (>=%d); flagging needs_human",
                     sender_lc, sender_count, cfg.loop_window_hours, cfg.loop_threshold)
            _flag_needs_human(
                reason=f"loop suspected: {sender_count} emails in past {cfg.loop_window_hours}h",
                reason_type="loop",
                bot_guess="",
                confidence="",
            )
            return 0
    else:
        log.info("  -> sender %s is a job-board alias; skipping loop check", sender_lc)

    if cfg.shadow_mode:
        thread_history = gmail_client.ThreadHistory(frozenset(), False)
    else:
        thread_history = gmail_client.get_thread_history(
            gmail, cfg.gmail_user, msg.thread_id, cfg.internal_domains,
        )
    sent_in_thread = set(thread_history.bot_templates_sent)
    hr_manual = thread_history.hr_replied_manually
    if sent_in_thread:
        log.info("  -> thread already has bot templates: %s", sorted(sent_in_thread))
    if hr_manual:
        log.info("  -> thread already has manual HR reply (HR engaged)")

    def _can_send(template_key: str) -> tuple[bool, str]:
        if not template_key:
            return False, "no template selected"
        if _is_job_board_alias(msg.sender_email):
            return False, "job-board candidate; HR engages via platform UI"
        if internal_forward:
            return False, "sender is internal; never reply to HR/internal addresses"
        if hr_manual:
            return False, "HR replied manually in this thread"
        sender = (msg.sender_email or "").strip().lower()
        if sender and sender in known_emails:
            return False, "sender already in Candidates dashboard (HR engaged)"
        if sent_in_thread & gmail_client.TERMINAL_TEMPLATE_KEYS:
            terminal = sorted(sent_in_thread & gmail_client.TERMINAL_TEMPLATE_KEYS)
            return False, f"terminal template already sent in thread: {terminal}"
        if template_key in sent_in_thread:
            return False, f"duplicate {template_key} (already sent in thread)"
        if cfg.business_hours_only_replies and not in_business_hours:
            return False, "outside business hours (queued for next biz-hour run)"
        return True, ""

    if sent_in_thread:
        closure = scorer.classify_conversation_closure(
            api_key=cfg.anthropic_api_key,
            bot_templates_sent=sorted(sent_in_thread),
            candidate_message_body=gmail_client.strip_quoted_text(msg.body_text),
            subject=msg.subject,
        )
        log.info("  -> closure check: decision=%s confidence=%.2f reason=%r",
                 closure.decision, closure.confidence, closure.reasoning[:80])
        if closure.decision == "closed":
            log.info("  -> conversation closed; silently archiving")
            _archive_as_closed(closure.reasoning)
            return 0
        if closure.decision == "unclear":
            log.info("  -> closure unclear; routing to Needs Human")
            _flag_needs_human(
                reason=f"closure unclear: {closure.reasoning}",
                reason_type="manual",
                bot_guess="conversation_closed?",
                confidence=f"{closure.confidence:.2f}",
            )
            return 0

    indeed_quick_apply_attached = False
    if not msg.has_resume and _is_job_board_alias(msg.sender_email):
        view_url = indeed_fetcher.extract_view_resume_url(msg.body_text)
        if view_url:
            log.info("  -> Indeed Quick Apply detected (pre-classifier); fetching resume PDF")
            pdf_bytes = indeed_fetcher.fetch_resume_pdf(view_url)
            if pdf_bytes:
                log.info("  -> Indeed fetch succeeded (%d bytes); attached to msg", len(pdf_bytes))
                msg.attachments.append(gmail_client.Attachment(
                    filename="indeed_quick_apply_resume.pdf",
                    mime_type="application/pdf",
                    data=pdf_bytes,
                ))
                indeed_quick_apply_attached = True
            else:
                log.warning("  -> Indeed fetch failed; routing to Needs Human")
                _flag_needs_human(
                    reason="Indeed Quick Apply: auto-fetch failed",
                    reason_type="indeed_fetch",
                    bot_guess="resume",
                    confidence="0.95",
                )
                return 0

    if indeed_quick_apply_attached:
        log.info("  -> classifier skipped: Indeed Quick Apply PDF attached, routing to RESUME")
        cr = scorer.ClassifierResult(label="resume", confidence=0.99)
    else:
        classifier_body = gmail_client.strip_quoted_text(msg.body_text)
        if classifier_body != msg.body_text:
            log.info("  -> stripped quoted text for classifier (%d -> %d chars)",
                     len(msg.body_text), len(classifier_body))
        cr = scorer.classify_inbound_email(
            api_key=cfg.anthropic_api_key,
            subject=msg.subject,
            body=classifier_body,
            sender_email=msg.sender_email,
            has_attachment=msg.has_resume,
        )
    log.info("  -> classifier: label=%s confidence=%.2f has_attachment=%s",
             cr.label, cr.confidence, msg.has_resume)

    if cr.confidence < cfg.classifier_confidence_threshold:
        log.info("  -> confidence %.2f < threshold %.2f; flagging needs_human",
                 cr.confidence, cfg.classifier_confidence_threshold)
        _flag_needs_human(
            reason=f"low confidence ({cr.confidence:.2f} < {cfg.classifier_confidence_threshold})",
            reason_type="low_confidence",
            bot_guess=cr.label,
            confidence=f"{cr.confidence:.2f}",
        )
        return 0

    email_type = cr.label

    if email_type == "misc":
        log.info("  -> misc (not candidate-related); logging to %s and skipping",
                 cfg.misc_tab)
        _archive_as_misc(
            "Classifier flagged as non-candidate (newsletter/alert/internal/spam).",
            "archived to Misc, no reply",
        )
        return 0

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

        allowed, block_reason = _can_send(template_key)
        if not allowed:
            log.info("  -> %s suppressed: %s", template_key, block_reason)
            _log_inbox(log_type, f"suppressed ({block_reason})")
            if "outside business hours" in block_reason:
                log.info("  -> leaving unprocessed for retry next biz-hour run")
                return 0
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

    return _process_resume_attachments(
        cfg, gmail, drive, sheets, all_filters, templates,
        active_role_names, paused_role_names, msg, msg_id, label_id,
        outcome_label_ids, known_emails,
        is_internal_forward=False,
        in_business_hours=in_business_hours,
        log_inbox=_log_inbox,
        sent_in_thread=sent_in_thread,
        can_send=_can_send,
        learning_examples=learning_examples,
    )


def _process_resume_attachments(
    cfg, gmail, drive, sheets, all_filters, templates,
    active_role_names, paused_role_names, msg, msg_id, label_id,
    outcome_label_ids, known_emails, *,
    is_internal_forward: bool,
    in_business_hours: bool,
    log_inbox,
    sent_in_thread: set = None,
    can_send=None,
    learning_examples: list | None = None,
) -> int:
    if sent_in_thread is None:
        sent_in_thread = set()

    scored = 0
    last_bucket = None
    reply_queued_for_biz_hours = False
    inbox_type = "resume_internal_forward" if is_internal_forward else "resume"

    for att in msg.attachments:
        text, used_ocr = resume_parser.extract(
            att.filename, att.mime_type, att.data,
            api_key=cfg.anthropic_api_key,
        )

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
            learning_examples=learning_examples,
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
            outcome_folder_id = cfg.folder_review
            template_key = None
        elif active_matches:
            bucket = "qualified"
            outcome_folder_id = cfg.folder_qualified
            template_key = None
        elif paused_matches:
            bucket = "pending_paused"
            outcome_folder_id = cfg.folder_pending
            template_key = "paused_match"
        else:
            bucket = "not_qualified"
            outcome_folder_id = cfg.folder_not_qualified
            template_key = "denied"

        last_bucket = _better_bucket(last_bucket, bucket)

        folder_id = (cfg.folder_internal if (is_internal_forward and cfg.folder_internal)
                     else outcome_folder_id)

        applied_for = result.get("applied_for_role", "unspecified") or "unspecified"
        top_active = active_matches[0]["role"] if active_matches else ""
        is_cross_fit = (
            applied_for != "unspecified"
            and top_active
            and applied_for != top_active
        )
        cross_fit_flag = "\U0001F6A8" if is_cross_fit else ""

        if is_cross_fit and template_key == "denied":
            template_key = None
            log.info("  -> cross-fit detected; suppressing 'denied' template")

        if is_internal_forward:
            template_key = None

        recruiter_agency_value = (result.get("recruiter_agency") or "N/A").strip() or "N/A"
        is_recruiter_submission = recruiter_agency_value.lower() not in {
            "n/a", "na", "none", "null", "unknown", "",
        }
        if is_recruiter_submission and template_key:
            log.info("  -> recruiter/agency submission (%s); suppressing auto-reply",
                     recruiter_agency_value)
            template_key = None

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

        is_indeed_candidate = _is_job_board_alias(msg.sender_email)

        _sender_lc = (msg.sender_email or "").lower()
        _body_lc = (msg.body_text or "").lower()
        if is_recruiter_submission:
            application_submitted = f"Recruiter/Agency - {recruiter_agency_value}"
        elif is_indeed_candidate:
            application_submitted = "Indeed"
        elif "craigslist" in _sender_lc or "craigslist.org" in _body_lc:
            application_submitted = "Craigslist"
        else:
            application_submitted = "Email"

        candidate_timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sheets_client.append_candidate(
            sheets, cfg.sheet_id, cfg.dashboard_tab,
            {
                "timestamp": candidate_timestamp,
                "candidate_name": result["candidate_name"],
                "email": _pick_candidate_email(result["candidate_email"], msg.sender_email, resume_text=text, email_body=msg.body_text),
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
                "application_submitted": application_submitted,
            },
        )
        if is_indeed_candidate:
            sheets_client.append_indeed_queue(
                sheets, cfg.sheet_id, "Indeed Queue",
                {
                    "timestamp": candidate_timestamp,
                    "candidate_name": result["candidate_name"],
                    "applied_for": applied_for,
                    "decision": bucket,
                },
            )

        if cfg.shadow_mode:
            would_have_sent = template_key if template_key in templates else None
            log.info("  -> %s | conf=%.2f | shadow: would_have_sent=%s",
                     bucket, result["confidence"], would_have_sent)
            log_inbox(inbox_type, f"shadow: scored={bucket}, would_have_sent={would_have_sent}")
        elif template_key and template_key in templates and msg.sender_email and can_send is not None:
            allowed, block_reason = can_send(template_key)
            if not allowed:
                log.info("  -> %s | conf=%.2f | suppressed %s: %s",
                         bucket, result["confidence"], template_key, block_reason)
                log_inbox(inbox_type,
                          f"scored - {bucket}; reply suppressed ({block_reason})")
                if "outside business hours" in block_reason:
                    reply_queued_for_biz_hours = True
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
                sent_in_thread.add(template_key)
                log.info("  -> %s | conf=%.2f | sent '%s' to %s",
                         bucket, result["confidence"], template_key, msg.sender_email)
                log_inbox(inbox_type, f"scored - {bucket}; replied with {template_key}")
        else:
            if is_internal_forward:
                log.info("  -> %s | conf=%.2f | internal forward (no auto-reply)",
                         bucket, result["confidence"])
                log_inbox(inbox_type,
                          f"scored - {bucket}; internal forward (no reply, Internal folder)")
            else:
                log.info("  -> %s | conf=%.2f | no template / no email",
                         bucket, result["confidence"])
                log_inbox(inbox_type, f"scored - {bucket}; no auto-reply")

        scored += 1

    final_bucket = last_bucket or "unreadable"
    if cfg.shadow_mode:
        log.info("  -> email outcome=%s, shadow mode (no Gmail label changes)", final_bucket)
        return scored

    if reply_queued_for_biz_hours:
        log.info("  -> outcome=%s, leaving unprocessed for biz-hour reply retry",
                 final_bucket)
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
