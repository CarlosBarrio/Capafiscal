"""Lee un correo (.eml) y extrae lo que interesa: remitente, asunto, adjuntos."""
from __future__ import annotations

import email
import hashlib
from dataclasses import dataclass
from dataclasses import field
from email import policy
from email.utils import parsedate_to_datetime
from datetime import datetime


@dataclass
class Attachment:
    filename: str
    content_type: str
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass
class ParsedEmail:
    message_id: str
    sender: str | None
    subject: str | None
    received_at: datetime | None
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def parse_eml(raw: bytes) -> ParsedEmail:
    message = email.message_from_bytes(raw, policy=policy.default)
    message_id = (message.get("Message-ID") or "").strip().strip("<>")
    if not message_id:
        # Sin Message-ID: la huella del correo sirve de identificador estable.
        message_id = "sha256:" + hashlib.sha256(raw).hexdigest()

    received_at = None
    if message.get("Date"):
        try:
            received_at = parsedate_to_datetime(message["Date"])
        except (TypeError, ValueError):
            received_at = None

    body_part = message.get_body(preferencelist=("plain", "html"))
    body = body_part.get_content() if body_part is not None else ""

    attachments = []
    for part in message.iter_attachments():
        filename = part.get_filename()
        if not filename:
            continue
        content = part.get_payload(decode=True) or b""
        if content:
            attachments.append(Attachment(filename=filename, content_type=part.get_content_type(), content=content))

    return ParsedEmail(
        message_id=message_id[:200],
        sender=str(message.get("From") or "") or None,
        subject=str(message.get("Subject") or "") or None,
        received_at=received_at,
        body=body[:5000],
        attachments=attachments,
    )
