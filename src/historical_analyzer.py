"""Historical email analyzer.

Mines the pre-bot jobs@ inbox to extract real labeled examples for tuning
the production classifier prompt. For each thread in the configured date
window:

  1. Pull the full Gmail thread (every message, both directions).
  2. Identify the FIRST inbound message (not sent by HR/internal).
  3. Identify HR's FIRST response, if any.
  4. Send (original message + HR's response) to Claude Sonnet, which infers
     the true category from HR's behavior -- a much stronger label than what
     a per-email classifier could guess in real time.
  5. Run the current production classifier on the same message as a baseline
     so we can see where bot and ground-truth disagree.
  6. Write a row to the 'Historical Analysis' tab on the bot's main sheet.

Manual workflow (workflow_dispatch only). Designed to be run once after
launch to seed the few-shot pool, then again every few months to catch
pattern drift.

Run as: python -m src.historical_analyzer [--days-back 180] [--max-threads 500]
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import json
import logging
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import config, google_auth, scorer

log = logging.getLogger(__name__)


# --------------------------- Constants --------------------------------------

ANALYSIS_TAB = "Historical Analysis"

ANALYSIS_HEADERS = [
    "Analyzed At", "Thread ID", "Original Date", "Sender Email",
    "Sender Domain", "Subject", "Body Preview", "Has Attachment",
    "HR Replied", "HR Reply Preview", "Inferred Category", "Confidence",
    "Key Signals", "Useful Example", "Bot Would Classify As",
    "Agreement", "Notes", "Gmail Thread Link",
]

ANALYZER_MODEL = "claude-sonnet-4-5"
DEFAULT_DAYS_BACK = 180  # 6 months of history

# How long an extracted body chunk can be (chars). Big enough to give Claude
# enough context; small enough to cap API cost.
MAX_BODY_CHARS = 2000
MAX_HR_REPLY_CHARS = 1500

# What to write to the sheet (sheet cells truncate hard; keep modest).
SHEET_BODY_PREVIEW_CHARS = 500
SHEET_HR_PREVIEW_CHARS = 300


ANALYZER_PROMPT = """You analyze pre-bot HR email threads to label them \
for an email triage classifier. The classifier sorts inbound emails into \
one of these categories:

  RESUME                  -- a candidate is applying AND attached their resume
  APPLICATION_NO_RESUME   -- a candidate is applying but forgot the attachment
  QUESTION                -- someone is asking HR a question (pay, hours,
                             process) and wants a human reply
  MISC                    -- newsletter, recruiter outreach, automated
                             notification, internal forward, spam -- NOT
                             a candidate at all
  UNCLEAR                 -- genuinely ambiguous after seeing HR's behavior

You see (a) the original inbound email and (b) what HR did in response. \
Use HR's behavior as ground truth: if HR replied substantively to a resume \
attachment, it WAS a resume; if HR asked for the file, it WAS \
application_no_resume; if HR answered a question with information, it WAS \
a question; if HR archived without replying, it was probably misc (or a \
question HR chose not to answer).

Return JSON ONLY (no prose, no code fences). Exact schema:

{
  "category": "RESUME" | "APPLICATION_NO_RESUME" | "QUESTION" | "MISC" | "UNCLEAR",
  "confidence": <number between 0.0 and 1.0>,
  "signals": ["<short phrases naming what told you the category>"],
  "useful_example": <true | false>,
  "notes": "<1-2 sentences explaining the case>"
}

