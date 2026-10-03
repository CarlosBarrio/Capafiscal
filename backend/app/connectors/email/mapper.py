"""
Del correo a la entrada común.

Cada adjunto útil (PDF o texto) se guarda como documento —igual que una
subida manual: misma deduplicación por huella y misma lectura— y entra como
evento con identificador estable «<Message-ID>#<huella del adjunto>». Si el
mismo correo llega dos veces, no se procesa dos veces.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.email.parser import Attachment
from app.connectors.email.parser import ParsedEmail
from app.invoice_service import add_audit_event
from app.models import Document

logger = logging.getLogger(__name__)


def store_document(database: Session, attachment: Attachment, *, provider: str, message: ParsedEmail) -> tuple[Document, bool]:
    """Guarda el adjunto como documento y lo lee. Devuelve (documento, ¿es nuevo?)."""
    from app.invoice_service import process_document

    existing = database.scalar(select(Document).where(Document.sha256 == attachment.sha256).limit(1))
    if existing is not None:
        return existing, False

    extension = Path(attachment.filename).suffix.lower()
    stored_filename = f"{uuid.uuid4().hex}{extension}"
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    path = settings.upload_dir / stored_filename
    path.write_bytes(attachment.content)

    document = Document(
        original_filename=attachment.filename[:255],
        stored_filename=stored_filename,
        sha256=attachment.sha256,
        mime_type=attachment.content_type,
        extension=extension,
        size_bytes=len(attachment.content),
        source="email",
        source_provider=provider,
        external_id=message.message_id[:255],
        is_demo=False,
        status="RECEIVED",
        extraction_status="PENDING",
        requires_ocr=False,
        failure_reason=None,
    )
    database.add(document)
    database.flush()
    add_audit_event(
        database,
        action="document.received_by_email",
        entity_type="document",
        entity_id=document.id,
        actor="conector-correo",
        event_data={"from": message.sender, "subject": message.subject, "message_id": message.message_id, "filename": attachment.filename},
    )
    database.commit()
    try:
        process_document(database, document=document, file_path=path, actor="conector-correo")
    except Exception:
        database.rollback()
        logger.exception("No se pudo leer el adjunto %s", attachment.filename)
    return database.get(Document, document.id), True


def ingest_email(database: Session, message: ParsedEmail, *, provider: str = "importacion") -> dict[str, Any]:
    """Convierte un correo en eventos de la entrada común."""
    from app.agents.intake import ingest
    from app.agents.intake import serialize_event
    from app.agents.intake import with_retry

    allowed = settings.allowed_extension_set
    results: list[dict[str, Any]] = []
    skipped: list[str] = []

    for attachment in message.attachments:
        extension = Path(attachment.filename).suffix.lower()
        if extension not in allowed:
            skipped.append(f"{attachment.filename} (formato no admitido)")
            continue
        if len(attachment.content) > settings.max_upload_size:
            skipped.append(f"{attachment.filename} (demasiado grande)")
            continue
        document, created = store_document(database, attachment, provider=provider, message=message)
        def work(document_id=document.id, attachment=attachment):
            result = ingest(
                database,
                source="email",
                external_id=f"{message.message_id}#{attachment.sha256[:16]}",
                kind="document",
                payload={
                    "document_id": document_id,
                    "message_id": message.message_id,
                    "from": message.sender,
                    "subject": message.subject,
                    "filename": attachment.filename,
                },
            )
            database.commit()
            return result

        event, duplicate, case = with_retry(database, work)
        document = database.get(Document, document.id)
        results.append(
            {
                "filename": attachment.filename,
                "document_id": document.id,
                "new_document": created,
                "duplicate": duplicate,
                "event": serialize_event(event),
                "case_id": case.id if case is not None else None,
                "case_code": case.code if case is not None else None,
                "kind": document.kind,
            }
        )

    return {
        "message_id": message.message_id,
        "from": message.sender,
        "subject": message.subject,
        "attachments": results,
        "skipped": skipped,
    }
