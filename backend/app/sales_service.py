"""
Ventas: clientes, facturas emitidas desde CapaFiscal y facturas recurrentes.

Al emitir una factura:
- se le asigna el siguiente número correlativo de su serie y año;
- se genera el registro de facturación con huella SHA-256 encadenada a la
  factura anterior (RD 1007/2023 y Orden HAC/1177/2024) y la URL del QR
  tributario;
- se crea el PDF y se anota en el libro de facturas emitidas, de modo que
  el IVA repercutido, el 347, los cobros y la tesorería se actualizan solos.

La remisión a la AEAT (VERI*FACTU) necesita el conector con certificado; hasta
entonces el registro queda preparado y encadenado, sin remitir.
"""
from __future__ import annotations

from app import clock
import hashlib
import io
import uuid
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import ROUND_HALF_UP
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calendar_es import add_months
from app.config import settings
from app.extractor import is_valid_spanish_tax_id
from app.extractor import normalize_tax_id
from app.invoice_service import add_audit_event
from app.models import CompanyProfile
from app.models import Customer
from app.models import Document
from app.models import Invoice
from app.models import InvoiceTaxLine
from app.models import RecurringInvoice
from app.models import SalesInvoice

ZERO = Decimal("0")
CENT = Decimal("0.01")

VAT_RATES = (Decimal("21"), Decimal("10"), Decimal("5"), Decimal("4"), Decimal("0"))
FREQUENCIES = {"MONTHLY": ("Mensual", 1), "QUARTERLY": ("Trimestral", 3), "YEARLY": ("Anual", 12)}
SERIES_LABELS = {"F": "Ordinaria", "R": "Rectificativa"}
INVOICE_TYPES = {
    "F1": "Factura completa",
    "F2": "Factura simplificada",
    "R1": "Rectificativa (art. 80.1, 80.2 y error fundado)",
    "R4": "Rectificativa (resto de causas)",
}
MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
QR_BASE_URL = "https://www2.agenciatributaria.gob.es/wlpl/TIKE-CONT/ValidarQR"
DEFAULT_CATEGORY = "Prestación de servicios"


class SalesError(ValueError):
    """Error de validación que se muestra tal cual al usuario."""


def money(value: Any) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def format_eur(value: Any) -> str:
    amount = money(value)
    text = f"{abs(amount):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{'-' if amount < 0 else ''}{text} €"


def format_day(value: date | None) -> str:
    return value.strftime("%d/%m/%Y") if value else "—"


# ---------------------------------------------------------------------
# Hora oficial peninsular (sin depender de tzdata en Windows)
# ---------------------------------------------------------------------


def _last_sunday(year: int, month: int) -> date:
    day = add_months(date(year, month, 1), 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() + 1) % 7)


def madrid_now(now: datetime | None = None) -> datetime:
    """Hora de Madrid con su desplazamiento (CET/CEST)."""
    utc = (now or clock.now()).astimezone(timezone.utc)
    start = datetime.combine(_last_sunday(utc.year, 3), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(utc.year, 10), datetime.min.time(), timezone.utc) + timedelta(hours=1)
    offset = 2 if start <= utc < end else 1
    return utc.astimezone(timezone(timedelta(hours=offset))).replace(microsecond=0)


# ---------------------------------------------------------------------
# Clientes
# ---------------------------------------------------------------------


def serialize_customer(customer: Customer, stats: dict[str, Any] | None = None) -> dict[str, Any]:
    tax_id = customer.tax_id
    return {
        "id": customer.id,
        "name": customer.name,
        "tax_id": tax_id,
        "tax_id_valid": is_valid_spanish_tax_id(tax_id) if tax_id else None,
        "email": customer.email,
        "phone": customer.phone,
        "address": customer.address,
        "postal_code": customer.postal_code,
        "city": customer.city,
        "country": customer.country or "ES",
        "payment_days": customer.payment_days,
        "withholding_rate": as_float(customer.withholding_rate),
        "notes": customer.notes,
        **(stats or {}),
    }


def customer_stats(database: Session) -> dict[str, dict[str, Any]]:
    """Facturado y pendiente de cobro por NIF (del libro de emitidas)."""
    rows = database.execute(
        select(
            Invoice.customer_tax_id,
            func.count(Invoice.id),
            func.sum(Invoice.total),
            func.sum(Invoice.total).filter(Invoice.paid_at.is_(None)),
        )
        .where(Invoice.direction == "ISSUED", Invoice.review_status == "APPROVED")
        .group_by(Invoice.customer_tax_id)
    ).all()

    return {
        (tax_id or ""): {
            "invoice_count": count,
            "invoiced": float(money(total)),
            "pending": float(money(pending)),
        }
        for tax_id, count, total, pending in rows
    }


def list_customers(database: Session) -> list[dict[str, Any]]:
    stats = customer_stats(database)
    customers = database.scalars(select(Customer).order_by(Customer.name)).all()
    empty = {"invoice_count": 0, "invoiced": 0.0, "pending": 0.0}
    return [serialize_customer(item, stats.get(item.tax_id or "\0", empty)) for item in customers]