useful_example=true ONLY when the case is a non-obvious edge case the \
classifier would benefit from seeing as a few-shot example (recruiter \
pretending to be applicant, vague short subject, link instead of \
attachment, internal forward dressed as application, etc.). Routine \
applications with a clear resume attachment should be false."""


# --------------------------- Gmail helpers ---------------------------------

def _format_gmail_date(d: datetime) -> str:
    """Gmail query format: YYYY/MM/DD."""
    return d.strftime("%Y/%m/%d")


def _get_header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def _walk_parts(payload: dict):
    if "parts" in payload:
        for p in payload["parts"]:
            yield from _walk_parts(p)
    else:
        yield payload


def _decode_body(part: dict) -> str:
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body_text(payload: dict) -> str:
    """Best-effort text extraction. Prefers text/plain; falls back to
    stripping HTML tags from text/html."""
    plain, html = [], []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        if mime.startswith("text/plain"):
            plain.append(_decode_body(part))
        elif mime.startswith("text/html"):
            html.append(_decode_body(part))
    if plain:
        return "\n".join(plain).strip()
    if html:
        return re.sub(r"<[^>]+>", " ", "\n".join(html)).strip()
    return ""


def _has_attachment(payload: dict) -> bool:
    """True if any part of the message has a real file attachment (filename
    present, attachmentId present)."""
    for part in _walk_parts(payload):
        fname = part.get("filename") or ""
        att_id = part.get("body", {}).get("attachmentId")
        if att_id and fname:
            return True
    return False


def _is_internal_sender(from_addr: str, gmail_user: str,
                        internal_domains: tuple) -> bool:
    """True if From: is the HR mailbox or an internal company domain."""
    fa = (from_addr or "").lower()
    if gmail_user and gmail_user.lower() in fa:
        return True
    for dom in internal_domains:
        if "@" + dom in fa:
            return True
    return False


def list_thread_ids(svc, user: str, after: datetime, before: datetime,
                    gmail_user: str) -> list[str]:
    """Find thread IDs in [after, before) that contain at least one message
    NOT sent by the HR mailbox. We exclude bot-sent messages so the result
    list reflects 'threads that started with someone writing in to HR.'"""
    query = (f"after:{_format_gmail_date(after)} "
             f"before:{_format_gmail_date(before)} "
             f"-from:{gmail_user}")
    thread_ids: list[str] = []
    page_token = None
    while True:
        params = {"userId": user, "q": query, "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        resp = svc.users().threads().list(**params).execute()
        thread_ids.extend(t["id"] for t in resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return thread_ids


def fetch_thread(svc, user: str, thread_id: str) -> dict:
    return svc.users().threads().get(
        userId=user, id=thread_id, format="full",
    ).execute()


def analyze_thread_structure(thread: dict, gmail_user: str,
                              internal_domains: tuple) -> dict | None:
    """Extract the key info we need from a thread: first inbound + first
    HR response after it. Returns None if the thread is all-internal
    (no real applicant message to analyze)."""
    messages = thread.get("messages", []) or []
    if not messages:
        return None
    # Sort messages by Gmail's internalDate (ms epoch).
    messages.sort(key=lambda m: int(m.get("internalDate") or 0))

    # First inbound = first message NOT from HR or an internal domain.
    first_inbound = None
    for m in messages:
        from_addr = _get_header(m.get("payload", {}), "From")
        if not _is_internal_sender(from_addr, gmail_user, internal_domains):
            first_inbound = m
            break
    if not first_inbound:
        return None

    # First HR reply = first message AFTER first_inbound sent by HR/internal.
    hr_reply = None
    fi_ts = int(first_inbound.get("internalDate") or 0)
    for m in messages:
        if m["id"] == first_inbound["id"]:
            continue
        from_addr = _get_header(m.get("payload", {}), "From")
        if not _is_internal_sender(from_addr, gmail_user, internal_domains):
            continue
        if int(m.get("internalDate") or 0) <= fi_ts:
            continue
        hr_reply = m
        break

    fi_payload = first_inbound.get("payload", {})
    sender_raw = _get_header(fi_payload, "From")
    _, sender_email = email.utils.parseaddr(sender_raw)
    sender_email = (sender_email or "").lower()
    sender_domain = sender_email.rsplit("@", 1)[-1] if "@" in sender_email else ""

    return {
        "thread_id": thread.get("id"),
        "first_inbound_date": _get_header(fi_payload, "Date"),
        "sender_raw": sender_raw,
        "sender_email": sender_email,
        "sender_domain": sender_domain,
        "subject": _get_header(fi_payload, "Subject"),
        "body": _extract_body_text(fi_payload),
        "has_attachment": _has_attachment(fi_payload),
        "hr_replied": hr_reply is not None,
        "hr_reply_body": (_extract_body_text(hr_reply.get("payload", {}))
                          if hr_reply else ""),
        "n_messages": len(messages),
    }


# --------------------------- Claude analyzer -------------------------------

def call_claude_analyzer(api_key: str, info: dict) -> dict | None:
    """Send the thread context to Claude Sonnet for category inference.
    Returns parsed JSON or None on failure."""
    try:
        import anthropic
    except ImportError as e:
        log.error("anthropic SDK unavailable: %s", e)
        return None

    body_chunk = (info.get("body") or "")[:MAX_BODY_CHARS]
    hr_chunk = (info.get("hr_reply_body") or "")[:MAX_HR_REPLY_CHARS]

    user_msg = (
        "ORIGINAL EMAIL:\n"
        f"From: {info.get('sender_raw') or '(unknown)'}\n"
        f"Subject: {info.get('subject') or '(none)'}\n"
        f"Has Attachment: {'yes' if info.get('has_attachment') else 'no'}\n\n"
        "Body:\n"
        f"{body_chunk or '(empty)'}\n\n"
        "---\n"
        "HR RESPONSE:\n"
        f"{hr_chunk if info.get('hr_replied') else '[HR did not reply in this thread]'}\n"
        "---\n\n"
        "Analyze and return JSON only."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=ANALYZER_MODEL,
            max_tokens=600,
            system=ANALYZER_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        # Strip code fences if the model wrapped JSON despite instructions.
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*\n?", "", text)
            text = re.sub(r"\n?```\s*$", "", text).strip()
        return json.loads(text)
    except Exception as e:
        log.warning("Claude analyzer call failed: %s", e)
        return None


def call_bot_baseline(api_key: str, info: dict) -> str:
    """Run the production classifier on the same message to see what the
    bot WOULD have classified it as. Used for the Agreement column --
    measures bot accuracy against the ground truth Claude derives from
    HR's behavior."""
    try:
        cr = scorer.classify_inbound_email(
            api_key=api_key,
            subject=info.get("subject", ""),
            body=info.get("body", ""),
            sender_email=info.get("sender_email", ""),
            has_attachment=info.get("has_attachment", False),
        )
        return cr.label
    except Exception as e:
        log.warning("Bot baseline classifier failed: %s", e)
        return "error"


