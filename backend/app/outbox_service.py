"""
Bandeja de salida: el agente redacta los correos (facturas, reclamaciones,
recibos de nómina, resumen diario, cierre para la gestoría) y una persona
los revisa y los envía.

- Con SMTP configurado (.env), «Enviar» los manda directamente.
- Sin SMTP, se descargan como borrador .eml con los adjuntos ya puestos:
  Outlook o Thunderbird lo abren listo para pulsar «Enviar».
"""
from __future__ import annotations

from app import clock
import re
import smtplib
import ssl
from datetime import date
from datetime import datetime
from datetime import timezone
from email.message import EmailMessage
from email.utils import formataddr
from email.utils import make_msgid
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.invoice_service import add_audit_event
from app.models import CompanyProfile
from app.models import Document
from app.models import OutboxMessage
from app.models import SalesInvoice

KIND_LABELS = {
    "INVOICE": "Factura",
    "DUNNING": "Reclamación de cobro",
    "PAYSLIP": "Recibo de nómina",
    "DIGEST": "Resumen del agente",
    "ADVISOR": "Gestoría",
    "REQUEST": "Petición de documentación",
    "OTHER": "Mensaje",
}
STATUS_LABELS = {"DRAFT": "Pendiente de revisar", "SENT": "Enviado", "DISCARDED": "Descartado", "FAILED": "Error al enviar"}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class OutboxError(ValueError):
    pass


def format_iban(value: str | None) -> str:
    compact = (value or "").replace(" ", "").upper()
    return " ".join(compact[index:index + 4] for index in range(0, len(compact), 4))


def smtp_configured() -> bool:
    return bool(settings.smtp_host and (settings.smtp_from or settings.smtp_user))


def company_profile(database: Session) -> CompanyProfile | None:
    return database.scalar(select(CompanyProfile).limit(1))


def sender(database: Session) -> tuple[str, str]:
    company = company_profile(database)
    name = (company.name if company else None) or settings.app_name
    address = settings.smtp_from or settings.smtp_user or (company.email if company else None) or "no-reply@capafiscal.local"
    return name, address


def create_message(
    database: Session,
    *,
    kind: str,
    subject: str,
    body: str,
    to_email: str | None,
    to_name: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    level: int | None = None,
    created_by: str = "agent",
) -> OutboxMessage:
    message = OutboxMessage(
        kind=kind,
        status="DRAFT",
        subject=subject[:255],
        body=body,
        to_email=(to_email or "").strip() or None,
        to_name=to_name,
        attachments=attachments or [],
        entity_type=entity_type,
        entity_id=entity_id,
        level=level,
        created_by=created_by,
    )
    database.add(message)
    database.flush()
    return message


# ---------------------------------------------------------------------
# Adjuntos (se generan al enviar para que siempre estén actualizados)
# ---------------------------------------------------------------------


def resolve_attachment(database: Session, attachment: dict[str, Any]) -> tuple[str, str, bytes]:
    kind = attachment.get("type")
    filename = attachment.get("filename") or "adjunto.pdf"

    if kind == "sales_invoice":
        from app.sales_service import invoice_pdf_bytes

        invoice = database.get(SalesInvoice, attachment["id"])
        if invoice is None:
            raise OutboxError("La factura adjunta ya no existe.")
        return filename, "application/pdf", invoice_pdf_bytes(database, invoice)

    if kind == "payslip":
        from app.models import Payslip
        from app.payroll_service import build_payslips_pdf

        payslip = database.get(Payslip, attachment["id"])
        if payslip is None:
            raise OutboxError("El recibo de nómina ya no existe.")
        return filename, "application/pdf", build_payslips_pdf(payslip.run, company_profile(database), [payslip])

    if kind == "dunning_letter":
        from app.dunning_service import build_letter_pdf

        return filename, "application/pdf", build_letter_pdf(database, attachment["id"])

    if kind == "advisor_pack":
        from app.advisor_service import build_advisor_pack

        return filename, "application/zip", build_advisor_pack(database, year=attachment["year"], quarter=attachment["quarter"])

    if kind == "document":
        document = database.get(Document, attachment["id"])
        if document is None:
            raise OutboxError("El documento adjunto ya no existe.")
        path = settings.upload_dir / document.stored_filename
        return document.original_filename, document.mime_type or "application/octet-stream", path.read_bytes()

    raise OutboxError(f"Tipo de adjunto desconocido: {kind}")