CUSTOMER_FIELDS = (
    "name", "tax_id", "email", "phone", "address", "postal_code", "city",
    "country", "payment_days", "withholding_rate", "notes",
)


def apply_customer_changes(database: Session, customer: Customer, data: dict[str, Any]) -> Customer:
    for field in CUSTOMER_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if isinstance(value, str):
            value = value.strip() or None
        if field == "tax_id" and value:
            value = normalize_tax_id(value) or value.upper().replace(" ", "")
        if field == "country" and value:
            value = value.upper()[:2]
        setattr(customer, field, value)

    if not customer.name:
        raise SalesError("Indica el nombre o razón social del cliente.")

    if customer.tax_id:
        duplicate = database.scalar(
            select(Customer).where(Customer.tax_id == customer.tax_id, Customer.id != (customer.id or 0))
        )
        if duplicate:
            raise SalesError(f"Ya existe un cliente con el NIF {customer.tax_id}: {duplicate.name.rstrip('.')}.")

    return customer


def import_customers_from_ledger(database: Session) -> int:
    """Da de alta como clientes las contrapartes de las facturas emitidas leídas."""
    known = {item for item in database.scalars(select(Customer.tax_id)).all() if item}
    rows = database.execute(
        select(Invoice.customer_tax_id, Invoice.customer_name)
        .where(Invoice.direction == "ISSUED", Invoice.customer_tax_id.is_not(None))
        .distinct()
    ).all()
    created = 0

    for tax_id, name in rows:
        if tax_id in known or not name:
            continue
        database.add(Customer(name=name, tax_id=tax_id, country="ES"))
        known.add(tax_id)
        created += 1

    return created


# ---------------------------------------------------------------------
# Cálculo de importes
# ---------------------------------------------------------------------


def normalize_lines(raw_lines: list[dict[str, Any]], *, allow_negative: bool) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []

    for raw in raw_lines or []:
        description = str(raw.get("description") or "").strip()
        quantity = Decimal(str(raw.get("quantity") if raw.get("quantity") not in (None, "") else 1))
        unit_price = Decimal(str(raw.get("unit_price") or 0))
        discount = Decimal(str(raw.get("discount") or 0))
        vat_rate = Decimal(str(raw.get("vat_rate") if raw.get("vat_rate") not in (None, "") else 21))

        if not description and not unit_price:
            continue
        if not description:
            raise SalesError("Cada línea necesita una descripción.")
        if not ZERO <= discount <= Decimal("100"):
            raise SalesError("El descuento debe estar entre 0 y 100 %.")
        if not ZERO <= vat_rate <= Decimal("21"):
            raise SalesError("El tipo de IVA debe estar entre 0 y 21 %.")
        if not allow_negative and (quantity < 0 or unit_price < 0):
            raise SalesError("Las cantidades negativas solo se admiten en facturas rectificativas.")

        amount = money(quantity * unit_price * (Decimal("1") - discount / Decimal("100")))
        lines.append(
            {
                "description": description[:500],
                "quantity": float(quantity),
                "unit_price": float(unit_price),
                "discount": float(discount),
                "vat_rate": float(vat_rate),
                "amount": float(amount),
            }
        )

    return lines


def compute_totals(lines: list[dict[str, Any]], withholding_rate: Any) -> dict[str, Any]:
    by_rate: dict[Decimal, Decimal] = {}

    for line in lines:
        rate = Decimal(str(line["vat_rate"]))
        by_rate[rate] = by_rate.get(rate, ZERO) + money(line["amount"])

    breakdown = []
    subtotal = ZERO
    tax_total = ZERO

    for rate in sorted(by_rate, reverse=True):
        base = money(by_rate[rate])
        tax = money(base * rate / Decimal("100"))
        subtotal += base
        tax_total += tax
        breakdown.append({"rate": float(rate), "base": float(base), "tax": float(tax)})

    withholding = money(subtotal * Decimal(str(withholding_rate or 0)) / Decimal("100"))

    return {
        "breakdown": breakdown,
        "subtotal": money(subtotal),
        "tax_total": money(tax_total),
        "withholding_total": withholding,
        "total": money(subtotal + tax_total - withholding),
    }


def fill_template(text: str, period: date) -> str:
    """Sustituye {mes}, {trimestre} y {año} en las líneas recurrentes."""
    quarter = (period.month - 1) // 3 + 1
    return (
        text.replace("{mes}", f"{MONTHS_ES[period.month - 1]} {period.year}")
        .replace("{trimestre}", f"{quarter}T {period.year}")
        .replace("{año}", str(period.year))
    )


# ---------------------------------------------------------------------
# Facturas: borradores
# ---------------------------------------------------------------------


def get_company(database: Session) -> CompanyProfile | None:
    return database.scalar(select(CompanyProfile).limit(1))


def customer_snapshot(customer: Customer) -> dict[str, Any]:
    return {
        "name": customer.name,
        "tax_id": customer.tax_id,
        "address": customer.address,
        "postal_code": customer.postal_code,
        "city": customer.city,
        "country": customer.country or "ES",
        "email": customer.email,
    }


