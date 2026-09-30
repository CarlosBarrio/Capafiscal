from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import and_
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.config import settings
from app.extractor import EXTRACTOR_NAME
from app.extractor import EXTRACTOR_VERSION
from app.extractor import extract_invoice
from app.extractor import normalize_amount
from app.extractor import normalize_tax_id
from app.models import AuditEvent
from app.models import Document
from app.models import ExtractionRun
from app.models import Invoice
from app.models import InvoiceTaxLine
from app.schemas import InvoiceUpdate


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso_date(
    value: str | date | None,
) -> date | None:
    if value is None:
        return None

    if isinstance(value, date):
        return value

    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def format_eur(value: Decimal) -> str:
    text = f"{value:,.2f}"

    return (
        text.replace(",", "X").replace(".", ",").replace("X", ".")
        + " €"
    )


def serialize_audit_value(
    value: Any,
) -> Any:
    if isinstance(value, Decimal):
        return format(value, ".2f")

    if isinstance(value, (date, datetime)):
        return value.isoformat()

    if isinstance(value, list):
        return [
            serialize_audit_value(item)
            for item in value
        ]

    if isinstance(value, dict):
        return {
            key: serialize_audit_value(item)
            for key, item in value.items()
        }

    return value


def add_audit_event(
    database: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: int | str,
    actor: str = "system",
    event_data: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        actor=actor,
        event_data=serialize_audit_value(
            event_data or {}
        ),
    )

    database.add(event)

    return event


