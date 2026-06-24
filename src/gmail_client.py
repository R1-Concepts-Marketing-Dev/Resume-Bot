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

BOT_TEMPLATE_HEADER = "X-R1-Ref"
LEGACY_BOT_HEADER = "X-Resume-Bot-Template"

TEMPLATE_KEY_TO_CODE = {
    "no_resume":    "nr",
    "question":     "q",
    "denied":       "d",
    "paused_match": "pm",
}
TEMPLATE_CODE_TO_KEY = {v: k for k, v in TEMPLATE_KEY_TO_CODE.items()}

TERMINAL_TEMPLATE_KEYS = frozenset({"denied", "paused_match"})

ALL_TEMPLATE_KEYS = frozenset({"no_resume", "question", "denied", "paused_match"})

_AUTO_REPLY_HEADER_SIGNALS = {
    "auto-submitted":  ("auto-replied", "auto-generated", "auto-notified"),
    "x-autoreply":     None,
    "x-autorespond":   None,
    "x-autoresponder": None,
    "precedence":      ("auto_reply", "junk"),
}

_OOO_SUBJECT_PATTERNS = [
    re.compile(r"\bout[\s-]of[\s-]office\b", re.IGNORECASE),
    re.compile(r"\bauto[\s-]?reply\b", re.IGNORECASE),
    re.compile(r"\bautomatic\s+reply\b", re.IGNORECASE),
    re.compile(r"\bOOO\b"),
    re.compile(r"\bon\s+vacation\b", re.IGNORECASE),
    re.compile(r"\baway from (the\s+)?office\b", re.IGNORECASE),
]

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

_CALENDAR_MIME_TYPES = {
    "text/calendar",
    "application/ics",
    "application/vnd.icalendar",
}

# Outcome labels applied to threads. Names use Gmail's "Parent/Child"
# convention so they nest in the sidebar.
#   "Handled/*"   = bot handled the thread; INBOX label removed (archived).
#   "For HR"   = umbrella label applied to every stuck thread so HR can
#                click it in the sidebar and see the full queue.
#   "For HR/*" = sub-label naming the specific reason it's stuck.
OUTCOME_LABELS = {
    "qualified":         "Handled/Qualified",
    "needs_review":      "Handled/Needs review",
    "not_qualified":     "Handled/Not qualified",
    "pending_paused":    "Handled/Paused role",
    "unreadable":        "Handled/Unreadable",
    "not_a_resume":      "Handled/Not a resume",
    "closed":            "Handled/Closed",
    "needs_human":       "For HR",
    "for_hr":            "For HR",
    "for_hr_question":         "For HR/Question?",
    "for_hr_indeed_fetch":     "For HR/Indeed fetch failed",
    "for_hr_loop":             "For HR/Looping sender",
}


REASON_TYPE_TO_HR_SUBLABEL = {
    "low_confidence": "for_hr_question",
    "indeed_fetch":   "for_hr_indeed_fetch",
    "loop":           "for_hr_loop",
}