def default_payment_days(database: Session, customer: Customer | None) -> int:
    if customer and customer.payment_days is not None:
        return customer.payment_days
    company = get_company(database)
    if company and company.default_payment_days is not None:
        return company.default_payment_days
    return 30


def apply_draft_changes(database: Session, invoice: SalesInvoice, data: dict[str, Any]) -> SalesInvoice:
    if invoice.status != "DRAFT":
        raise SalesError("Una factura emitida no se puede modificar. Emite una rectificativa.")

    if "customer_id" in data:
        customer = database.get(Customer, data["customer_id"]) if data["customer_id"] else None
        if data["customer_id"] and customer is None:
            raise SalesError("Cliente no encontrado.")
        invoice.customer_id = customer.id if customer else None
        if customer and "withholding_rate" not in data and customer.withholding_rate is not None:
            invoice.withholding_rate = customer.withholding_rate

    for field in ("issue_date", "operation_date", "due_date", "notes", "payment_terms", "rectification_reason"):
        if field in data:
            value = data[field]
            setattr(invoice, field, value.strip() or None if isinstance(value, str) else value)

    if "withholding_rate" in data:
        rate = Decimal(str(data["withholding_rate"] or 0))
        if not ZERO <= rate <= Decimal("47"):
            raise SalesError("La retención de IRPF debe estar entre 0 y 47 %.")
        invoice.withholding_rate = rate

    if "lines" in data:
        invoice.lines = normalize_lines(data["lines"], allow_negative=invoice.series == "R")

    totals = compute_totals(invoice.lines or [], invoice.withholding_rate)
    invoice.subtotal = totals["subtotal"]
    invoice.tax_total = totals["tax_total"]
    invoice.withholding_total = totals["withholding_total"]
    invoice.total = totals["total"]

    return invoice


def create_draft(database: Session, data: dict[str, Any], *, series: str = "F") -> SalesInvoice:
    invoice = SalesInvoice(series=series, status="DRAFT", lines=[])
    database.add(invoice)
    apply_draft_changes(database, invoice, data)
    database.flush()
    return invoice


def create_rectification(database: Session, original: SalesInvoice, reason: str | None) -> SalesInvoice:
    if original.status != "ISSUED":
        raise SalesError("Solo se rectifican facturas ya emitidas.")

    lines = [
        {**line, "quantity": -float(line["quantity"])}
        for line in (original.lines or [])
    ]
    draft = create_draft(
        database,
        {
            "customer_id": original.customer_id,
            "withholding_rate": original.withholding_rate,
            "lines": lines,
            "rectification_reason": reason or "Anulación de la factura original",
            "notes": f"Rectifica la factura {original.code} de {format_day(original.issue_date)}.",
        },
        series="R",
    )
    draft.rectifies_id = original.id
    return draft


# ---------------------------------------------------------------------
# Emisión y registro de facturación
# ---------------------------------------------------------------------


def verifactu_amount(value: Decimal) -> str:
    return f"{money(value):.2f}"


def record_hash(
    *,
    issuer_tax_id: str,
    code: str,
    issue_date: date,
    invoice_type: str,
    tax_total: Decimal,
    total: Decimal,
    previous_hash: str,
    timestamp: str,
) -> str:
    """Huella del registro de alta (SHA-256, hexadecimal en mayúsculas)."""
    chain = (
        f"IDEmisorFactura={issuer_tax_id}"
        f"&NumSerieFactura={code}"
        f"&FechaExpedicionFactura={issue_date.strftime('%d-%m-%Y')}"
        f"&TipoFactura={invoice_type}"
        f"&CuotaTotal={verifactu_amount(tax_total)}"
        f"&ImporteTotal={verifactu_amount(total)}"
        f"&Huella={previous_hash}"
        f"&FechaHoraHusoGenRegistro={timestamp}"
    )
    return hashlib.sha256(chain.encode("utf-8")).hexdigest().upper()


def qr_url(issuer_tax_id: str, code: str, issue_date: date, total: Decimal) -> str:
    return f"{QR_BASE_URL}?" + urlencode(
        {
            "nif": issuer_tax_id,
            "numserie": code,
            "fecha": issue_date.strftime("%d-%m-%Y"),
            "importe": verifactu_amount(total),
        }
    )


def last_issued(database: Session, series: str | None = None) -> SalesInvoice | None:
    statement = select(SalesInvoice).where(SalesInvoice.status == "ISSUED")
    if series:
        statement = statement.where(SalesInvoice.series == series)
    return database.scalar(statement.order_by(SalesInvoice.record_timestamp.desc(), SalesInvoice.id.desc()).limit(1))