# --------------------------- Sheet helpers ---------------------------------

def ensure_analysis_tab(svc, sheet_id: str) -> None:
    """Create the Historical Analysis tab if missing; write headers if blank."""
    try:
        meta = svc.spreadsheets().get(
            spreadsheetId=sheet_id,
            fields="sheets.properties.title",
        ).execute()
    except Exception as e:
        log.error("Could not load sheet metadata: %s", e)
        raise

    existing = [s["properties"]["title"]
                for s in meta.get("sheets", []) if s.get("properties")]
    if ANALYSIS_TAB not in existing:
        log.info("Creating tab %r", ANALYSIS_TAB)
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={"requests": [
                {"addSheet": {"properties": {"title": ANALYSIS_TAB}}},
            ]},
        ).execute()

    # Write headers if A1 is blank.
    head_rng = f"{ANALYSIS_TAB}!A1:R1"
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id, range=head_rng,
    ).execute()
    if not resp.get("values"):
        log.info("Writing header row to %r", ANALYSIS_TAB)
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id, range=head_rng, valueInputOption="RAW",
            body={"values": [ANALYSIS_HEADERS]},
        ).execute()


def append_analysis_row(svc, sheet_id: str, row: list) -> None:
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{ANALYSIS_TAB}!A:R",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()


def _hyperlink(url: str, label: str = "Link") -> str:
    if not url:
        return ""
    return f'=HYPERLINK("{url.replace(chr(34), "")}","{label}")'


def _safe(v) -> str:
    """Escape values Sheets USER_ENTERED would parse as a formula."""
    if v is None:
        return ""
    s = str(v)
    if s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


# --------------------------- Entry point -----------------------------------

