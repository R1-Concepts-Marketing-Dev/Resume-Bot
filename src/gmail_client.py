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

# Custom header the bot adds to every outgoing reply, naming which template
# was used. Lets us detect "what has the bot already said in this thread?"
# without parsing subject lines.
BOT_TEMPLATE_HEADER = "X-Resume-Bot-Template"

# Templates that count as terminal -- once sent, the bot must not auto-reply
# again in that thread. HR owns the conversation from here.
TERMINAL_TEMPLATE_KEYS = frozenset({"denied", "paused_match"})

# Every template key the bot might send. Used to filter unknown values
# out of the X-Resume-Bot-Template header, just in case.
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
    # Gmail / Apple Mail: "On Mon, Jan 1, 2024 at 1:23 PM, Name <e@x> wrote:"
    re.compile(r"\n\s*On\s[^\n]{1,200}\swrote:\s*\n", re.IGNORECASE),
    # Outlook reply separator: "-----Original Message-----"
    re.compile(r"\n\s*-{2,}\s*Original Message\s*-{2,}\s*\n", re.IGNORECASE),
    # Outlook forwarded-header block ("From: ...\nSent: ...")
    re.compile(r"\n\s*From:\s[^\n]{1,200}\n\s*Sent:\s", re.IGNORECASE),
    # Apple Mail forward marker
    re.compile(r"\n\s*Begin forwarded message:\s*\n", re.IGNORECASE),
    # Long underscore separator (some clients)
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
    # All inbound headers, lower-cased keys. Used by is_auto_response and
    # any caller that needs to inspect specific headers like Message-ID.
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
        newsletter, mailing-list traffic, or otherwise automated. The bot
        should never reply to these -- replying creates ping-pong loops
        and the recipient isn't a human anyway."""
        for hdr, allowed_substrings in _AUTO_REPLY_HEADER_SIGNALS.items():
            val = (self.headers.get(hdr) or "").lower()
            if not val:
                continue
            if allowed_substrings is None:
                return True
            if any(s in val for s in allowed_substrings):
                return True
        return False


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
    """Create the 5 outcome labels under a 'Resume Bot' parent if missing."""
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
    """Accept YYYY-MM-DD or YYYY/MM/DD, return YYYY/MM/DD for Gmail's after:
    operator. Empty input returns empty (no filter)."""
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

    # Capture all headers as a lower-cased-key dict so is_auto_response (and
    # any future header-based checks) doesn't need to walk the list each time.
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
    """Apply the 'I've seen this' label without archiving."""
    svc.users().messages().modify(
        userId=user, id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()


def mark_unread(svc, user: str, msg_id: str) -> None:
    """Force the UNREAD label back on a message. Used in shadow mode."""
    try:
        svc.users().messages().modify(
            userId=user, id=msg_id,
            body={"addLabelIds": ["UNREAD"]},
        ).execute()
    except Exception as e:
        log.warning("Failed to re-mark msg=%s unread: %s", msg_id, e)


def archive_with_outcome(svc, user: str, msg_id: str, *,
                         processed_label_id: str, outcome_label_id: str) -> None:
    """Apply both the bot-seen label and the outcome label, then remove INBOX."""
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
    outgoing message with the X-Resume-Bot-Template header so future runs
    can detect what was already sent in this thread."""
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["From"] = user
    mime["Subject"] = subject
    if in_reply_to_msg_id:
        mime["In-Reply-To"] = in_reply_to_msg_id
        mime["References"] = in_reply_to_msg_id
    if template_key:
        mime[BOT_TEMPLATE_HEADER] = template_key

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    body_obj: dict = {"raw": raw}
    if thread_id:
        body_obj["threadId"] = thread_id

    svc.users().messages().send(userId=user, body=body_obj).execute()


# --------------------------- Quote / forward stripping -----------------------

def strip_quoted_text(body: str) -> str:
    """Strip quoted and forwarded content from an email body so a classifier
    sees only what the current sender typed this turn.

    Conservative by design:
    - If no quote markers are found, returns the body unchanged.
    - If stripping would leave fewer than 30 chars, returns the body
      unchanged (we'd be cutting too aggressively to be useful).

    Does NOT modify the body kept on the Message dataclass; callers should
    invoke this just before passing text to the classifier, and keep the
    original around for audit / scoring purposes."""
    if not body or len(body) < 50:
        return body

    earliest = len(body)
    for pat in _QUOTE_PATTERNS:
        m = pat.search(body)
        if m and m.start() < earliest:
            earliest = m.start()

    # Markdown-style quoting: two consecutive lines starting with ">". Find
    # the start of the first such block and use it as a cut point.
    lines = body.split("\n")
    running_pos = 0
    for i, line in enumerate(lines[:-1]):
        if line.lstrip().startswith(">") and lines[i + 1].lstrip().startswith(">"):
            if running_pos < earliest:
                earliest = running_pos
            break
        running_pos += len(line) + 1  # +1 for the newline we split on

    stripped = body[:earliest].rstrip()
    if len(stripped) < 30:
        # Too aggressive -- give back the original rather than feed the
        # classifier an empty string.
        return body
    return stripped


# --------------------------- Thread / template introspection ----------------

def get_sent_templates_in_thread(svc, user: str, thread_id: str) -> set[str]:
    """Return the set of template keys the bot has already sent in this
    thread. Used to enforce two rules in main.py:

      1. No-duplicate-template: don't send the same template twice in a
         thread (kills "please attach resume" ping-pong loops).
      2. Outcome-terminal: once any TERMINAL_TEMPLATE_KEYS template has
         been sent, the bot stops auto-replying in that thread -- HR
         owns the conversation from there.

    Detection uses two signals, in priority order:

      a) X-Resume-Bot-Template header on outgoing messages (modern bot
         sends, post the gmail_client.send_reply update).
      b) Subject-line match against known template subjects (legacy
         fallback for threads where the bot replied before we started
         adding the header).

    Returns an empty set if the thread can't be loaded -- treated as
    'no bot replies yet'. This errs toward sending a reply, which is
    the safer default for a brand-new conversation."""
    try:
        thread = svc.users().threads().get(
            userId=user, id=thread_id, format="full",
        ).execute()
    except Exception as e:
        log.warning("Could not load thread %s: %s", thread_id, e)
        return set()

    sent: set[str] = set()
    user_lower = (user or "").lower()

    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        msg_headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in payload.get("headers", [])}

        # Only inspect messages WE (the bot) sent.
        from_addr = (msg_headers.get("from") or "").lower()
        if user_lower and user_lower not in from_addr:
            continue

        # (a) Modern detection: explicit header naming the template.
        tmpl = (msg_headers.get(BOT_TEMPLATE_HEADER.lower()) or "").strip()
        if tmpl in ALL_TEMPLATE_KEYS:
            sent.add(tmpl)
            continue

        # (b) Legacy detection: subject-line match. We compare against the
        # known template subject prefixes (stripped of any "Re:" prefix).
        subj = (msg_headers.get("subject") or "").strip()
        subj = re.sub(r"^(re|fwd?):\s*", "", subj, flags=re.IGNORECASE).lower()
        if subj.startswith("please resend with your resume"):
            sent.add("no_resume")
        elif subj.startswith("thanks for reaching out to "):
            sent.add("question")
        elif subj.startswith("thank you for your interest in "):
            sent.add("denied")
        elif subj.startswith("we'll keep your resume on file for "):
            sent.add("paused_match")

    return sent