def issue_invoice(
    database: Session,
    invoice: SalesInvoice,
    *,
    actor: str = "user",
    today: date | None = None,
) -> SalesInvoice:
    if invoice.status != "DRAFT":
        raise SalesError("La factura ya está emitida.")

    company = get_company(database)
    if not company or not company.tax_id or not company.name:
        raise SalesError("Completa el nombre y el NIF de tu empresa en «Mi empresa» antes de emitir facturas.")
    if not is_valid_spanish_tax_id(company.tax_id):
        raise SalesError("El NIF de tu empresa no es válido. Revísalo en «Mi empresa».")

    customer = database.get(Customer, invoice.customer_id) if invoice.customer_id else None
    if customer is None:
        raise SalesError("Elige un cliente para la factura.")
    if not invoice.lines:
        raise SalesError("Añade al menos una línea a la factura.")
    if invoice.series == "F" and money(invoice.total) <= ZERO:
        raise SalesError("El importe de una factura ordinaria debe ser positivo.")

    issue_day = invoice.issue_date or today or clock.today()
    previous_in_series = last_issued(database, invoice.series)
    if previous_in_series and previous_in_series.issue_date and issue_day < previous_in_series.issue_date:
        raise SalesError(
            f"La fecha no puede ser anterior a la última factura de la serie "
            f"({previous_in_series.code}, {format_day(previous_in_series.issue_date)})."
        )

    if invoice.series == "F" and not customer.tax_id and money(invoice.total) > Decimal("400"):
        raise SalesError("Para facturas de más de 400 € el cliente necesita NIF (factura completa).")

    year = issue_day.year
    # Cerrojo de la cadena de facturación: dos emisiones a la vez se ordenan aquí, así el número
    # y la huella anterior (previous_hash) se leen ya serializados y la cadena no se bifurca.
    from app.sequences import next_value

    next_value(database, "sales-chain")
    last_number = database.scalar(
        select(func.max(SalesInvoice.number)).where(SalesInvoice.series == invoice.series, SalesInvoice.year == year)
    )
    invoice.year = year
    invoice.number = (last_number or 0) + 1
    invoice.code = f"{invoice.series}{year}-{invoice.number:04d}"
    invoice.issue_date = issue_day
    invoice.due_date = invoice.due_date or issue_day + timedelta(days=default_payment_days(database, customer))
    invoice.customer_snapshot = customer_snapshot(customer)
    invoice.invoice_type = (
        ("R1" if invoice.rectifies_id else "R4") if invoice.series == "R" else ("F1" if customer.tax_id else "F2")
    )

    # Registro de facturación encadenado (toda la facturación del emisor).
    previous = last_issued(database)
    invoice.previous_hash = previous.record_hash if previous else ""
    invoice.record_timestamp = madrid_now().isoformat()
    invoice.record_hash = record_hash(
        issuer_tax_id=company.tax_id,
        code=invoice.code,
        issue_date=issue_day,
        invoice_type=invoice.invoice_type,
        tax_total=money(invoice.tax_total),
        total=money(invoice.total) + money(invoice.withholding_total),
        previous_hash=invoice.previous_hash,
        timestamp=invoice.record_timestamp,
    )
    invoice.qr_url = qr_url(company.tax_id, invoice.code, issue_day, money(invoice.total) + money(invoice.withholding_total))
    invoice.status = "ISSUED"

    pdf = build_invoice_pdf(invoice, company)
    record_in_ledger(database, invoice, company, pdf, actor=actor)

    add_audit_event(
        database,
        action="sales_invoice.issued",
        entity_type="sales_invoice",
        entity_id=invoice.id,
        actor=actor,
        event_data={"code": invoice.code, "total": invoice.total, "hash": invoice.record_hash},
    )

    return invoice


def record_in_ledger(
    database: Session,
    invoice: SalesInvoice,
    company: CompanyProfile,
    pdf: bytes,
    *,
    actor: str,
) -> None:
    """Guarda el PDF y crea la factura en el libro de emitidas."""
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}.pdf"
    (settings.upload_dir / stored).write_bytes(pdf)

    document = Document(
        original_filename=f"{invoice.code}.pdf",
        stored_filename=stored,
        sha256=hashlib.sha256(pdf).hexdigest(),
        mime_type="application/pdf",
        extension=".pdf",
        size_bytes=len(pdf),
        source="capafiscal_emision",
        status="APPROVED",
        extraction_status="COMPLETED",
        kind="INVOICE",
    )
    database.add(document)
    database.flush()

    snapshot = invoice.customer_snapshot or {}
    concept = "; ".join(line["description"] for line in (invoice.lines or []))[:500]
    ledger = Invoice(
        document_id=document.id,
        supplier_name=company.name,
        supplier_tax_id=company.tax_id,
        customer_name=snapshot.get("name"),
        customer_tax_id=snapshot.get("tax_id"),
        direction="ISSUED",
        invoice_number=invoice.code,
        invoice_date=invoice.issue_date,
        due_date=invoice.due_date,
        subtotal=invoice.subtotal,
        tax_total=invoice.tax_total,
        withholding_total=invoice.withholding_total,
        surcharge_total=ZERO,
        total=invoice.total,
        currency="EUR",
        concept=concept,
        category=DEFAULT_CATEGORY,
        confidence=100,
        field_confidences={},
        validation_status="VALID",
        validation_messages=[],
        review_status="APPROVED",
        duplicate_status="NONE",
        approved_at=clock.now(),
    )
    database.add(ledger)
    database.flush()

    for item in compute_totals(invoice.lines or [], invoice.withholding_rate)["breakdown"]:
        database.add(
            InvoiceTaxLine(
                invoice_id=ledger.id,
                tax_type="IVA",
                tax_rate=Decimal(str(item["rate"])),
                tax_base=money(item["base"]),
                tax_amount=money(item["tax"]),
                source="emision",
                confidence=100,
            )
        )

    invoice.document_id = document.id
    invoice.invoice_id = ledger.id

    add_audit_event(
        database,
        action="document.generated",
        entity_type="document",
        entity_id=document.id,
        actor=actor,
        event_data={"original_filename": document.original_filename, "source": "capafiscal_emision"},
    )


