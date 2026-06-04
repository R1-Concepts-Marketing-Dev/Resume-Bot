"""Gmail operations: find unprocessed messages, pull attachments, apply
labels, and send reply emails."""

from __future__ import annotations

import base64
import email.utils
import logging
import re
from dataclasses import dataclass, field
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

# Header the bot adds to every outgoing reply so future runs can detect
# which template was sent. Deliberately bland -- if an applicant views
# raw mail source they see "X-R1-Ref: nr" and have no idea what it means
# or that an automated system sent the reply.
BOT_TEMPLATE_HEADER = "X-R1-Ref"

# Backward-compat: the bot previously used this header name. Old threads
# may have it on past bot messages, so the introspection code still reads
# it. New messages always use BOT_TEMPLATE_HEADER.
LEGACY_BOT_HEADER = "X-Resume-Bot-Template"

# Opaque short codes used in the outgoing X-R1-Ref header. The full
# template name never appears in headers.
TEMPLATE_KEY_TO_CODE = {
    "no_resume":    "nr",
    "question":     "q",
    "denied":       "d",
    "paused_match": "pm",
}
TEMPLATE_CODE_TO_KEY = {v: k for k, v in TEMPLATE_KEY_TO_CODE.items()}

# Templates that count as terminal -- once sent, the bot must not auto-reply
# again in that thread. HR owns the conversation from here.
TERMINAL_TEMPLATE_KEYS = frozenset({"denied", "paused_match"})

# Every template key the bot might send. Used to filter unknown values
# out of the legacy template header, just in case.
ALL_TEMPLATE_KEYS = frozenset({"no_resume", "question", "denied", "paused_match"})

# Headers that signal "this is an automated / bulk email, do not auto-reply".
# Value of None means "any non-empty value triggers". Otherwise tuple of
# substrings (case-insensitive) that must appear in the header value.
_AUTO_REPLY_HEADER_SIGNALS = {
    "auto-submitted":   ("auto-replied", "auto-generated", "auto-notified"),
    "x-autoreply":      None,
    "x-autorespond":    None,
    "x-autoresponder":  None,
    "precedence":       ("auto_reply", "bulk", "list", "junk"),
    "list-unsubscribe": None,
    "list-id":          None,
}

# Regex patterns that mark the start of quoted/forwarded content in an
# email body. Used by strip_quoted_text() so the classifier sees only
# what the current sender typed this turn.
_QUOTE_PATTERNS = [
    re.compile(r"\n\s*On\s[^\n]{1,200}\swrote:\s*\n", re.IGNORECASE),
    re.compile(r"\n\s*-{2,}\s*Original Message\s*-{2,}\s*\n", re.IGNORECASE),
    re.compile(r"\n\s*From:\s[^\n]{1,200}\n\s*Sent:\s", re.IGNORECASE),
    re.compile(r"\n\s*Begin forwarded message:\s*\n", re.IGNORECASE),
    re.compile(r"\n_{10,}\s*\n"),
]


RESUME_MIME_TYPES = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "text/plain": ".txt",
    "text/rtf": ".rtf",
    "application/rtf": ".rtf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/heic": ".heic",
    "image/webp": ".webp",
    "image/tiff": ".tif",
}

OUTCOME_LABELS = {
    "qualified":       "Resume Bot/Qualified",
    "needs_review":    "Resume Bot/Needs Review",
    "not_qualified":   "Resume Bot/Not Qualified",
    "pending_paused":  "Resume Bot/Pending Paused Role",
    "unreadable":      "Resume Bot/Unreadable",
    "not_a_resume":    "Resume Bot/Not A Resume",
}


@dataclass
class Attachment:
    filename: str
    mime_type: str
    data: bytes