def _parse_bot_start_date(s: str) -> datetime:
    """Accept YYYY-MM-DD or YYYY/MM/DD."""
    if not s:
        # Fallback to 'today' if env var missing.
        return datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=None,
        )
    norm = s.replace("-", "/").strip()
    return datetime.strptime(norm, "%Y/%m/%d")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Mine pre-bot Gmail history for classifier training examples.",
    )
    parser.add_argument(
        "--days-back", type=int, default=DEFAULT_DAYS_BACK,
        help=f"Days before BOT_START_DATE to scan (default: {DEFAULT_DAYS_BACK}).",
    )
    parser.add_argument(
        "--max-threads", type=int, default=500,
        help="Cap on total threads analyzed in this run (default: 500).",
    )
    args = parser.parse_args()

    cfg = config.load()

    creds = google_auth.make_credentials(
        cfg.oauth_client_id, cfg.oauth_client_secret, cfg.oauth_refresh_token,
    )
    gmail = google_auth.gmail(creds)
    sheets = google_auth.sheets(creds)

    # Date window: [bot_start - days_back, bot_start)
    bot_start_dt = _parse_bot_start_date(cfg.bot_start_date)
    after = bot_start_dt - timedelta(days=args.days_back)
    before = bot_start_dt

    log.info("Scanning %s -> %s in mailbox %s",
             after.date(), before.date(), cfg.gmail_user)

    ensure_analysis_tab(sheets, cfg.sheet_id)

    thread_ids = list_thread_ids(
        gmail, cfg.gmail_user, after, before, cfg.gmail_user,
    )
    log.info("Found %d candidate threads", len(thread_ids))
    if args.max_threads and len(thread_ids) > args.max_threads:
        log.info("Capping to first %d threads (--max-threads)", args.max_threads)
        thread_ids = thread_ids[:args.max_threads]

    analyzed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counter: Counter = Counter()
    skipped = 0
    written = 0

    for i, tid in enumerate(thread_ids, 1):
        try:
            thread = fetch_thread(gmail, cfg.gmail_user, tid)
            info = analyze_thread_structure(thread, cfg.gmail_user,
                                             cfg.internal_domains)
            if not info:
                log.info("[%d/%d] %s -> all-internal, skipping",
                         i, len(thread_ids), tid)
                skipped += 1
                continue

            # Skip internal-sender threads (we want APPLICANT pre-bot mail).
            if info["sender_domain"] in cfg.internal_domains:
                log.info("[%d/%d] %s -> internal sender, skipping",
                         i, len(thread_ids), tid)
                skipped += 1
                continue

            log.info("[%d/%d] %s -- subj=%r from=%s",
                     i, len(thread_ids), tid,
                     (info["subject"] or "")[:60], info["sender_email"])

            claude_result = call_claude_analyzer(cfg.anthropic_api_key, info)
            bot_label = call_bot_baseline(cfg.anthropic_api_key, info)

            category = ((claude_result or {}).get("category") or "UNCLEAR").upper()
            confidence = (claude_result or {}).get("confidence", "")
            signals = ", ".join((claude_result or {}).get("signals") or [])[:500]
            useful = "yes" if (claude_result or {}).get("useful_example") else "no"
            notes = ((claude_result or {}).get("notes") or "")[:500]

            # Normalize for comparison: strip underscores, uppercase.
            norm_bot = (bot_label or "").upper().replace("_", "")
            norm_claude = category.replace("_", "")
            agreement = "yes" if norm_bot == norm_claude else "no"

            counter[category] += 1
            thread_link = f"https://mail.google.com/mail/u/0/#all/{tid}"

            row = [
                analyzed_at,
                tid,
                info["first_inbound_date"],
                _safe(info["sender_email"]),
                info["sender_domain"],
                _safe(info["subject"]),
                _safe((info["body"] or "").replace("\n", " ")[:SHEET_BODY_PREVIEW_CHARS]),
                "yes" if info["has_attachment"] else "no",
                "yes" if info["hr_replied"] else "no",
                _safe((info["hr_reply_body"] or "").replace("\n", " ")[:SHEET_HR_PREVIEW_CHARS]),
                category,
                str(confidence),
                _safe(signals),
                useful,
                bot_label,
                agreement,
                _safe(notes),
                _hyperlink(thread_link),
            ]
            append_analysis_row(sheets, cfg.sheet_id, row)
            written += 1
        except Exception as e:
            log.exception("[%d/%d] thread %s failed: %s",
                          i, len(thread_ids), tid, e)

    log.info("")
    log.info("Done. Wrote %d rows. Skipped %d threads. Category breakdown: %s",
             written, skipped, dict(counter))
    return 0


if __name__ == "__main__":
    sys.exit(main())