LEGACY_LABEL_RENAMES = {
    "Resume Bot/Qualified":          "Handled/Qualified",
    "Resume Bot/Needs Review":       "Handled/Needs review",
    "Resume Bot/Not Qualified":      "Handled/Not qualified",
    "Resume Bot/Pending Paused Role":"Handled/Paused role",
    "Resume Bot/Unreadable":         "Handled/Unreadable",
    "Resume Bot/Not A Resume":       "Handled/Not a resume",
    "Resume Bot/Closed":             "Handled/Closed",
    "Resume Bot/Needs Human":        "For HR",
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
    has_calendar_invite: bool = False

    @property
    def thread_link(self) -> str:
        return (
            "https://mail.google.com/mail/"
            "?authuser=jobs@r1concepts.com"
            f"#inbox/{self.thread_id}"
        )

    @property
    def has_resume(self) -> bool:
        return bool(self.attachments)

    @property
    def is_auto_response(self) -> bool:
        for hdr, allowed_substrings in _AUTO_REPLY_HEADER_SIGNALS.items():
            val = (self.headers.get(hdr) or "").lower()
            if not val:
                continue
            if allowed_substrings is None:
                return True
            if any(s in val for s in allowed_substrings):
                return True
        return False

    @property
    def subject_indicates_ooo(self) -> bool:
        s = self.subject or ""
        return any(p.search(s) for p in _OOO_SUBJECT_PATTERNS)


@dataclass(frozen=True)
class ThreadHistory:
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


def migrate_legacy_labels(svc, user: str) -> int:
    """One-time idempotent rename of pre-redesign labels.

    Maps each "Resume Bot/X" label to its new "Handled/X" or "For HR" name
    via the Gmail labels PATCH endpoint. Renaming preserves the label
    ID so threads previously labeled keep their label under the new
    name -- no thread-level re-labeling needed. Safe to re-run."""
    try:
        labels = svc.users().labels().list(userId=user).execute().get("labels", [])
    except Exception as e:
        log.warning("migrate_legacy_labels: could not list labels: %s", e)
        return 0
    by_name = {lbl["name"]: lbl["id"] for lbl in labels}
    renamed = 0
    for old_name, new_name in LEGACY_LABEL_RENAMES.items():
        if old_name not in by_name:
            continue
        if new_name in by_name:
            log.info("migrate_legacy_labels: skip %r -> %r (target exists)",
                     old_name, new_name)
            continue
        try:
            svc.users().labels().patch(
                userId=user, id=by_name[old_name],
                body={"name": new_name},
            ).execute()
            log.info("migrate_legacy_labels: renamed %r -> %r",
                     old_name, new_name)
            renamed += 1
        except Exception as e:
            log.warning("migrate_legacy_labels: could not rename %r: %s",
                        old_name, e)
    try:
        labels_after = svc.users().labels().list(
            userId=user).execute().get("labels", [])
        children_remain = any(
            lbl["name"].startswith("Resume Bot/") for lbl in labels_after
        )
        if "Resume Bot" in by_name and not children_remain:
            svc.users().labels().delete(
                userId=user, id=by_name["Resume Bot"]).execute()
            log.info("migrate_legacy_labels: removed empty 'Resume Bot' parent")
    except Exception as e:
        log.warning("migrate_legacy_labels: parent cleanup failed: %s", e)
    return renamed


def ensure_outcome_labels(svc, user: str) -> dict[str, str]:
    """Create the For HR / Done label tree if missing. Idempotent."""
    migrate_legacy_labels(svc, user)
    existing = {lbl["name"]: lbl["id"]
                for lbl in svc.users().labels().list(userId=user).execute().get("labels", [])}

    def _create(name: str) -> str:
        created = svc.users().labels().create(
            userId=user,
            body={"name": name,
                  "labelListVisibility": "labelShow",
                  "messageListVisibility": "show"},
        ).execute()
        existing[name] = created["id"]
        log.info("created Gmail label %r", name)
        return created["id"]

    for parent in ("Handled", "For HR"):
        if parent not in existing:
            _create(parent)

    out: dict[str, str] = {}
    for key, name in OUTCOME_LABELS.items():
        if name in existing:
            out[key] = existing[name]
            continue
        out[key] = _create(name)
    return out


def list_unprocessed(svc, user: str, processed_label: str, max_results: int,
                     start_date: str = "") -> list[str]:
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


_ANCHOR_TAG_RE = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*['"]([^'"]+)['"][^>]*?>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    def inline(match):
        href = (match.group(1) or "").strip()
        text = re.sub(r"<[^>]+>", " ", match.group(2) or "").strip()
        if href and text and href != text:
            return f"{text} {href}"
        return href or text
    inlined = _ANCHOR_TAG_RE.sub(inline, html)
    stripped = re.sub(r"<[^>]+>", " ", inlined)
    return re.sub(r"[ \t]+", " ", stripped).strip()


def _extract_body_text(payload: dict) -> str:
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        if mime.startswith("text/plain"):
            plain_chunks.append(_decode_body(part))
        elif mime.startswith("text/html"):
            html_chunks.append(_decode_body(part))
    parts: list[str] = []
    if plain_chunks:
        parts.append("\n".join(plain_chunks).strip())
    if html_chunks:
        parts.append(_html_to_text("\n".join(html_chunks)))
    return "\n\n".join(p for p in parts if p).strip()


def _has_calendar_invite(payload: dict) -> bool:
    for part in _walk_parts(payload):
        mime = (part.get("mimeType") or "").lower()
        if mime in _CALENDAR_MIME_TYPES:
            return True
        filename = (part.get("filename") or "").lower()
        if filename.endswith(".ics"):
            return True
    return False


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
        has_calendar_invite=_has_calendar_invite(payload),
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


def flag_needs_human(svc, user: str, msg_id: str, *,
                     needs_human_label_id: str,
                     processed_label_id: str = "",
                     extra_label_ids: list | None = None) -> None:
    """Apply the For HR umbrella label so HR sees the thread in their
    inbox via the "For HR" sidebar entry. Does NOT remove INBOX -- the
    point is that HR keeps seeing the thread.

    Pass extra_label_ids to also attach a "For HR/<reason>" sub-label
    so HR can see WHY at a glance.
    """
    label_ids = [needs_human_label_id]
    if processed_label_id:
        label_ids.append(processed_label_id)
    if extra_label_ids:
        for lid in extra_label_ids:
            if lid and lid not in label_ids:
                label_ids.append(lid)
    try:
        svc.users().messages().modify(
            userId=user, id=msg_id,
            body={"addLabelIds": label_ids},
        ).execute()
    except Exception as e:
        log.warning("Failed to flag msg=%s for human review: %s", msg_id, e)


def send_reply(svc, user: str, *, to: str, subject: str, body: str,
               thread_id: str = "", in_reply_to_msg_id: str = "",
               template_key: str = "") -> None:
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


def strip_quoted_text(body: str) -> str:
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


_LEGACY_SUBJECT_PREFIXES = {
    "please resend with your resume":  "no_resume",
    "thanks for reaching out to ":     "question",
    "thank you for your interest in ": "denied",
    "we'll keep your resume on file for ": "paused_match",
}


def get_thread_history(svc, user: str, thread_id: str,
                       internal_domains) -> ThreadHistory:
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

        code = (msg_headers.get(BOT_TEMPLATE_HEADER.lower()) or "").strip()
        if code and code in TEMPLATE_CODE_TO_KEY:
            sent.add(TEMPLATE_CODE_TO_KEY[code])
            continue

        legacy_tmpl = (msg_headers.get(LEGACY_BOT_HEADER.lower()) or "").strip()
        if legacy_tmpl in ALL_TEMPLATE_KEYS:
            sent.add(legacy_tmpl)
            continue

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

        hr_manual = True

    return ThreadHistory(
        bot_templates_sent=frozenset(sent),
        hr_replied_manually=hr_manual,
    )


def get_sent_templates_in_thread(svc, user: str, thread_id: str) -> set:
    h = get_thread_history(svc, user, thread_id, internal_domains=())
    return set(h.bot_templates_sent)