@dataclass
class Message:
    id: str
    thread_id: str
    subject: str
    sender: str
    sender_email: str
    sender_name: str
    body_text: str
    attachments: list[Attachment] = field(default_factory=list)
    was_unread: bool = False
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def thread_link(self) -> str:
        return f"https://mail.google.com/mail/u/0/#inbox/{self.thread_id}"

    @property
    def has_resume(self) -> bool:
        return bool(self.attachments)

    @property
    def is_auto_response(self) -> bool:
        """True if the headers indicate this is an auto-reply, OOO,
        newsletter, mailing-list traffic, or otherwise automated."""
        for hdr, allowed_substrings in _AUTO_REPLY_HEADER_SIGNALS.items():
            val = (self.headers.get(hdr) or "").lower()
            if not val:
                continue
            if allowed_substrings is None:
                return True
            if any(s in val for s in allowed_substrings):
                return True
        return False


@dataclass(frozen=True)
class ThreadHistory:
    """Snapshot of what's happened in a Gmail thread, used to gate bot
    auto-replies.

    bot_templates_sent  -- set of template_keys the bot already sent
                           in this thread (no_resume, question, denied,
                           paused_match). Detected via the X-R1-Ref
                           header on outgoing messages.

    hr_replied_manually -- True if any message in the thread was sent
                           from an internal address (jobs@, HR personal
                           inbox, anywhere @ internal_domains) BUT does
                           NOT carry the bot's tracking header. That
                           signals a human HR reply. When true the bot
                           should never auto-reply in this thread --
                           HR owns the conversation.
    """
    bot_templates_sent: frozenset
    hr_replied_manually: bool


