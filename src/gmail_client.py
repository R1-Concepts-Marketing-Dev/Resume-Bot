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

# Outcome labels the bot applies to processed emails. Nested under a
# "Resume Bot" parent so they group together in Gmail's sidebar.
OUTCOME_LABELS = {
    "qualified":       "Resume Bot/Qualified",
    "needs_review":    "Resume Bot/Needs Review",
    "not_qualified":   "Resume Bot/Not Qualified",
    "pending_paused":  "Resume Bot/Pending Paused Role",
    "unreadable":      "Resume Bot/Unreadable",
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

    @property
    def thread_link(self) -> str:
        return f"https://mail.google.com/mail/u/0/#inbox/{self.thread_id}"

    @property
    def has_resume(self) -> bool:
        return bool(self.attachments)


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
    """Create the 5 outcome labels under a 'Resume Bot' parent if they don't
    already exist. Returns a mapping from outcome key (e.g. 'qualified') to
    the Gmail label id."""
    existing = {lbl["name"]: lbl["id"]
                for lbl in svc.users().labels().list(userId=user).execute().get("labels", [])}
    # Parent label first so the children nest cleanly in Gmail's sidebar.
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


def list_unprocessed(svc, user: str, processed_label: str, max_results: int) -> list[str]:
    query = f"in:inbox -label:{processed_label}"
    resp = svc.users().messages().list(
        userId=user, q=query, maxResults=max_results
    ).execute()
    return [m["id"] for m in resp.get("messages", [])]


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

    return Message(
        id=msg_id,
        thread_id=msg.get("threadId", ""),
        subject=_header(payload, "Subject"),
        sender=sender_raw,
        sender_email=sender_email,
        sender_name=sender_name or (sender_email.split("@")[0] if sender_email else ""),
        body_text=_extract_body_text(payload),
        attachments=attachments,
    )


def mark_processed(svc, user: str, msg_id: str, label_id: str) -> None:
    """Apply the 'I've seen this' label without archiving. Used for emails
    the bot looked at but had no resume attachment to act on."""
    svc.users().messages().modify(
        userId=user, id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()


def archive_with_outcome(svc, user: str, msg_id: str, *,
                         processed_label_id: str, outcome_label_id: str) -> None:
    """Apply both the bot-seen label and the outcome label, then remove
    INBOX so the email is archived. The outcome label keeps the thread
    findable in Gmail's sidebar; archiving keeps the inbox clean."""
    svc.users().messages().modify(
        userId=user, id=msg_id,
        body={
            "addLabelIds": [processed_label_id, outcome_label_id],
            "removeLabelIds": ["INBOX"],
        },
    ).execute()


def send_reply(svc, user: str, *, to: str, subject: str, body: str,
               thread_id: str = "", in_reply_to_msg_id: str = "") -> None:
    mime = MIMEText(body, "plain", "utf-8")
    mime["To"] = to
    mime["From"] = user
    mime["Subject"] = subject
    if in_reply_to_msg_id:
        mime["In-Reply-To"] = in_reply_to_msg_id
        mime["References"] = in_reply_to_msg_id

    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
    body_obj: dict = {"raw": raw}
    if thread_id:
        body_obj["threadId"] = thread_id

    svc.users().messages().send(userId=user, body=body_obj).execute()
