"""
API de Ventas (clientes, facturas emitidas, recurrentes, cobros), bandeja
de salida, registro de jornada, automatizaciones y cierre para la gestoría.
"""
from __future__ import annotations

from app import clock
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import Response
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from app.deps import ActorHeader
from app.deps import DatabaseDependency
from app.deps import normalize_actor
from app.invoice_service import add_audit_event
from app.models import Customer
from app.models import OutboxMessage
from app.models import RecurringInvoice
from app.models import SalesInvoice
from app.models import TimeEntry

router = APIRouter(prefix="/api")


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def unprocessable(error: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


def file_response(content: bytes, filename: str, media_type: str, inline: bool = False) -> Response:
    disposition = "inline" if inline else "attachment"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# -------------------------------------------------------------------
# Clientes
# -------------------------------------------------------------------


class CustomerPayload(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    tax_id: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=30)
    address: str | None = Field(default=None, max_length=255)
    postal_code: str | None = Field(default=None, max_length=10)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=2)
    payment_days: int | None = Field(default=None, ge=0, le=365)
    withholding_rate: Decimal | None = Field(default=None, ge=0, le=47)
    notes: str | None = Field(default=None, max_length=5000)


@router.get("/sales/customers", tags=["Ventas"])
def customers(database: DatabaseDependency) -> list[dict[str, Any]]:
    from app.sales_service import list_customers

    return list_customers(database)