def invoice_pdf_bytes(database: Session, invoice: SalesInvoice) -> bytes:
    """PDF archivado (emitida) o vista previa (borrador)."""
    if invoice.status == "ISSUED" and invoice.document_id:
        document = database.get(Document, invoice.document_id)
        if document:
            path = settings.upload_dir / document.stored_filename
            if path.exists():
                return path.read_bytes()
    return build_invoice_pdf(invoice, get_company(database), draft=invoice.status != "ISSUED", database=database)


# ---------------------------------------------------------------------
# Serialización
# ---------------------------------------------------------------------


def collection_state(invoice: SalesInvoice, today: date) -> dict[str, Any]:
    ledger = invoice.invoice
    if invoice.status != "ISSUED" or ledger is None:
        return {"state": "draft", "label": "Borrador"}
    if ledger.paid_at:
        return {"state": "paid", "label": f"Cobrada {format_day(ledger.paid_at)}", "paid_at": ledger.paid_at.isoformat()}
    if money(invoice.total) <= ZERO:
        return {"state": "paid", "label": "Sin cobro (rectificativa)"}
    due = invoice.due_date or invoice.issue_date
    if due and due < today:
        return {"state": "overdue", "label": f"Vencida hace {(today - due).days} d", "days": (today - due).days}
    return {"state": "pending", "label": f"Vence {format_day(due)}"}


def serialize_invoice(invoice: SalesInvoice, today: date | None = None, *, full: bool = False) -> dict[str, Any]:
    today = today or clock.today()
    customer = invoice.customer_snapshot or (customer_snapshot(invoice.customer) if invoice.customer else {})
    data = {
        "id": invoice.id,
        "series": invoice.series,
        "code": invoice.code,
        "status": invoice.status,
        "invoice_type": invoice.invoice_type,
        "customer_id": invoice.customer_id,
        "customer_name": customer.get("name"),
        "customer_tax_id": customer.get("tax_id"),
        "customer_email": (invoice.customer.email if invoice.customer else None) or customer.get("email"),
        "issue_date": invoice.issue_date.isoformat() if invoice.issue_date else None,
        "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
        "subtotal": as_float(invoice.subtotal),
        "tax_total": as_float(invoice.tax_total),
        "withholding_rate": as_float(invoice.withholding_rate),
        "withholding_total": as_float(invoice.withholding_total),
        "total": as_float(invoice.total),
        "invoice_id": invoice.invoice_id,
        "document_id": invoice.document_id,
        "rectifies_id": invoice.rectifies_id,
        "recurring_id": invoice.recurring_id,
        "sent_at": invoice.sent_at.isoformat() if invoice.sent_at else None,
        "collection": collection_state(invoice, today),
        "concept": "; ".join(line["description"] for line in (invoice.lines or []))[:140],
    }

    if full:
        data.update(
            {
                "lines": invoice.lines or [],
                "breakdown": compute_totals(invoice.lines or [], invoice.withholding_rate)["breakdown"],
                "operation_date": invoice.operation_date.isoformat() if invoice.operation_date else None,
                "notes": invoice.notes,
                "payment_terms": invoice.payment_terms,
                "rectification_reason": invoice.rectification_reason,
                "rectifies_code": invoice.rectifies.code if invoice.rectifies else None,
                "record": {
                    "timestamp": invoice.record_timestamp,
                    "hash": invoice.record_hash,
                    "previous_hash": invoice.previous_hash,
                    "qr_url": invoice.qr_url,
                    "type_label": INVOICE_TYPES.get(invoice.invoice_type or "", None),
                    "remitted": False,
                }
                if invoice.record_hash
                else None,
            }
        )

    return data