def build_email(database: Session, message: OutboxMessage, *, draft: bool = False) -> EmailMessage:
    name, address = sender(database)
    email = EmailMessage()
    email["From"] = formataddr((name, address))
    if message.to_email:
        email["To"] = formataddr((message.to_name or "", message.to_email))
    email["Subject"] = message.subject
    email["Message-ID"] = make_msgid(domain=address.split("@")[-1])
    if draft:
        # Outlook abre el .eml como borrador listo para enviar.
        email["X-Unsent"] = "1"
    email.set_content(message.body)

    for attachment in message.attachments or []:
        filename, mime, content = resolve_attachment(database, attachment)
        main, _, sub = mime.partition("/")
        email.add_attachment(content, maintype=main, subtype=sub or "octet-stream", filename=filename)

    return email


def eml_bytes(database: Session, message: OutboxMessage) -> bytes:
    return bytes(build_email(database, message, draft=True))


# ---------------------------------------------------------------------
# Acciones
# ---------------------------------------------------------------------


def after_sent(database: Session, message: OutboxMessage) -> None:
    now = clock.now()
    message.status = "SENT"
    message.sent_at = now
    message.error = None

    if message.entity_type == "sales_invoice" and message.entity_id:
        invoice = database.get(SalesInvoice, message.entity_id)
        if invoice:
            invoice.sent_at = now


def send_message(database: Session, message: OutboxMessage, *, actor: str = "user") -> OutboxMessage:
    if message.status == "SENT":
        raise OutboxError("El mensaje ya se envió.")
    if not message.to_email or not EMAIL_PATTERN.match(message.to_email):
        raise OutboxError("Indica un email de destino válido antes de enviar.")
    if not smtp_configured():
        raise OutboxError(
            "No hay servidor de correo configurado. Descarga el borrador (.eml) y envíalo desde tu "
            "correo, o configura SMTP_HOST en el archivo .env."
        )

    email = build_email(database, message)

    try:
        if settings.smtp_use_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
            server.starttls(context=ssl.create_default_context())
        with server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(email)
    except (OSError, smtplib.SMTPException) as error:
        message.status = "FAILED"
        message.error = str(error)[:500]
        raise OutboxError(f"El servidor de correo rechazó el envío: {error}") from error

    after_sent(database, message)
    add_audit_event(
        database,
        action="outbox.sent",
        entity_type="outbox",
        entity_id=message.id,
        actor=actor,
        event_data={"to": message.to_email, "subject": message.subject, "kind": message.kind},
    )
    return message


def mark_sent(database: Session, message: OutboxMessage, *, actor: str = "user") -> OutboxMessage:
    """El usuario lo ha enviado desde su propio correo (.eml)."""
    after_sent(database, message)
    add_audit_event(
        database,
        action="outbox.marked_sent",
        entity_type="outbox",
        entity_id=message.id,
        actor=actor,
        event_data={"to": message.to_email, "subject": message.subject, "kind": message.kind},
    )
    return message


def discard(database: Session, message: OutboxMessage, *, actor: str = "user") -> OutboxMessage:
    if message.status == "SENT":
        raise OutboxError("Un mensaje enviado no se puede descartar.")
    message.status = "DISCARDED"
    add_audit_event(
        database,
        action="outbox.discarded",
        entity_type="outbox",
        entity_id=message.id,
        actor=actor,
        event_data={"subject": message.subject, "kind": message.kind},
    )
    return message


# ---------------------------------------------------------------------
# Consultas
# ---------------------------------------------------------------------


def serialize_message(message: OutboxMessage) -> dict[str, Any]:
    return {
        "id": message.id,
        "kind": message.kind,
        "kind_label": KIND_LABELS.get(message.kind, message.kind),
        "status": message.status,
        "status_label": STATUS_LABELS.get(message.status, message.status),
        "to_email": message.to_email,
        "to_name": message.to_name,
        "subject": message.subject,
        "body": message.body,
        "attachments": [
            {"type": item.get("type"), "filename": item.get("filename")}
            for item in (message.attachments or [])
        ],
        "entity_type": message.entity_type,
        "entity_id": message.entity_id,
        "level": message.level,
        "created_by": message.created_by,
        "error": message.error,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "sent_at": message.sent_at.isoformat() if message.sent_at else None,
        "needs_email": not message.to_email,
    }