def ensure_label(svc, user: str, name: str) -> str:
    existing = svc.users().labels().list(userId=user).execute().get("labels", [])
    for lbl in existing:
        if lbl["name"] == name:
            return lbl["id"]
    created = svc.users().labels().create(
        userId=user,
        body={"name": name, "labelListVisibility": "labelShow",
              "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def ensure_outcome_labels(svc, user: str) -> dict[str, str]:
    """Create the 6 outcome labels under a 'Resume Bot' parent if missing."""
    existing = {lbl["name"]: lbl["id"]
                for lbl in svc.users().labels().list(userId=user).execute().get("labels", [])}
    if "Resume Bot" not in existing:
        created = svc.users().labels().create(
            userId=user,
            body={"name": "Resume Bot",
                  "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()
        existing["Resume Bot"] = created["id"]
    out: dict[str, str] = {}
    for key, name in OUTCOME_LABELS.items():
        if name in existing:
            out[key] = existing[name]
            continue
        created = svc.users().labels().create(
            userId=user,
            body={"name": name,
                  "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()
        out[key] = created["id"]
    return out


def list_unprocessed(svc, user: str, processed_label: str, max_results: int,
                     start_date: str = "") -> list[str]:
    """Live-mode message lookup. Inbox messages that don't already have the
    bot-seen label, optionally floored to emails received after start_date."""
    query = f"in:inbox -label:{processed_label}"
    after = _format_after_date(start_date)
    if after:
        query = f"{query} after:{after}"
    resp = svc.users().messages().list(
        userId=user, q=query, maxResults=max_results
    ).execute()
    return [m["id"] for m in resp.get("messages", [])]


def _format_after_date(s: str) -> str:
    if not s:
        return ""
    s = s.strip().replace("-", "/")
    parts = s.split("/")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        log.warning("BOT_START_DATE %r looks malformed; ignoring", s)
        return ""
    return s


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
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
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        if mime.startswith("text/plain"):
            plain_chunks.append(_decode_body(part))
        elif mime.startswith("text/html"):
            html_chunks.append(_decode_body(part))
    if plain_chunks:
        return "\n".join(plain_chunks).strip()
    if html_chunks:
        return re.sub(r"<[^>]+>", " ", "\n".join(html_chunks)).strip()
    return ""


def fetch(svc, user: str, msg_id: str) -> Message:
    msg = svc.users().messages().get(userId=user, id=msg_id, format="full").execute()
    payload = msg.get("payload", {})

    attachments: list[Attachment] = []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        att_id = part.get("body", {}).get("attachmentId")
        if not att_id:
            continue
        if mime not in RESUME_MIME_TYPES and not filename.lower().endswith(
            tuple(RESUME_MIME_TYPES.values())
        ):
            continue
        att = svc.users().messages().attachments().get(
            userId=user, messageId=msg_id, id=att_id
        ).execute()
        data = base64.urlsafe_b64decode(att["data"])
        attachments.append(Attachment(filename=filename, mime_type=mime, data=data))

    sender_raw = _header(payload, "From")
    sender_name, sender_email = email.utils.parseaddr(sender_raw)
    if not sender_email and "@" in sender_raw:
        sender_email = sender_raw.strip().strip("<>")

    headers_map: dict[str, str] = {}
    for h in payload.get("headers", []):
        key = (h.get("name") or "").lower()
        if key:
            headers_map[key] = h.get("value", "")

    return Message(
        id=msg_id,
        thread_id=msg.get("threadId", ""),
        subject=_header(payload, "Subject"),
        sender=sender_raw,
        sender_email=sender_email,
        sender_name=sender_name or (sender_email.split("@")[0] if sender_email else ""),
        body_text=_extract_body_text(payload),
        attachments=attachments,
        was_unread="UNREAD" in msg.get("labelIds", []),
        headers=headers_map,
    )


def mark_processed(svc, user: str, msg_id: str, label_id: str) -> None:
    svc.users().messages().modify(
        userId=user, id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()


def mark_unread(svc, user: str, msg_id: str) -> None:
    try:
        svc.users().messages().modify(
            userId=user, id=msg_id,
            body={"addLabelIds": ["UNREAD"]},
        ).execute()
    except Exception as e:
        log.warning("Failed to re-mark msg=%s unread: %s", msg_id, e)


def archive_with_outcome(svc, user: str, msg_id: str, *,
                         processed_label_id: str, outcome_label_id: str) -> None:
    svc.users().messages().modify(
        userId=user, id=msg_id,
        body={
            "addLabelIds": [processed_label_id, outcome_label_id],
            "removeLabelIds": ["INBOX"],
        },
    ).execute()


def send_reply(svc, user: str, *, to: str, subject: str, body: str,
               thread_id: str = "", in_reply_to_msg_id: str = "",
               template_key: str = "") -> None:
    """Send a plaintext reply. If template_key is provided, stamps the
    outgoing message with the X-R1-Ref header (opaque short code) so
    future runs can detect which template was already sent in this
    thread without revealing anything about automation to the recipient."""
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["From"] = user
    mime["Subject"] = subject
    if in_reply_to_msg_id:
        mime["In-Reply-To"] = in_reply_to_msg_id
        mime["References"] = in_reply_to_msg_id
    if template_key:
        code = TEMPLATE_KEY_TO_CODE.get(template_key)
        if code:
            mime[BOT_TEMPLATE_HEADER] = code

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    body_obj: dict = {"raw": raw}
    if thread_id:
        body_obj["threadId"] = thread_id

    svc.users().messages().send(userId=user, body=body_obj).execute()


# --------------------------- Quote / forward stripping -----------------------

def strip_quoted_text(body: str) -> str:
    """Strip quoted and forwarded content from an email body so a classifier
    sees only what the current sender typed this turn. Conservative: if
    stripping would leave fewer than 30 chars, returns the body unchanged."""
    if not body or len(body) < 50:
        return body

    earliest = len(body)
    for pat in _QUOTE_PATTERNS:
        m = pat.search(body)
        if m and m.start() < earliest:
            earliest = m.start()

    lines = body.split("\n")
    running_pos = 0
    for i, line in enumerate(lines[:-1]):
        if line.lstrip().startswith(">") and lines[i + 1].lstrip().startswith(">"):
            if running_pos < earliest:
                earliest = running_pos
            break
        running_pos += len(line) + 1

    stripped = body[:earliest].rstrip()
    if len(stripped) < 30:
        return body
    return stripped


# --------------------------- Thread introspection --------------------------

# Subjects of bot-sent templates, used for legacy-thread detection on
# messages that predate the X-R1-Ref header. Match prefix, case-insensitive,
# after stripping any "Re:" / "Fwd:" prefix.
_LEGACY_SUBJECT_PREFIXES = {
    "please resend with your resume":  "no_resume",
    "thanks for reaching out to ":     "question",
    "thank you for your interest in ": "denied",
    "we'll keep your resume on file for ": "paused_match",
}


def get_thread_history(svc, user: str, thread_id: str,
                       internal_domains) -> ThreadHistory:
    """Return what the bot has done in this thread plus whether a human at
    HR has manually replied.

    Detection rules for each message in the thread:
      1. If From: matches an internal address (the GMAIL_USER mailbox OR
         any address @ internal_domains), inspect it. Otherwise ignore.
      2. Modern: X-R1-Ref header carrying a known short code -> bot reply,
         add the corresponding template key to bot_templates_sent.
      3. Legacy: X-Resume-Bot-Template header carrying a known key ->
         bot reply (backward compat).
      4. Legacy: subject-line prefix match against known template subjects
         -> bot reply (for old threads from before the header existed).
      5. Otherwise (internal sender, no recognizable bot fingerprint) ->
         a manual HR reply. Sets hr_replied_manually=True.

    Returns an empty ThreadHistory if the thread can't be loaded; that's
    the safe default (no signals -> let the bot's standard logic decide)."""
    try:
        thread = svc.users().threads().get(
            userId=user, id=thread_id, format="full",
        ).execute()
    except Exception as e:
        log.warning("Could not load thread %s: %s", thread_id, e)
        return ThreadHistory(frozenset(), False)

    sent: set = set()
    hr_manual = False
    user_lower = (user or "").lower()
    domains = tuple((d or "").lower().lstrip("@") for d in (internal_domains or ()))

    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        msg_headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in payload.get("headers", [])}
        from_addr = (msg_headers.get("from") or "").lower()

        # Inspect only messages sent from an internal address (the HR mailbox
        # itself or any address at an internal domain). External senders
        # are candidates -- their messages don't affect bot reply gating.
        is_internal = False
        if user_lower and user_lower in from_addr:
            is_internal = True
        else:
            for dom in domains:
                if dom and ("@" + dom) in from_addr:
                    is_internal = True
                    break
        if not is_internal:
            continue

        # (a) Modern: opaque short code in X-R1-Ref.
        code = (msg_headers.get(BOT_TEMPLATE_HEADER.lower()) or "").strip()
        if code and code in TEMPLATE_CODE_TO_KEY:
            sent.add(TEMPLATE_CODE_TO_KEY[code])
            continue

        # (b) Legacy: full key in X-Resume-Bot-Template (recent transition).
        legacy_tmpl = (msg_headers.get(LEGACY_BOT_HEADER.lower()) or "").strip()
        if legacy_tmpl in ALL_TEMPLATE_KEYS:
            sent.add(legacy_tmpl)
            continue

        # (c) Legacy: subject-line match (old threads pre-header).
        subj = (msg_headers.get("subject") or "").strip()
        subj_norm = re.sub(r"^(re|fwd?):\s*", "", subj, flags=re.IGNORECASE).lower()
        matched_legacy = False
        for prefix, key in _LEGACY_SUBJECT_PREFIXES.items():
            if subj_norm.startswith(prefix):
                sent.add(key)
                matched_legacy = True
                break
        if matched_legacy:
            continue

        # No bot fingerprint on an internal-sent message -> manual HR reply.
        hr_manual = True

    return ThreadHistory(
        bot_templates_sent=frozenset(sent),
        hr_replied_manually=hr_manual,
    )


def get_sent_templates_in_thread(svc, user: str, thread_id: str) -> set:
    """Backward-compat wrapper. Prefer get_thread_history -- it also
    surfaces the hr_replied_manually flag. Internal domains default to
    empty here, so this only detects the GMAIL_USER mailbox's own
    history (the legacy behavior)."""
    h = get_thread_history(svc, user, thread_id, internal_domains=())
    return set(h.bot_templates_sent)