@router.post("/sales/customers", tags=["Ventas"], status_code=201)
def create_customer(payload: CustomerPayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.sales_service import SalesError
    from app.sales_service import apply_customer_changes
    from app.sales_service import serialize_customer

    customer = Customer()
    try:
        apply_customer_changes(database, customer, payload.model_dump(exclude_unset=True))
    except SalesError as error:
        raise unprocessable(error) from error
    database.add(customer)
    database.flush()
    add_audit_event(database, action="customer.created", entity_type="customer", entity_id=customer.id, actor=normalize_actor(actor_header), event_data={"name": customer.name, "tax_id": customer.tax_id})
    database.commit()
    return serialize_customer(customer)


@router.patch("/sales/customers/{customer_id}", tags=["Ventas"])
def update_customer(customer_id: int, payload: CustomerPayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import SalesError
    from app.sales_service import apply_customer_changes
    from app.sales_service import serialize_customer

    customer = database.get(Customer, customer_id)
    if customer is None:
        raise not_found("Cliente no encontrado.")
    try:
        apply_customer_changes(database, customer, payload.model_dump(exclude_unset=True))
    except SalesError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_customer(customer)


@router.delete("/sales/customers/{customer_id}", tags=["Ventas"])
def delete_customer(customer_id: int, database: DatabaseDependency) -> dict[str, Any]:
    customer = database.get(Customer, customer_id)
    if customer is None:
        raise not_found("Cliente no encontrado.")
    if database.scalar(select(SalesInvoice.id).where(SalesInvoice.customer_id == customer_id, SalesInvoice.status == "ISSUED")):
        raise HTTPException(status_code=409, detail="El cliente tiene facturas emitidas: no se puede borrar.")
    database.delete(customer)
    database.commit()
    return {"deleted": True}


@router.post("/sales/customers/import", tags=["Ventas"])
def import_customers(database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import import_customers_from_ledger

    created = import_customers_from_ledger(database)
    database.commit()
    return {"created": created}


# -------------------------------------------------------------------
# Facturas emitidas
# -------------------------------------------------------------------


class LinePayload(BaseModel):
    description: str | None = Field(default=None, max_length=500)
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    discount: Decimal | None = None
    vat_rate: Decimal | None = None


class SalesInvoicePayload(BaseModel):
    customer_id: int | None = None
    issue_date: date | None = None
    operation_date: date | None = None
    due_date: date | None = None
    withholding_rate: Decimal | None = None
    lines: list[LinePayload] | None = None
    notes: str | None = Field(default=None, max_length=2000)
    payment_terms: str | None = Field(default=None, max_length=255)
    rectification_reason: str | None = Field(default=None, max_length=255)


def invoice_data(payload: SalesInvoicePayload) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    if "lines" in data and data["lines"] is not None:
        data["lines"] = [{key: (float(value) if isinstance(value, Decimal) else value) for key, value in line.items()} for line in data["lines"]]
    return data


def sales_invoice_or_404(database, invoice_id: int) -> SalesInvoice:
    invoice = database.get(SalesInvoice, invoice_id)
    if invoice is None:
        raise not_found("Factura no encontrada.")
    return invoice


@router.get("/sales/overview", tags=["Ventas"])
def sales_overview(database: DatabaseDependency) -> dict[str, Any]:
    from app.dunning_service import collections_overview
    from app.sales_service import sales_overview as overview

    data = overview(database)
    data["collections"] = collections_overview(database)["summary"]
    return data


@router.get("/sales/invoices", tags=["Ventas"])
def sales_invoices(
    database: DatabaseDependency,
    status: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=100),
    year: int | None = Query(default=None),
) -> list[dict[str, Any]]:
    from app.sales_service import list_invoices

    return list_invoices(database, status=status, query=q, year=year)


@router.post("/sales/invoices", tags=["Ventas"], status_code=201)
def create_sales_invoice(payload: SalesInvoicePayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import SalesError
    from app.sales_service import create_draft
    from app.sales_service import serialize_invoice

    try:
        invoice = create_draft(database, invoice_data(payload))
    except SalesError as error:
        raise unprocessable(error) from error
    database.commit()
    database.refresh(invoice)
    return serialize_invoice(invoice, full=True)


@router.get("/sales/invoices/{invoice_id}", tags=["Ventas"])
def get_sales_invoice(invoice_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import serialize_invoice

    return serialize_invoice(sales_invoice_or_404(database, invoice_id), full=True)


@router.patch("/sales/invoices/{invoice_id}", tags=["Ventas"])
def update_sales_invoice(invoice_id: int, payload: SalesInvoicePayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import SalesError
    from app.sales_service import apply_draft_changes
    from app.sales_service import serialize_invoice

    invoice = sales_invoice_or_404(database, invoice_id)
    try:
        apply_draft_changes(database, invoice, invoice_data(payload))
    except SalesError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_invoice(invoice, full=True)


@router.delete("/sales/invoices/{invoice_id}", tags=["Ventas"])
def delete_sales_invoice(invoice_id: int, database: DatabaseDependency) -> dict[str, Any]:
    invoice = sales_invoice_or_404(database, invoice_id)
    if invoice.status != "DRAFT":
        raise HTTPException(status_code=409, detail="Una factura emitida no se puede borrar: emite una rectificativa.")
    database.delete(invoice)
    database.commit()
    return {"deleted": True}


@router.post("/sales/invoices/{invoice_id}/issue", tags=["Ventas"])
def issue_sales_invoice(invoice_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.sales_service import SalesError
    from app.sales_service import issue_invoice
    from app.sales_service import serialize_invoice

    invoice = sales_invoice_or_404(database, invoice_id)
    try:
        issue_invoice(database, invoice, actor=normalize_actor(actor_header))
    except SalesError as error:
        database.rollback()
        raise unprocessable(error) from error
    database.commit()
    database.refresh(invoice)
    return serialize_invoice(invoice, full=True)


class RectifyPayload(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


@router.post("/sales/invoices/{invoice_id}/rectify", tags=["Ventas"], status_code=201)
def rectify_sales_invoice(invoice_id: int, payload: RectifyPayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import SalesError
    from app.sales_service import create_rectification
    from app.sales_service import serialize_invoice

    original = sales_invoice_or_404(database, invoice_id)
    try:
        draft = create_rectification(database, original, payload.reason)
    except SalesError as error:
        raise unprocessable(error) from error
    database.commit()
    database.refresh(draft)
    return serialize_invoice(draft, full=True)


@router.post("/sales/invoices/{invoice_id}/duplicate", tags=["Ventas"], status_code=201)
def duplicate_sales_invoice(invoice_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import create_draft
    from app.sales_service import serialize_invoice

    original = sales_invoice_or_404(database, invoice_id)
    lines = [line for line in (original.lines or [])]
    if original.series == "R":
        lines = [{**line, "quantity": abs(line["quantity"])} for line in lines]
    draft = create_draft(
        database,
        {"customer_id": original.customer_id, "withholding_rate": original.withholding_rate, "lines": lines, "notes": original.notes, "payment_terms": original.payment_terms},
    )
    database.commit()
    database.refresh(draft)
    return serialize_invoice(draft, full=True)


@router.get("/sales/invoices/{invoice_id}/pdf", tags=["Ventas"])
def sales_invoice_pdf(invoice_id: int, database: DatabaseDependency) -> Response:
    from app.sales_service import invoice_pdf_bytes

    invoice = sales_invoice_or_404(database, invoice_id)
    return file_response(invoice_pdf_bytes(database, invoice), f"{invoice.code or f'borrador_{invoice.id}'}.pdf", "application/pdf", inline=True)


@router.get("/sales/invoices/{invoice_id}/qr.svg", tags=["Ventas"])
def sales_invoice_qr(invoice_id: int, database: DatabaseDependency) -> Response:
    from app.sales_service import build_qr_svg

    invoice = sales_invoice_or_404(database, invoice_id)
    if not invoice.qr_url:
        raise not_found("La factura aún no tiene registro de facturación.")
    return Response(content=build_qr_svg(invoice.qr_url), media_type="image/svg+xml")


@router.post("/sales/invoices/{invoice_id}/send", tags=["Ventas"])
def send_sales_invoice(invoice_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.outbox_service import OutboxError
    from app.outbox_service import prepare_invoice_email
    from app.outbox_service import serialize_message

    invoice = sales_invoice_or_404(database, invoice_id)
    try:
        message = prepare_invoice_email(database, invoice, created_by="user")
    except OutboxError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_message(message)


@router.get("/sales/chain", tags=["Ventas"])
def sales_chain(database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import verify_chain

    return verify_chain(database)


# -------------------------------------------------------------------
# Recurrentes
# -------------------------------------------------------------------


class RecurringPayload(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    customer_id: int | None = None
    lines: list[LinePayload] | None = None
    withholding_rate: Decimal | None = None
    frequency: str | None = Field(default=None, max_length=20)
    next_date: date | None = None
    end_date: date | None = None
    auto_issue: bool | None = None
    auto_send: bool | None = None
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


def recurring_data(payload: RecurringPayload) -> dict[str, Any]:
    data = payload.model_dump(exclude_unset=True)
    if data.get("lines") is not None:
        data["lines"] = [{key: (float(value) if isinstance(value, Decimal) else value) for key, value in line.items()} for line in data["lines"]]
    return data


@router.get("/sales/recurring", tags=["Ventas"])
def recurring_list(database: DatabaseDependency) -> list[dict[str, Any]]:
    from app.sales_service import serialize_recurring

    items = database.scalars(select(RecurringInvoice).order_by(RecurringInvoice.active.desc(), RecurringInvoice.next_date)).all()
    return [serialize_recurring(item) for item in items]


@router.post("/sales/recurring", tags=["Ventas"], status_code=201)
def recurring_create(payload: RecurringPayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import SalesError
    from app.sales_service import apply_recurring_changes
    from app.sales_service import serialize_recurring

    template = RecurringInvoice(lines=[], generated_count=0, frequency="MONTHLY", active=True, auto_issue=False, auto_send=False)
    try:
        apply_recurring_changes(database, template, recurring_data(payload))
    except SalesError as error:
        raise unprocessable(error) from error
    database.add(template)
    database.commit()
    database.refresh(template)
    return serialize_recurring(template)


@router.patch("/sales/recurring/{template_id}", tags=["Ventas"])
def recurring_update(template_id: int, payload: RecurringPayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.sales_service import SalesError
    from app.sales_service import apply_recurring_changes
    from app.sales_service import serialize_recurring

    template = database.get(RecurringInvoice, template_id)
    if template is None:
        raise not_found("Factura recurrente no encontrada.")
    try:
        apply_recurring_changes(database, template, recurring_data(payload))
    except SalesError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_recurring(template)


@router.delete("/sales/recurring/{template_id}", tags=["Ventas"])
def recurring_delete(template_id: int, database: DatabaseDependency) -> dict[str, Any]:
    template = database.get(RecurringInvoice, template_id)
    if template is None:
        raise not_found("Factura recurrente no encontrada.")
    database.delete(template)
    database.commit()
    return {"deleted": True}


# -------------------------------------------------------------------
# Cobros
# -------------------------------------------------------------------


@router.get("/collections", tags=["Cobros"])
def collections(database: DatabaseDependency) -> dict[str, Any]:
    from app.dunning_service import collections_overview

    return collections_overview(database)


class RemindPayload(BaseModel):
    invoice_ids: list[int] | None = None


@router.post("/collections/remind", tags=["Cobros"])
def collections_remind(payload: RemindPayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.dunning_service import prepare_reminders

    created = prepare_reminders(database, invoice_ids=set(payload.invoice_ids) if payload.invoice_ids else None, created_by="user")
    database.commit()
    return {"created": len(created), "without_email": sum(1 for item in created if not item.to_email)}


@router.get("/collections/{invoice_id}/letter.pdf", tags=["Cobros"])
def collections_letter(invoice_id: int, database: DatabaseDependency) -> Response:
    from app.dunning_service import build_letter_pdf

    try:
        content = build_letter_pdf(database, invoice_id)
    except ValueError as error:
        raise not_found(str(error)) from error
    return file_response(content, f"requerimiento_{invoice_id}.pdf", "application/pdf", inline=True)


# -------------------------------------------------------------------
# Bandeja de salida
# -------------------------------------------------------------------


class OutboxPayload(BaseModel):
    to_email: str | None = Field(default=None, max_length=255)
    to_name: str | None = Field(default=None, max_length=255)
    subject: str | None = Field(default=None, max_length=255)
    body: str | None = Field(default=None, max_length=20000)


def message_or_404(database, message_id: int) -> OutboxMessage:
    message = database.get(OutboxMessage, message_id)
    if message is None:
        raise not_found("Mensaje no encontrado.")
    return message


@router.get("/outbox", tags=["Bandeja de salida"])
def outbox(database: DatabaseDependency, status: str | None = Query(default=None), kind: str | None = Query(default=None)) -> dict[str, Any]:
    from app.outbox_service import list_messages
    from app.outbox_service import outbox_counts

    return {"messages": list_messages(database, status=status, kind=kind), "counts": outbox_counts(database)}


@router.patch("/outbox/{message_id}", tags=["Bandeja de salida"])
def outbox_update(message_id: int, payload: OutboxPayload, database: DatabaseDependency) -> dict[str, Any]:
    from app.outbox_service import serialize_message

    message = message_or_404(database, message_id)
    if message.status == "SENT":
        raise HTTPException(status_code=409, detail="El mensaje ya se envió.")
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field in {"subject", "body"} and not (value or "").strip():
            raise HTTPException(status_code=422, detail="El asunto y el texto no pueden quedar vacíos.")
        setattr(message, field, value.strip() if isinstance(value, str) and field != "body" else value)
    if message.status == "FAILED":
        message.status = "DRAFT"
    database.commit()
    return serialize_message(message)


@router.post("/outbox/{message_id}/send", tags=["Bandeja de salida"])
def outbox_send(message_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.outbox_service import OutboxError
    from app.outbox_service import send_message
    from app.outbox_service import serialize_message

    message = message_or_404(database, message_id)
    try:
        send_message(database, message, actor=normalize_actor(actor_header))
    except OutboxError as error:
        database.commit()  # guarda el estado de error
        raise unprocessable(error) from error
    database.commit()
    return serialize_message(message)


@router.post("/outbox/{message_id}/mark-sent", tags=["Bandeja de salida"])
def outbox_mark_sent(message_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.outbox_service import mark_sent
    from app.outbox_service import serialize_message

    message = message_or_404(database, message_id)
    mark_sent(database, message, actor=normalize_actor(actor_header))
    database.commit()
    return serialize_message(message)


@router.post("/outbox/{message_id}/discard", tags=["Bandeja de salida"])
def outbox_discard(message_id: int, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.outbox_service import OutboxError
    from app.outbox_service import discard
    from app.outbox_service import serialize_message

    message = message_or_404(database, message_id)
    try:
        discard(database, message, actor=normalize_actor(actor_header))
    except OutboxError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_message(message)


@router.get("/outbox/{message_id}/eml", tags=["Bandeja de salida"])
def outbox_eml(message_id: int, database: DatabaseDependency) -> Response:
    from app.outbox_service import OutboxError
    from app.outbox_service import eml_bytes

    message = message_or_404(database, message_id)
    try:
        content = eml_bytes(database, message)
    except OutboxError as error:
        raise unprocessable(error) from error
    return file_response(content, f"mensaje_{message.id}.eml", "message/rfc822")


@router.get("/outbox/{message_id}/attachments/{index}", tags=["Bandeja de salida"])
def outbox_attachment(message_id: int, index: int, database: DatabaseDependency) -> Response:
    from app.outbox_service import OutboxError
    from app.outbox_service import resolve_attachment

    message = message_or_404(database, message_id)
    attachments = message.attachments or []
    if not 0 <= index < len(attachments):
        raise not_found("Adjunto no encontrado.")
    try:
        filename, mime, content = resolve_attachment(database, attachments[index])
    except OutboxError as error:
        raise unprocessable(error) from error
    return file_response(content, filename, mime, inline=mime == "application/pdf")


@router.post("/payroll/runs/{run_id}/email", tags=["Nóminas"])
def payroll_email(run_id: int, database: DatabaseDependency) -> dict[str, Any]:
    from app.outbox_service import OutboxError
    from app.outbox_service import prepare_payslip_emails

    try:
        result = prepare_payslip_emails(database, run_id)
    except OutboxError as error:
        raise unprocessable(error) from error
    database.commit()
    return result


# -------------------------------------------------------------------
# Registro de jornada
# -------------------------------------------------------------------


class ClockPayload(BaseModel):
    employee_id: int
    note: str | None = Field(default=None, max_length=255)


class TimeEntryPayload(BaseModel):
    employee_id: int
    work_date: date
    start: str = Field(max_length=5)
    end: str | None = Field(default=None, max_length=5)
    reason: str | None = Field(default=None, max_length=255)
    note: str | None = Field(default=None, max_length=255)


class DeleteEntryPayload(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


@router.get("/timesheet/today", tags=["Jornada"])
def timesheet_today(database: DatabaseDependency) -> dict[str, Any]:
    from app.timesheet_service import day_board

    return day_board(database)


@router.post("/timesheet/clock", tags=["Jornada"])
def timesheet_clock(payload: ClockPayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.timesheet_service import TimesheetError
    from app.timesheet_service import clock

    try:
        result = clock(database, payload.employee_id, actor=normalize_actor(actor_header), note=payload.note)
    except TimesheetError as error:
        raise unprocessable(error) from error
    database.commit()
    return result


@router.get("/timesheet/entries", tags=["Jornada"])
def timesheet_entries(
    database: DatabaseDependency,
    date_from: date = Query(...),
    date_to: date = Query(...),
    employee_id: int | None = Query(default=None),
) -> list[dict[str, Any]]:
    from app.timesheet_service import entries_between
    from app.timesheet_service import local_now
    from app.timesheet_service import serialize_entry

    now = local_now()
    return [serialize_entry(item, now) for item in entries_between(database, date_from, date_to, employee_id)]


@router.post("/timesheet/entries", tags=["Jornada"], status_code=201)
def timesheet_create(payload: TimeEntryPayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.timesheet_service import TimesheetError
    from app.timesheet_service import save_manual
    from app.timesheet_service import serialize_entry

    try:
        entry = save_manual(database, **payload.model_dump(), actor=normalize_actor(actor_header))
    except TimesheetError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_entry(entry)


@router.patch("/timesheet/entries/{entry_id}", tags=["Jornada"])
def timesheet_update(entry_id: int, payload: TimeEntryPayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.timesheet_service import TimesheetError
    from app.timesheet_service import save_manual
    from app.timesheet_service import serialize_entry

    entry = database.get(TimeEntry, entry_id)
    if entry is None:
        raise not_found("Registro no encontrado.")
    try:
        save_manual(database, **payload.model_dump(), entry=entry, actor=normalize_actor(actor_header))
    except TimesheetError as error:
        raise unprocessable(error) from error
    database.commit()
    return serialize_entry(entry)


@router.post("/timesheet/entries/{entry_id}/delete", tags=["Jornada"])
def timesheet_delete(entry_id: int, payload: DeleteEntryPayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.timesheet_service import TimesheetError
    from app.timesheet_service import delete_entry

    entry = database.get(TimeEntry, entry_id)
    if entry is None:
        raise not_found("Registro no encontrado.")
    try:
        delete_entry(database, entry, payload.reason, actor=normalize_actor(actor_header))
    except TimesheetError as error:
        raise unprocessable(error) from error
    database.commit()
    return {"deleted": True}


@router.get("/timesheet/month", tags=["Jornada"])
def timesheet_month(database: DatabaseDependency, year: int = Query(..., ge=2000, le=2100), month: int = Query(..., ge=1, le=12)) -> dict[str, Any]:
    from app.timesheet_service import month_summary

    return month_summary(database, year, month)


@router.get("/timesheet/alerts", tags=["Jornada"])
def timesheet_alerts_endpoint(database: DatabaseDependency) -> list[dict[str, Any]]:
    from app.timesheet_service import timesheet_alerts

    return timesheet_alerts(database)


@router.get("/timesheet/month.pdf", tags=["Jornada"])
def timesheet_pdf(
    database: DatabaseDependency,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    employee_id: int | None = Query(default=None),
) -> Response:
    from app.timesheet_service import build_month_pdf

    return file_response(build_month_pdf(database, year, month, employee_id), f"registro_jornada_{year}_{month:02d}.pdf", "application/pdf", inline=True)


@router.get("/timesheet/month.xlsx", tags=["Jornada"])
def timesheet_xlsx(database: DatabaseDependency, year: int = Query(..., ge=2000, le=2100), month: int = Query(..., ge=1, le=12)) -> Response:
    from app.timesheet_service import build_month_xlsx

    return file_response(build_month_xlsx(database, year, month), f"registro_jornada_{year}_{month:02d}.xlsx", XLSX)


# -------------------------------------------------------------------
# Automatizaciones
# -------------------------------------------------------------------


class AutomationPayload(BaseModel):
    enabled: bool


@router.get("/automations", tags=["Automatizaciones"])
def automations(database: DatabaseDependency) -> dict[str, Any]:
    from app.automation_service import automations_overview

    data = automations_overview(database)
    database.commit()
    return data


@router.patch("/automations/{code}", tags=["Automatizaciones"])
def automation_toggle(code: str, payload: AutomationPayload, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.automation_service import set_enabled

    try:
        set_enabled(database, code, payload.enabled)
    except ValueError as error:
        raise not_found(str(error)) from error
    add_audit_event(database, action="automation.toggled", entity_type="automation", entity_id=code, actor=normalize_actor(actor_header), event_data={"enabled": payload.enabled})
    database.commit()
    return {"code": code, "enabled": payload.enabled}


@router.post("/automations/{code}/run", tags=["Automatizaciones"])
def automation_run(code: str, database: DatabaseDependency, actor_header: ActorHeader = None) -> dict[str, Any]:
    from app.automation_service import run_automation
    from app.automation_service import serialize_run

    try:
        run = run_automation(database, code, trigger="MANUAL", actor=normalize_actor(actor_header))
    except ValueError as error:
        raise not_found(str(error)) from error
    database.commit()
    return serialize_run(run)


@router.get("/digest", tags=["Automatizaciones"])
def digest(database: DatabaseDependency) -> dict[str, Any]:
    from app.automation_service import build_digest

    return build_digest(database, clock.today())


# -------------------------------------------------------------------
# Cierre para la gestoría
# -------------------------------------------------------------------


@router.get("/advisor/summary", tags=["Gestoría"])
def advisor_summary(database: DatabaseDependency, year: int = Query(..., ge=2000, le=2100), quarter: int = Query(..., ge=1, le=4)) -> dict[str, Any]:
    from app.advisor_service import pack_summary

    return pack_summary(database, year, quarter)


@router.get("/advisor/pack.zip", tags=["Gestoría"])
def advisor_pack(database: DatabaseDependency, year: int = Query(..., ge=2000, le=2100), quarter: int = Query(..., ge=1, le=4)) -> Response:
    from app.advisor_service import build_advisor_pack

    return file_response(build_advisor_pack(database, year=year, quarter=quarter), f"cierre_{year}_{quarter}T.zip", "application/zip")


@router.post("/advisor/send", tags=["Gestoría"])
def advisor_send(database: DatabaseDependency, year: int = Query(..., ge=2000, le=2100), quarter: int = Query(..., ge=1, le=4)) -> dict[str, Any]:
    from app.advisor_service import prepare_advisor_email
    from app.outbox_service import serialize_message

    message = prepare_advisor_email(database, year=year, quarter=quarter)
    database.commit()
    return serialize_message(message)