def validate_invoice_values(
    *,
    supplier_name: str | None,
    supplier_tax_id: str | None,
    invoice_number: str | None,
    invoice_date: date | None,
    subtotal: Decimal | None,
    tax_total: Decimal | None,
    withholding_total: Decimal | None,
    surcharge_total: Decimal | None,
    total: Decimal | None,
    currency: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    messages: list[dict[str, Any]] = []

    required_fields = {
        "supplier_name": supplier_name,
        "supplier_tax_id": supplier_tax_id,
        "invoice_number": invoice_number,
        "invoice_date": invoice_date,
        "total": total,
        "currency": currency,
    }

    for field_name, field_value in required_fields.items():
        if field_value in (None, ""):
            messages.append(
                {
                    "code": "required_field_missing",
                    "field": field_name,
                    "severity": "error",
                    "message": (
                        f"Falta el campo obligatorio: "
                        f"{field_name}."
                    ),
                }
            )

    if total is not None and total < 0:
        messages.append(
            {
                "code": "negative_total",
                "field": "total",
                "severity": "warning",
                "message": (
                    "El total es negativo. Comprueba si "
                    "se trata de una factura rectificativa."
                ),
            }
        )

    if invoice_date is not None:
        today = date.today()

        if invoice_date > today:
            messages.append(
                {
                    "code": "future_invoice_date",
                    "field": "invoice_date",
                    "severity": "warning",
                    "message": (
                        "La fecha de la factura es futura."
                    ),
                }
            )

    amounts_available = (
        subtotal is not None
        and tax_total is not None
        and total is not None
    )

    if amounts_available:
        withholding = (
            withholding_total
            or Decimal("0.00")
        )
        surcharge = (
            surcharge_total
            or Decimal("0.00")
        )

        expected_total = (
            subtotal
            + tax_total
            + surcharge
            - withholding
        ).quantize(Decimal("0.01"))

        difference = abs(
            total - expected_total
        ).quantize(Decimal("0.01"))

        tolerance = Decimal(
            str(settings.amount_tolerance)
        )

        if difference > tolerance:
            messages.append(
                {
                    "code": "amount_mismatch",
                    "field": "total",
                    "severity": "error",
                    "message": (
                        "Los importes no cuadran. "
                        f"El total esperado es "
                        f"{format_eur(expected_total)} y el total "
                        f"extraído es {format_eur(total)}."
                    ),
                    "difference": format(
                        difference,
                        ".2f",
                    ),
                    "expected_total": format(
                        expected_total,
                        ".2f",
                    ),
                }
            )
    else:
        messages.append(
            {
                "code": "amounts_incomplete",
                "field": "amounts",
                "severity": "warning",
                "message": (
                    "No se puede comprobar el cuadre "
                    "porque faltan base, impuestos o total."
                ),
            }
        )

    error_messages = [
        message
        for message in messages
        if message["severity"] == "error"
    ]

    missing_messages = [
        message
        for message in messages
        if message["code"]
        == "required_field_missing"
    ]

    if missing_messages:
        return "INCOMPLETE", messages

    if error_messages:
        return "MISMATCH", messages

    if not amounts_available:
        return "INCOMPLETE", messages

    return "VALID", messages


def get_document(
    database: Session,
    document_id: int,
) -> Document | None:
    statement = (
        select(Document)
        .where(Document.id == document_id)
        .options(
            selectinload(Document.invoice).selectinload(
                Invoice.tax_lines
            ),
            selectinload(
                Document.extraction_runs
            ),
        )
    )

    return database.scalar(statement)


def get_invoice(
    database: Session,
    invoice_id: int,
) -> Invoice | None:
    statement = (
        select(Invoice)
        .where(Invoice.id == invoice_id)
        .options(
            selectinload(Invoice.tax_lines),
            selectinload(Invoice.document),
        )
    )

    return database.scalar(statement)


def field_value(
    result: dict[str, Any],
    field_name: str,
) -> Any:
    return (
        result
        .get("fields", {})
        .get(field_name, {})
        .get("value")
    )


def field_confidences(
    result: dict[str, Any],
) -> dict[str, Any]:
    confidences: dict[str, Any] = {}

    for name, field in result.get(
        "fields",
        {},
    ).items():
        confidences[name] = {
            "confidence": field.get(
                "confidence",
                0,
            ),
            "source": field.get("source"),
            "evidence": field.get(
                "evidence"
            ),
        }

    return confidences


def find_fiscal_duplicate(
    database: Session,
    *,
    invoice: Invoice,
) -> tuple[str, Invoice | None]:
    supplier_tax_id = normalize_tax_id(
        invoice.supplier_tax_id
    )

    invoice_number = normalize_invoice_number(
        invoice.invoice_number
    )

    if supplier_tax_id and invoice_number:
        strong_statement = (
            select(Invoice)
            .where(
                Invoice.id != invoice.id,
                Invoice.supplier_tax_id
                == supplier_tax_id,
                Invoice.invoice_number
                == invoice_number,
            )
            .order_by(Invoice.id.asc())
        )

        duplicate = database.scalar(
            strong_statement
        )

        if duplicate:
            return "STRONG", duplicate

    if (
        supplier_tax_id
        and invoice.invoice_date
        and invoice.total is not None
    ):
        probable_statement = (
            select(Invoice)
            .where(
                Invoice.id != invoice.id,
                Invoice.supplier_tax_id
                == supplier_tax_id,
                Invoice.invoice_date
                == invoice.invoice_date,
                Invoice.total == invoice.total,
            )
            .order_by(Invoice.id.asc())
        )

        duplicate = database.scalar(
            probable_statement
        )

        if duplicate:
            return "PROBABLE", duplicate

    return "NONE", None


def apply_duplicate_detection(
    database: Session,
    invoice: Invoice,
) -> None:
    duplicate_status, duplicate = (
        find_fiscal_duplicate(
            database,
            invoice=invoice,
        )
    )

    invoice.duplicate_status = (
        duplicate_status
    )
    invoice.duplicate_of_invoice_id = (
        duplicate.id
        if duplicate
        else None
    )


def update_document_status(
    document: Document,
    invoice: Invoice | None,
) -> None:
    if document.requires_ocr:
        document.status = "NEEDS_REVIEW"
        document.extraction_status = (
            "OCR_REQUIRED"
        )
        return

    if invoice is None:
        document.status = "NEEDS_REVIEW"
        document.extraction_status = (
            "COMPLETED"
        )
        return

    if invoice.review_status == "APPROVED":
        document.status = "APPROVED"
        return

    if invoice.review_status == "REJECTED":
        document.status = "REJECTED"
        return

    if invoice.duplicate_status != "NONE":
        document.status = "NEEDS_REVIEW"
        return

    if (
        invoice.validation_status == "VALID"
        and invoice.confidence
        >= settings.minimum_auto_confidence
    ):
        document.status = (
            "READY_FOR_APPROVAL"
        )
    else:
        document.status = "NEEDS_REVIEW"


def create_or_replace_tax_lines(
    database: Session,
    invoice: Invoice,
    tax_lines: list[dict[str, Any]],
) -> None:
    invoice.tax_lines.clear()
    database.flush()

    for line in tax_lines:
        invoice.tax_lines.append(
            InvoiceTaxLine(
                tax_type=(
                    line.get("tax_type")
                    or "IVA"
                ),
                tax_rate=normalize_amount(
                    line.get("tax_rate")
                ),
                tax_base=normalize_amount(
                    line.get("tax_base")
                ),
                tax_amount=normalize_amount(
                    line.get("tax_amount")
                ),
                source=line.get("source"),
                confidence=int(
                    line.get("confidence")
                    or 0
                ),
            )
        )


def persist_extraction_result(
    database: Session,
    *,
    document: Document,
    extraction_run: ExtractionRun,
    result: dict[str, Any],
) -> Invoice | None:
    document.page_count = result.get(
        "page_count"
    )
    document.requires_ocr = bool(
        result.get("requires_ocr")
    )

    extraction_run.status = "COMPLETED"
    extraction_run.raw_text = result.get(
        "raw_text"
    )
    extraction_run.result_json = {
        key: value
        for key, value in result.items()
        if key != "raw_text"
    }
    extraction_run.finished_at = utc_now()

    if document.requires_ocr:
        document.extraction_status = (
            "OCR_REQUIRED"
        )
        document.status = "NEEDS_REVIEW"
        return None

    document.extraction_status = "COMPLETED"

    if not result.get("is_invoice"):
        document.status = "NEEDS_REVIEW"

        add_audit_event(
            database,
            action=(
                "document.not_identified_as_invoice"
            ),
            entity_type="document",
            entity_id=document.id,
            actor="extractor",
            event_data={
                "invoice_likelihood": result.get(
                    "invoice_likelihood"
                ),
                "signals": result.get(
                    "signals",
                    [],
                ),
            },
        )

        return None

    invoice = document.invoice

    if invoice is None:
        invoice = Invoice(
            document=document,
        )
        database.add(invoice)

    invoice.supplier_name = field_value(
        result,
        "supplier_name",
    )
    invoice.supplier_tax_id = normalize_tax_id(
        field_value(
            result,
            "supplier_tax_id",
        )
    )

    invoice.customer_name = field_value(
        result,
        "customer_name",
    )
    invoice.customer_tax_id = normalize_tax_id(
        field_value(
            result,
            "customer_tax_id",
        )
    )

    invoice.invoice_number = normalize_invoice_number(
        field_value(
            result,
            "invoice_number",
        )
    )
    invoice.invoice_date = parse_iso_date(
        field_value(
            result,
            "invoice_date",
        )
    )
    invoice.due_date = parse_iso_date(
        field_value(
            result,
            "due_date",
        )
    )

    invoice.subtotal = normalize_amount(
        field_value(
            result,
            "subtotal",
        )
    )
    invoice.tax_total = normalize_amount(
        field_value(
            result,
            "tax_total",
        )
    )
    invoice.withholding_total = normalize_amount(
        field_value(
            result,
            "withholding_total",
        )
    )
    invoice.surcharge_total = normalize_amount(
        field_value(
            result,
            "surcharge_total",
        )
    )
    invoice.total = normalize_amount(
        field_value(
            result,
            "total",
        )
    )

    invoice.currency = (
        field_value(
            result,
            "currency",
        )
        or "EUR"
    )
    invoice.concept = field_value(
        result,
        "concept",
    )
    invoice.category = field_value(
        result,
        "category",
    )

    invoice.confidence = int(
        result.get(
            "overall_confidence",
            0,
        )
    )
    invoice.field_confidences = (
        field_confidences(result)
    )

    database.flush()

    create_or_replace_tax_lines(
        database,
        invoice,
        result.get(
            "tax_lines",
            [],
        ),
    )

    (
        invoice.validation_status,
        invoice.validation_messages,
    ) = validate_invoice_values(
        supplier_name=invoice.supplier_name,
        supplier_tax_id=invoice.supplier_tax_id,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        subtotal=invoice.subtotal,
        tax_total=invoice.tax_total,
        withholding_total=(
            invoice.withholding_total
        ),
        surcharge_total=(
            invoice.surcharge_total
        ),
        total=invoice.total,
        currency=invoice.currency,
    )

    database.flush()

    apply_duplicate_detection(
        database,
        invoice,
    )

    update_document_status(
        document,
        invoice,
    )

    return invoice


def process_document(
    database: Session,
    *,
    document: Document,
    file_path: Path,
    actor: str = "system",
) -> Invoice | None:
    document.status = "PROCESSING"
    document.extraction_status = "RUNNING"
    document.failure_reason = None

    extraction_run = ExtractionRun(
        document=document,
        extractor_name=EXTRACTOR_NAME,
        extractor_version=EXTRACTOR_VERSION,
        status="RUNNING",
    )

    database.add(extraction_run)
    database.flush()

    add_audit_event(
        database,
        action="document.processing.started",
        entity_type="document",
        entity_id=document.id,
        actor=actor,
        event_data={
            "extractor": (
                extraction_run.extractor_name
            ),
            "version": (
                extraction_run.extractor_version
            ),
        },
    )

    try:
        configured_tax_ids = (
            settings.company_tax_ids
            or settings.company_tax_id
        )

        result = extract_invoice(
            file_path,
            company_tax_id=configured_tax_ids,
            company_name=settings.company_name,
        )

        invoice = persist_extraction_result(
            database,
            document=document,
            extraction_run=extraction_run,
            result=result,
        )

        add_audit_event(
            database,
            action=(
                "document.processing.completed"
            ),
            entity_type="document",
            entity_id=document.id,
            actor="extractor",
            event_data={
                "is_invoice": result.get(
                    "is_invoice"
                ),
                "requires_ocr": result.get(
                    "requires_ocr"
                ),
                "overall_confidence": (
                    result.get(
                        "overall_confidence"
                    )
                ),
                "document_status": (
                    document.status
                ),
                "invoice_id": (
                    invoice.id
                    if invoice
                    else None
                ),
            },
        )

        database.commit()

        return invoice

    except Exception as error:
        document.status = "FAILED"
        document.extraction_status = "FAILED"
        document.failure_reason = str(error)

        extraction_run.status = "FAILED"
        extraction_run.error_message = str(error)
        extraction_run.finished_at = utc_now()

        add_audit_event(
            database,
            action="document.processing.failed",
            entity_type="document",
            entity_id=document.id,
            actor="extractor",
            event_data={
                "error": str(error),
            },
        )

        database.commit()

        raise


def invoice_snapshot(
    invoice: Invoice,
) -> dict[str, Any]:
    return {
        "supplier_name": invoice.supplier_name,
        "supplier_tax_id": (
            invoice.supplier_tax_id
        ),
        "customer_name": invoice.customer_name,
        "customer_tax_id": (
            invoice.customer_tax_id
        ),
        "invoice_number": (
            invoice.invoice_number
        ),
        "invoice_date": (
            invoice.invoice_date
        ),
        "due_date": invoice.due_date,
        "subtotal": invoice.subtotal,
        "tax_total": invoice.tax_total,
        "withholding_total": (
            invoice.withholding_total
        ),
        "surcharge_total": (
            invoice.surcharge_total
        ),
        "total": invoice.total,
        "currency": invoice.currency,
        "concept": invoice.concept,
        "category": invoice.category,
        "tax_lines": [
            {
                "tax_type": line.tax_type,
                "tax_rate": line.tax_rate,
                "tax_base": line.tax_base,
                "tax_amount": line.tax_amount,
            }
            for line in invoice.tax_lines
        ],
    }


def update_invoice(
    database: Session,
    *,
    invoice: Invoice,
    payload: InvoiceUpdate,
    actor: str = "user",
) -> Invoice:
    before = invoice_snapshot(invoice)

    changes = payload.model_dump(
        exclude_unset=True,
    )
    tax_lines = changes.pop(
        "tax_lines",
        None,
    )

    if "supplier_tax_id" in changes:
        changes["supplier_tax_id"] = (
            normalize_tax_id(
                changes["supplier_tax_id"]
            )
        )

    if "customer_tax_id" in changes:
        changes["customer_tax_id"] = (
            normalize_tax_id(
                changes["customer_tax_id"]
            )
        )

    amount_fields = (
        "subtotal",
        "tax_total",
        "withholding_total",
        "surcharge_total",
        "total",
    )

    for amount_field in amount_fields:
        if amount_field in changes:
            changes[amount_field] = (
                normalize_amount(
                    changes[amount_field]
                )
            )

    for field_name, field_value in changes.items():
        setattr(
            invoice,
            field_name,
            field_value,
        )

    if tax_lines is not None:
        create_or_replace_tax_lines(
            database,
            invoice,
            tax_lines,
        )

    invoice.review_status = "PENDING"
    invoice.approved_at = None
    invoice.rejected_at = None
    invoice.rejection_reason = None

    (
        invoice.validation_status,
        invoice.validation_messages,
    ) = validate_invoice_values(
        supplier_name=invoice.supplier_name,
        supplier_tax_id=invoice.supplier_tax_id,
        invoice_number=invoice.invoice_number,
        invoice_date=invoice.invoice_date,
        subtotal=invoice.subtotal,
        tax_total=invoice.tax_total,
        withholding_total=(
            invoice.withholding_total
        ),
        surcharge_total=(
            invoice.surcharge_total
        ),
        total=invoice.total,
        currency=invoice.currency,
    )

    database.flush()

    apply_duplicate_detection(
        database,
        invoice,
    )

    update_document_status(
        invoice.document,
        invoice,
    )

    after = invoice_snapshot(invoice)

    changed_fields = {
        field_name: {
            "before": before.get(field_name),
            "after": after.get(field_name),
        }
        for field_name in after
        if before.get(field_name)
        != after.get(field_name)
    }

    add_audit_event(
        database,
        action="invoice.updated",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=actor,
        event_data={
            "changed_fields": changed_fields,
            "validation_status": (
                invoice.validation_status
            ),
        },
    )

    database.commit()
    database.refresh(invoice)

    return invoice


def approve_invoice(
    database: Session,
    *,
    invoice: Invoice,
    actor: str = "user",
) -> Invoice:
    if invoice.validation_status != "VALID":
        raise ValueError(
            "La factura no puede aprobarse porque "
            "tiene errores o campos obligatorios "
            "pendientes."
        )

    if invoice.duplicate_status == "STRONG":
        raise ValueError(
            "La factura coincide con otro CIF y "
            "número de factura. Debes resolver el "
            "posible duplicado antes de aprobar."
        )

    invoice.review_status = "APPROVED"
    invoice.approved_at = utc_now()
    invoice.rejected_at = None
    invoice.rejection_reason = None

    update_document_status(
        invoice.document,
        invoice,
    )

    add_audit_event(
        database,
        action="invoice.approved",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=actor,
        event_data={
            "document_id": (
                invoice.document_id
            ),
            "total": invoice.total,
            "validation_status": (
                invoice.validation_status
            ),
        },
    )

    database.commit()
    database.refresh(invoice)

    return invoice


def reject_invoice(
    database: Session,
    *,
    invoice: Invoice,
    reason: str,
    actor: str = "user",
) -> Invoice:
    invoice.review_status = "REJECTED"
    invoice.rejected_at = utc_now()
    invoice.approved_at = None
    invoice.rejection_reason = reason

    update_document_status(
        invoice.document,
        invoice,
    )

    add_audit_event(
        database,
        action="invoice.rejected",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=actor,
        event_data={
            "document_id": (
                invoice.document_id
            ),
            "reason": reason,
        },
    )

    database.commit()
    database.refresh(invoice)

    return invoice

def normalize_invoice_number(
    value: str | None,
) -> str | None:
    if not value:
        return None

    normalized = value.strip().upper()
    normalized = " ".join(normalized.split())

    return normalized or None


def reopen_invoice(
    database: Session,
    *,
    invoice: Invoice,
    reason: str,
    actor: str = "user",
) -> Invoice:
    if invoice.review_status not in {"APPROVED", "REJECTED"}:
        raise ValueError(
            "Solo pueden reabrirse facturas aprobadas o rechazadas."
        )

    previous_status = invoice.review_status

    invoice.review_status = "PENDING"
    invoice.approved_at = None
    invoice.rejected_at = None
    invoice.rejection_reason = None

    update_document_status(
        invoice.document,
        invoice,
    )

    add_audit_event(
        database,
        action="invoice.reopened",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=actor,
        event_data={
            "document_id": invoice.document_id,
            "previous_status": previous_status,
            "reason": reason,
        },
    )

    database.commit()
    database.refresh(invoice)

    return invoice


def set_invoice_payment(
    database: Session,
    *,
    invoice: Invoice,
    paid: bool,
    paid_at: date | None,
    payment_method: str | None,
    actor: str = "user",
) -> Invoice:
    if paid and invoice.review_status != "APPROVED":
        raise ValueError(
            "Solo pueden marcarse como pagadas las facturas aprobadas."
        )

    if paid:
        invoice.paid_at = paid_at or date.today()
        invoice.payment_method = payment_method
        action = "invoice.paid"
    else:
        invoice.paid_at = None
        invoice.payment_method = None
        action = "invoice.payment_cancelled"

    add_audit_event(
        database,
        action=action,
        entity_type="invoice",
        entity_id=invoice.id,
        actor=actor,
        event_data={
            "document_id": invoice.document_id,
            "paid_at": invoice.paid_at,
            "payment_method": invoice.payment_method,
            "total": invoice.total,
        },
    )

    database.commit()
    database.refresh(invoice)

    return invoice