def list_invoices(
    database: Session,
    *,
    status: str | None = None,
    query: str | None = None,
    year: int | None = None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    statement = select(SalesInvoice).order_by(
        SalesInvoice.status.asc(),  # DRAFT antes que ISSUED
        SalesInvoice.issue_date.desc().nulls_first(),
        SalesInvoice.id.desc(),
    )
    if status in {"DRAFT", "ISSUED"}:
        statement = statement.where(SalesInvoice.status == status)
    if year:
        statement = statement.where((SalesInvoice.year == year) | (SalesInvoice.status == "DRAFT"))

    items = [serialize_invoice(item, today) for item in database.scalars(statement).all()]

    if status in {"overdue", "pending", "paid"}:
        items = [item for item in items if item["collection"]["state"] == status]

    if query:
        text = query.lower()
        items = [
            item for item in items
            if text in " ".join(str(item.get(key) or "") for key in ("code", "customer_name", "customer_tax_id", "concept")).lower()
        ]

    return items


def sales_overview(database: Session, today: date | None = None) -> dict[str, Any]:
    today = today or clock.today()
    invoices = database.scalars(select(SalesInvoice)).all()
    issued = [item for item in invoices if item.status == "ISSUED"]
    year_items = [item for item in issued if item.year == today.year]
    month_items = [item for item in year_items if item.issue_date and item.issue_date.month == today.month]
    states = [collection_state(item, today) for item in issued]

    pending = sum(
        (money(item.total) for item, state in zip(issued, states) if state["state"] in {"pending", "overdue"}),
        ZERO,
    )
    overdue = sum((money(item.total) for item, state in zip(issued, states) if state["state"] == "overdue"), ZERO)

    recurring = database.scalars(select(RecurringInvoice).where(RecurringInvoice.active.is_(True))).all()
    monthly_recurring = ZERO
    for template in recurring:
        amount = compute_totals(template.lines or [], template.withholding_rate)["subtotal"]
        monthly_recurring += amount / Decimal(FREQUENCIES.get(template.frequency, ("", 1))[1])

    return {
        "drafts": sum(1 for item in invoices if item.status == "DRAFT"),
        "issued_month": float(sum((money(item.subtotal) for item in month_items), ZERO)),
        "issued_year": float(sum((money(item.subtotal) for item in year_items), ZERO)),
        "count_year": len(year_items),
        "pending": float(pending),
        "overdue": float(overdue),
        "recurring_active": len(recurring),
        "recurring_monthly": float(money(monthly_recurring)),
        "next_code": f"F{today.year}-{((database.scalar(select(func.max(SalesInvoice.number)).where(SalesInvoice.series == 'F', SalesInvoice.year == today.year)) or 0) + 1):04d}",
        "last_hash": (last_issued(database).record_hash if last_issued(database) else None),
    }


def verify_chain(database: Session) -> dict[str, Any]:
    """Recalcula todas las huellas y comprueba que la cadena no se ha roto."""
    company = get_company(database)
    items = database.scalars(
        select(SalesInvoice)
        .where(SalesInvoice.status == "ISSUED")
        .order_by(SalesInvoice.record_timestamp.asc(), SalesInvoice.id.asc())
    ).all()
    previous = ""

    for item in items:
        expected = record_hash(
            issuer_tax_id=company.tax_id if company else "",
            code=item.code or "",
            issue_date=item.issue_date,
            invoice_type=item.invoice_type or "F1",
            tax_total=money(item.tax_total),
            total=money(item.total) + money(item.withholding_total),
            previous_hash=previous,
            timestamp=item.record_timestamp or "",
        )
        if item.previous_hash != previous or item.record_hash != expected:
            return {"valid": False, "records": len(items), "broken_at": item.code}
        previous = item.record_hash

    return {"valid": True, "records": len(items), "broken_at": None}


# ---------------------------------------------------------------------
# Recurrentes
# ---------------------------------------------------------------------


def serialize_recurring(template: RecurringInvoice) -> dict[str, Any]:
    totals = compute_totals(template.lines or [], template.withholding_rate)
    return {
        "id": template.id,
        "name": template.name,
        "customer_id": template.customer_id,
        "customer_name": template.customer.name if template.customer else None,
        "lines": template.lines or [],
        "withholding_rate": as_float(template.withholding_rate),
        "frequency": template.frequency,
        "frequency_label": FREQUENCIES.get(template.frequency, (template.frequency,))[0],
        "next_date": template.next_date.isoformat(),
        "end_date": template.end_date.isoformat() if template.end_date else None,
        "auto_issue": template.auto_issue,
        "auto_send": template.auto_send,
        "active": template.active,
        "notes": template.notes,
        "generated_count": template.generated_count,
        "subtotal": float(totals["subtotal"]),
        "total": float(totals["total"]),
    }


def apply_recurring_changes(database: Session, template: RecurringInvoice, data: dict[str, Any]) -> RecurringInvoice:
    for field in ("name", "customer_id", "frequency", "next_date", "end_date", "auto_issue", "auto_send", "active", "notes"):
        if field in data:
            value = data[field]
            if isinstance(value, str):
                value = value.strip() or None
            setattr(template, field, value)

    if "withholding_rate" in data:
        template.withholding_rate = Decimal(str(data["withholding_rate"] or 0))
    if "lines" in data:
        template.lines = normalize_lines(data["lines"], allow_negative=False)

    if not template.name:
        raise SalesError("Pon un nombre a la factura recurrente (p. ej. «Cuota de mantenimiento»).")
    if template.frequency not in FREQUENCIES:
        raise SalesError("Periodicidad no válida.")
    if not template.customer_id or database.get(Customer, template.customer_id) is None:
        raise SalesError("Elige el cliente de la factura recurrente.")
    if not template.lines:
        raise SalesError("Añade al menos una línea.")
    if not template.next_date:
        raise SalesError("Indica la fecha de la próxima factura.")
    if template.auto_send and not template.auto_issue:
        template.auto_send = False

    return template


def generate_due_recurring(
    database: Session,
    *,
    today: date | None = None,
    actor: str = "agent",
) -> list[SalesInvoice]:
    """Crea (y, si está configurado, emite) las facturas recurrentes que tocan."""
    today = today or clock.today()
    created: list[SalesInvoice] = []
    templates = database.scalars(
        select(RecurringInvoice).where(RecurringInvoice.active.is_(True), RecurringInvoice.next_date <= today)
    ).all()

    for template in templates:
        guard = 0
        while template.next_date <= today and guard < 12:
            guard += 1
            if template.end_date and template.next_date > template.end_date:
                template.active = False
                break

            period = template.next_date
            lines = [{**line, "description": fill_template(line["description"], period)} for line in template.lines]
            draft = create_draft(
                database,
                {
                    "customer_id": template.customer_id,
                    "withholding_rate": template.withholding_rate,
                    "lines": lines,
                    "notes": template.notes,
                },
            )
            draft.recurring_id = template.id

            if template.auto_issue:
                draft.issue_date = today
                try:
                    issue_invoice(database, draft, actor=actor, today=today)
                except SalesError:
                    draft.issue_date = None  # queda en borrador para revisión

            created.append(draft)
            template.generated_count += 1
            template.last_generated_at = clock.now()
            months = FREQUENCIES[template.frequency][1]
            template.next_date = add_months(template.next_date, months)

        if template.end_date and template.next_date > template.end_date:
            template.active = False

    return created


# ---------------------------------------------------------------------
# PDF de la factura
# ---------------------------------------------------------------------


def build_invoice_pdf(
    invoice: SalesInvoice,
    company: CompanyProfile | None,
    *,
    draft: bool = False,
    database: Session | None = None,
) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    accent = HexColor("#8c1d33")
    ink = HexColor("#1c1a17")
    muted = HexColor("#6b675f")
    line_color = HexColor("#e5e1da")
    soft = HexColor("#f6f4f0")

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left, right = 18 * mm, width - 18 * mm

    snapshot = invoice.customer_snapshot
    if not snapshot and invoice.customer_id and database is not None:
        customer = database.get(Customer, invoice.customer_id)
        snapshot = customer_snapshot(customer) if customer else {}
    snapshot = snapshot or {}

    title = "FACTURA RECTIFICATIVA" if invoice.series == "R" else "FACTURA"
    pdf.setTitle(f"{title.title()} {invoice.code or 'borrador'}")

    # Cabecera: emisor
    y = height - 22 * mm
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(left, y, (company.name if company else None) or "Tu empresa")
    pdf.setFont("Helvetica", 8.5)
    pdf.setFillColor(muted)
    issuer_lines = [
        f"NIF {company.tax_id}" if company and company.tax_id else None,
        company.address if company else None,
        " ".join(filter(None, [company.postal_code, company.city])) if company else None,
        " · ".join(filter(None, [company.phone, company.email])) if company else None,
    ]
    for text in filter(None, issuer_lines):
        y -= 4.2 * mm
        pdf.drawString(left, y, text)

    pdf.setFillColor(accent)
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawRightString(right, height - 22 * mm, title)
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica", 9)
    meta = [
        ("Número", invoice.code or "BORRADOR"),
        ("Fecha", format_day(invoice.issue_date) if invoice.issue_date else "—"),
        ("Vencimiento", format_day(invoice.due_date) if invoice.due_date else "—"),
    ]
    if invoice.operation_date:
        meta.append(("Fecha operación", format_day(invoice.operation_date)))
    my = height - 29 * mm
    for label, value in meta:
        pdf.setFillColor(muted)
        pdf.drawRightString(right - 34 * mm, my, label)
        pdf.setFillColor(ink)
        pdf.drawRightString(right, my, value)
        my -= 4.6 * mm

    # Cliente
    y = min(y, my) - 10 * mm
    box_height = 26 * mm
    pdf.setFillColor(soft)
    pdf.setStrokeColor(line_color)
    pdf.roundRect(left, y - box_height, (right - left) / 2, box_height, 3 * mm, stroke=1, fill=1)
    pdf.setFillColor(muted)
    pdf.setFont("Helvetica-Bold", 7.5)
    pdf.drawString(left + 5 * mm, y - 6 * mm, "FACTURAR A")
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left + 5 * mm, y - 11.5 * mm, (snapshot.get("name") or "—")[:48])
    pdf.setFont("Helvetica", 8.5)
    cy = y - 16 * mm
    for text in filter(
        None,
        [
            f"NIF {snapshot['tax_id']}" if snapshot.get("tax_id") else None,
            snapshot.get("address"),
            " ".join(filter(None, [snapshot.get("postal_code"), snapshot.get("city")])) or None,
        ],
    ):
        pdf.drawString(left + 5 * mm, cy, text[:60])
        cy -= 4 * mm

    if invoice.series == "R" and invoice.rectification_reason:
        pdf.setFont("Helvetica", 8.5)
        pdf.setFillColor(muted)
        rx = left + (right - left) / 2 + 8 * mm
        pdf.drawString(rx, y - 6 * mm, "MOTIVO DE LA RECTIFICACIÓN")
        pdf.setFillColor(ink)
        pdf.drawString(rx, y - 11 * mm, invoice.rectification_reason[:55])

    # Líneas
    y = y - box_height - 12 * mm
    columns = [
        ("Concepto", left, "l"),
        ("Cant.", right - 78 * mm, "r"),
        ("Precio", right - 58 * mm, "r"),
        ("Dto.", right - 42 * mm, "r"),
        ("IVA", right - 28 * mm, "r"),
        ("Importe", right, "r"),
    ]
    pdf.setFont("Helvetica-Bold", 7.5)
    pdf.setFillColor(muted)
    for label, x, align in columns:
        (pdf.drawString if align == "l" else pdf.drawRightString)(x, y, label.upper())
    y -= 2.5 * mm
    pdf.setStrokeColor(line_color)
    pdf.line(left, y, right, y)

    pdf.setFont("Helvetica", 9)
    for line in invoice.lines or []:
        y -= 6 * mm
        if y < 70 * mm:
            pdf.showPage()
            y = height - 25 * mm
            pdf.setFont("Helvetica", 9)
        pdf.setFillColor(ink)
        pdf.drawString(left, y, str(line["description"])[:62])
        quantity = Decimal(str(line["quantity"]))
        pdf.drawRightString(right - 78 * mm, y, f"{quantity.normalize():f}".replace(".", ","))
        pdf.drawRightString(right - 58 * mm, y, format_eur(line["unit_price"]))
        pdf.drawRightString(right - 42 * mm, y, f"{line['discount']:g} %" if line.get("discount") else "—")
        pdf.drawRightString(right - 28 * mm, y, f"{line['vat_rate']:g} %")
        pdf.drawRightString(right, y, format_eur(line["amount"]))
        pdf.setStrokeColor(line_color)
        pdf.line(left, y - 2.5 * mm, right, y - 2.5 * mm)

    # Totales
    totals = compute_totals(invoice.lines or [], invoice.withholding_rate)
    y -= 12 * mm
    tx = right - 70 * mm

    def total_row(label: str, value: str, *, bold: bool = False) -> None:
        nonlocal y
        pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 10.5 if bold else 9)
        pdf.setFillColor(ink if bold else muted)
        pdf.drawString(tx, y, label)
        pdf.setFillColor(ink)
        pdf.drawRightString(right, y, value)
        y -= 5.5 * mm

    total_row("Base imponible", format_eur(totals["subtotal"]))
    for item in totals["breakdown"]:
        total_row(f"IVA {item['rate']:g} % sobre {format_eur(item['base'])}", format_eur(item["tax"]))
    if totals["withholding_total"]:
        total_row(f"Retención IRPF {Decimal(str(invoice.withholding_rate)):g} %", f"-{format_eur(totals['withholding_total'])}")
    pdf.setStrokeColor(accent)
    pdf.line(tx, y + 2.5 * mm, right, y + 2.5 * mm)
    y -= 1.5 * mm
    total_row("TOTAL", format_eur(totals["total"]), bold=True)

    # Forma de pago y notas
    y -= 6 * mm
    pdf.setFont("Helvetica", 8.5)
    pdf.setFillColor(muted)
    payment = invoice.payment_terms or (
        f"Transferencia a {' '.join(company.iban[i:i + 4] for i in range(0, len(company.iban), 4))}" if company and company.iban else "Transferencia bancaria"
    )
    pdf.drawString(left, y, f"Forma de pago: {payment[:110]}")
    if invoice.notes:
        for text in invoice.notes.splitlines()[:4]:
            y -= 4.5 * mm
            pdf.drawString(left, y, text[:120])

    # Pie: registro de facturación
    pdf.setStrokeColor(line_color)
    pdf.line(left, 24 * mm, right, 24 * mm)
    pdf.setFont("Helvetica", 7)
    pdf.setFillColor(muted)
    if draft:
        pdf.setFillColor(accent)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(left, 19 * mm, "BORRADOR · sin validez fiscal hasta su emisión")
    elif invoice.record_hash:
        pdf.drawString(left, 19 * mm, f"Registro de facturación {invoice.record_timestamp} · huella {invoice.record_hash[:32]}…")
    footer = (company.invoice_footer if company else None) or ""
    if footer:
        pdf.drawString(left, 15 * mm, footer[:150])

    if draft:
        pdf.saveState()
        pdf.setFillColor(HexColor("#8c1d33"))
        pdf.setFillAlpha(0.07)
        pdf.setFont("Helvetica-Bold", 90)
        pdf.translate(width / 2, height / 2)
        pdf.rotate(35)
        pdf.drawCentredString(0, 0, "BORRADOR")
        pdf.restoreState()

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def build_qr_svg(url: str) -> bytes:
    from reportlab.graphics import renderSVG
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing

    widget = QrCodeWidget(url, barLevel="M")
    x1, y1, x2, y2 = widget.getBounds()
    size = 160
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    return renderSVG.drawToString(drawing).encode("utf-8")