def list_messages(database: Session, *, status: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    statement = select(OutboxMessage).order_by(OutboxMessage.created_at.desc(), OutboxMessage.id.desc()).limit(300)
    if status:
        statement = statement.where(OutboxMessage.status == status)
    if kind:
        statement = statement.where(OutboxMessage.kind == kind)
    return [serialize_message(item) for item in database.scalars(statement).all()]


def outbox_counts(database: Session) -> dict[str, Any]:
    rows = database.execute(select(OutboxMessage.status, func.count()).group_by(OutboxMessage.status)).all()
    counts = {status: count for status, count in rows}
    return {
        "draft": counts.get("DRAFT", 0) + counts.get("FAILED", 0),
        "sent": counts.get("SENT", 0),
        "smtp": smtp_configured(),
    }


def sent_since(database: Session, since: date) -> int:
    return database.scalar(
        select(func.count()).select_from(OutboxMessage).where(
            OutboxMessage.status == "SENT",
            OutboxMessage.sent_at >= datetime.combine(since, datetime.min.time(), timezone.utc),
        )
    ) or 0


# ---------------------------------------------------------------------
# Mensajes habituales
# ---------------------------------------------------------------------


def prepare_invoice_email(database: Session, invoice: SalesInvoice, *, created_by: str = "user") -> OutboxMessage:
    if invoice.status != "ISSUED":
        raise OutboxError("Emite la factura antes de enviarla.")

    existing = database.scalar(
        select(OutboxMessage).where(
            OutboxMessage.entity_type == "sales_invoice",
            OutboxMessage.entity_id == invoice.id,
            OutboxMessage.kind == "INVOICE",
            OutboxMessage.status == "DRAFT",
        )
    )
    if existing:
        return existing

    company = company_profile(database)
    snapshot = invoice.customer_snapshot or {}
    email = (invoice.customer.email if invoice.customer else None) or snapshot.get("email")
    from app.sales_service import format_day
    from app.sales_service import format_eur

    body = (
        f"Hola,\n\n"
        f"Te adjuntamos la factura {invoice.code} de {format_day(invoice.issue_date)} "
        f"por importe de {format_eur(invoice.total)}, con vencimiento el {format_day(invoice.due_date)}.\n\n"
        + (f"Puedes abonarla por transferencia a la cuenta {format_iban(company.iban)}.\n\n" if company and company.iban else "")
        + "Cualquier duda, responde a este correo.\n\n"
        f"Un saludo,\n{(company.name if company else '') or ''}"
    )
    return create_message(
        database,
        kind="INVOICE",
        subject=f"Factura {invoice.code} · {(company.name if company else '') or ''}".strip(" ·"),
        body=body,
        to_email=email,
        to_name=snapshot.get("name"),
        attachments=[{"type": "sales_invoice", "id": invoice.id, "filename": f"{invoice.code}.pdf"}],
        entity_type="sales_invoice",
        entity_id=invoice.id,
        created_by=created_by,
    )


def prepare_payslip_emails(database: Session, run_id: int, *, created_by: str = "user") -> dict[str, Any]:
    from app.models import PayrollRun
    from app.payroll_service import employee_display_name
    from app.payroll_service import period_label

    run = database.get(PayrollRun, run_id)
    if run is None:
        raise OutboxError("Nómina no encontrada.")
    if run.status == "DRAFT":
        raise OutboxError("Aprueba la nómina antes de enviar los recibos.")

    company = company_profile(database)
    created, skipped = 0, []

    for payslip in run.payslips:
        employee = payslip.employee
        name = employee_display_name(employee) if employee else "Empleado"
        exists = database.scalar(
            select(OutboxMessage.id).where(
                OutboxMessage.kind == "PAYSLIP",
                OutboxMessage.entity_type == "payslip",
                OutboxMessage.entity_id == payslip.id,
                OutboxMessage.status.in_(["DRAFT", "SENT"]),
            )
        )
        if exists:
            continue
        if not employee or not employee.email:
            skipped.append(name)
        create_message(
            database,
            kind="PAYSLIP",
            subject=f"Tu nómina de {period_label(run)}",
            body=(
                f"Hola {employee.first_name if employee else ''},\n\n"
                f"Te adjuntamos tu recibo de salarios de {period_label(run)}.\n\n"
                f"Un saludo,\n{(company.name if company else '') or ''}"
            ),
            to_email=employee.email if employee else None,
            to_name=name,
            attachments=[
                {"type": "payslip", "id": payslip.id, "filename": f"nomina_{run.year}_{run.month:02d}_{name.replace(' ', '_')}.pdf"}
            ],
            entity_type="payslip",
            entity_id=payslip.id,
            created_by=created_by,
        )
        created += 1

    return {"created": created, "without_email": skipped}
