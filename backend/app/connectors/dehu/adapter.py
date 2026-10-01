"""
Adaptador DEHú → entrada común.

Una notificación de la DEHú trae dos cosas: los METADATOS (identificador,
organismo emisor, concepto, titular, fecha de puesta a disposición y fecha de
acceso) y, si se ha accedido, el PDF del acto. El adaptador:

    1. guarda el PDF como documento y lo lee (igual que una subida);
    2. aplica las fechas de la DEHú, que son las oficiales: con la fecha de
       acceso el plazo deja de ser una estimación;
    3. si el PDF no se puede leer (escaneado) o no hay PDF, la notificación se
       crea con los metadatos: nunca se pierde;
    4. entrega el evento a la entrada común con identificador «dehu:<id>»:
       la misma notificación no se procesa dos veces.

El transporte (cómo se obtienen las notificaciones) va aparte: ver client.py.
"""
from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Document
from app.models import FiscalNotification


@dataclass
class DehuItem:
    identifier: str
    issuer_name: str
    subject: str
    holder_tax_id: str | None = None
    kind: str = "notificacion"  # notificacion | comunicacion
    available_at: date | None = None
    accessed_at: date | None = None
    pdf: bytes | None = None
    filename: str | None = None

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> "DehuItem":
        def as_date(value: Any) -> date | None:
            return date.fromisoformat(str(value)[:10]) if value else None

        return cls(
            identifier=str(data["identifier"]), issuer_name=data.get("issuer") or "", subject=data.get("subject") or "",
            holder_tax_id=data.get("holder_tax_id"), kind=data.get("kind") or "notificacion",
            available_at=as_date(data.get("available_at")), accessed_at=as_date(data.get("accessed_at")),
            pdf=base64.b64decode(data["pdf_base64"]) if data.get("pdf_base64") else None, filename=data.get("filename"),
        )


def issuer_code(name: str) -> str:
    from app.extractor import normalize_search_text
    from app.notification_service import detect_issuer

    return detect_issuer(normalize_search_text(name)) or "OTRO"


def store_pdf(database: Session, item: DehuItem) -> tuple[Document, bool]:
    from app.invoice_service import add_audit_event
    from app.invoice_service import process_document

    digest = hashlib.sha256(item.pdf).hexdigest()
    existing = database.scalar(select(Document).where(Document.sha256 == digest).limit(1))
    if existing is not None:
        return existing, False
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.pdf"
    path = settings.upload_dir / stored
    path.write_bytes(item.pdf)
    document = Document(
        original_filename=(item.filename or f"DEHU_{item.identifier}.pdf")[:255], stored_filename=stored, sha256=digest, mime_type="application/pdf",
        extension=".pdf", size_bytes=len(item.pdf), source="dehu", source_provider="dehu", external_id=item.identifier[:255], is_demo=False,
        status="RECEIVED", extraction_status="PENDING", requires_ocr=False,
    )
    database.add(document)
    database.flush()
    add_audit_event(database, action="document.received_from_dehu", entity_type="document", entity_id=document.id, actor="conector-dehu",
                    event_data={"identifier": item.identifier, "issuer": item.issuer_name, "subject": item.subject})
    database.commit()
    try:
        process_document(database, document=document, file_path=Path(path), actor="conector-dehu")
    except Exception:
        database.rollback()
    return database.get(Document, document.id), True


def apply_metadata(database: Session, notification: FiscalNotification, item: DehuItem) -> None:
    """Las fechas de la DEHú son las oficiales: mandan sobre lo que se lea en el PDF."""
    from app.notification_service import recompute_deadline

    if item.available_at:
        notification.available_at = item.available_at
    if item.accessed_at:
        notification.notified_at = item.accessed_at
    if notification.issuer == "OTRO":
        notification.issuer = issuer_code(item.issuer_name)
    if not notification.reference:
        notification.reference = item.identifier[:100]
    recompute_deadline(notification)


def metadata_notification(item: DehuItem) -> dict[str, Any]:
    from app.notification_service import NOTIFICATION_TYPES
    from app.notification_service import classify_type

    kind = "COMUNICACION" if item.kind == "comunicacion" else classify_type(item.subject)[0]
    return {
        "issuer": issuer_code(item.issuer_name), "notification_type": kind,
        "title": f"{NOTIFICATION_TYPES.get(kind, 'Notificación')} · {item.issuer_name}"[:255],
        "reference": item.identifier[:100], "summary": item.subject[:1000],
        "available_at": item.available_at, "notified_at": item.accessed_at,
    }


def ingest_dehu(database: Session, item: DehuItem) -> dict[str, Any]:
    """Una notificación de la DEHú → evento de la entrada común (idempotente)."""
    from app.agents.intake import ingest
    from app.agents.intake import serialize_event
    from app.agents.intake import with_retry
    from app.notification_service import create_notification

    external_id = f"dehu:{item.identifier}"
    document = None
    if item.pdf:
        document, _created = store_pdf(database, item)
        notification = database.scalar(select(FiscalNotification).where(FiscalNotification.document_id == document.id))
        if notification is None:
            # PDF ilegible o sin clasificar: la notificación existe igualmente, con los metadatos.
            notification = create_notification(database, document=document, data=metadata_notification(item), actor="conector-dehu")
        apply_metadata(database, notification, item)
        database.commit()

    def work():
        if document is not None:
            result = ingest(database, source="dehu", external_id=external_id, kind="document",
                            payload={"document_id": document.id, "dehu": {"identifier": item.identifier, "issuer": item.issuer_name, "subject": item.subject}})
        else:
            result = ingest(database, source="dehu", external_id=external_id, kind="notification",
                            payload={"notification": {key: (value.isoformat() if isinstance(value, date) else value) for key, value in metadata_notification(item).items()}})
        database.commit()
        return result

    event, duplicate, case = with_retry(database, work)
    return {"identifier": item.identifier, "duplicate": duplicate, "document_id": document.id if document else None,
            "event": serialize_event(event), "case_id": case.id if case else None, "case_code": case.code if case else None}
