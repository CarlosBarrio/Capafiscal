"""
Cobros y reclamación de impagos.

El agente revisa cada día las facturas emitidas vencidas y prepara la
reclamación que toca, de menos a más firme:

  1. Recordatorio amable          (desde el día siguiente al vencimiento)
  2. Segundo aviso                (a partir de 15 días de retraso)
  3. Requerimiento formal de pago (a partir de 30 días), con carta en PDF,
     intereses de demora y, entre empresas, la indemnización de 40 € por
     costes de cobro (arts. 7 y 8 de la Ley 3/2004).

Nada se envía sin que una persona lo revise en la bandeja de salida.
"""
from __future__ import annotations

import io
from datetime import date
from datetime import timedelta
from decimal import ROUND_HALF_UP
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CompanyProfile
from app.models import Customer
from app.models import Invoice
from app.models import OutboxMessage
from app.models import SalesInvoice
from app.outbox_service import create_message

ZERO = Decimal("0")
CENT = Decimal("0.01")
COMPENSATION = Decimal("40.00")
DEFAULT_TERM_DAYS = 30

LEVELS = {
    1: {"days": 1, "label": "Recordatorio amable"},
    2: {"days": 15, "label": "Segundo aviso"},
    3: {"days": 30, "label": "Requerimiento formal"},
}

# Interés de demora en operaciones comerciales (tipo BCE + 8 puntos),
# publicado cada semestre en el BOE. Los tipos sin publicar se estiman
# con el último conocido; se puede fijar otro en «Mi empresa».
LATE_INTEREST = {
    (2023, 1): Decimal("10.50"),
    (2023, 2): Decimal("12.00"),
    (2024, 1): Decimal("12.50"),
    (2024, 2): Decimal("12.25"),
    (2025, 1): Decimal("11.15"),
    (2025, 2): Decimal("10.15"),
}


def money(value: Any) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def semester_rate(year: int, half: int, override: Decimal | None) -> tuple[Decimal, bool]:
    if override is not None:
        return Decimal(override), True
    if (year, half) in LATE_INTEREST:
        return LATE_INTEREST[(year, half)], True
    latest = max(LATE_INTEREST)
    return LATE_INTEREST[latest], False


def late_interest(
    amount: Decimal,
    due: date,
    today: date,
    override: Decimal | None = None,
) -> dict[str, Any]:
    """Intereses desde el día siguiente al vencimiento, tramo a tramo por semestre."""
    start = due + timedelta(days=1)
    total = ZERO
    estimated = False
    current = start

    while current <= today:
        half = 1 if current.month <= 6 else 2
        semester_end = date(current.year, 6, 30) if half == 1 else date(current.year, 12, 31)
        end = min(semester_end, today)
        days = (end - current).days + 1
        rate, known = semester_rate(current.year, half, override)
        estimated = estimated or not known
        total += amount * rate / Decimal("100") * Decimal(days) / Decimal("365")
        current = end + timedelta(days=1)

    rate_today, _ = semester_rate(today.year, 1 if today.month <= 6 else 2, override)
    return {
        "amount": money(total),
        "rate": float(rate_today),
        "estimated_rate": estimated,
        "days": max(0, (today - due).days),
    }


def is_business(tax_id: str | None) -> bool:
    """CIF de persona jurídica (la indemnización de 40 € es entre empresas)."""
    return bool(tax_id) and tax_id[0].upper() in "ABCDEFGHJNPQRSUVW"


def due_date_of(invoice: Invoice) -> date | None:
    if invoice.due_date:
        return invoice.due_date
    if invoice.invoice_date:
        return invoice.invoice_date + timedelta(days=DEFAULT_TERM_DAYS)
    return None


def find_customer(database: Session, invoice: Invoice) -> Customer | None:
    if invoice.customer_tax_id:
        customer = database.scalar(select(Customer).where(Customer.tax_id == invoice.customer_tax_id))
        if customer:
            return customer
    if invoice.customer_name:
        return database.scalar(select(Customer).where(Customer.name == invoice.customer_name))
    return None


def reminders_for(database: Session, invoice_id: int) -> list[OutboxMessage]:
    return list(
        database.scalars(
            select(OutboxMessage)
            .where(
                OutboxMessage.kind == "DUNNING",
                OutboxMessage.entity_type == "invoice",
                OutboxMessage.entity_id == invoice_id,
                OutboxMessage.status != "DISCARDED",
            )
            .order_by(OutboxMessage.created_at.asc())
        ).all()
    )


def unpaid_issued(database: Session) -> list[Invoice]:
    return list(
        database.scalars(
            select(Invoice).where(
                Invoice.direction == "ISSUED",
                Invoice.review_status == "APPROVED",
                Invoice.paid_at.is_(None),
                Invoice.total > 0,
            )
        ).all()
    )


def company_override(database: Session) -> Decimal | None:
    company = database.scalar(select(CompanyProfile).limit(1))
    return company.late_interest_rate if company and company.late_interest_rate is not None else None


def target_level(days_overdue: int) -> int:
    level = 0
    for number, config in LEVELS.items():
        if days_overdue >= config["days"]:
            level = number
    return level


def average_delay_by_customer(database: Session) -> dict[str, float]:
    """Días medios de retraso en las facturas ya cobradas de cada cliente."""
    rows = database.scalars(
        select(Invoice).where(
            Invoice.direction == "ISSUED",
            Invoice.paid_at.is_not(None),
        )
    ).all()
    delays: dict[str, list[int]] = {}
    for invoice in rows:
        due = due_date_of(invoice)
        if not due:
            continue
        key = invoice.customer_tax_id or invoice.customer_name or "?"
        delays.setdefault(key, []).append((invoice.paid_at - due).days)
    return {key: round(sum(values) / len(values), 1) for key, values in delays.items()}


def collections_overview(database: Session, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    override = company_override(database)
    delays = average_delay_by_customer(database)
    rows: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []

    for invoice in unpaid_issued(database):
        due = due_date_of(invoice)
        if due is None:
            continue
        customer = find_customer(database, invoice)
        email = customer.email if customer else None
        reminders = reminders_for(database, invoice.id)
        sent_levels = [item.level or 0 for item in reminders]
        last = reminders[-1] if reminders else None
        key = invoice.customer_tax_id or invoice.customer_name or "?"
        base = {
            "invoice_id": invoice.id,
            "document_id": invoice.document_id,
            "number": invoice.invoice_number,
            "customer_name": invoice.customer_name,
            "customer_tax_id": invoice.customer_tax_id,
            "customer_id": customer.id if customer else None,
            "email": email,
            "total": float(money(invoice.total)),
            "due_date": due.isoformat(),
            "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
            "average_delay": delays.get(key),
        }

        if due >= today:
            if (due - today).days <= 7:
                upcoming.append({**base, "days_left": (due - today).days})
            continue

        days = (today - due).days
        interest = late_interest(money(invoice.total), due, today, override)
        level_due = target_level(days)
        current_level = max(sent_levels, default=0)
        rows.append(
            {
                **base,
                "days_overdue": days,
                "interest": float(interest["amount"]),
                "interest_rate": interest["rate"],
                "interest_estimated": interest["estimated_rate"],
                "compensation": float(COMPENSATION) if is_business(invoice.customer_tax_id) else 0.0,
                "level_sent": current_level,
                "level_due": level_due,
                "next_action": LEVELS[level_due]["label"] if level_due > current_level else None,
                "last_reminder": {
                    "id": last.id,
                    "status": last.status,
                    "level": last.level,
                    "created_at": last.created_at.isoformat() if last.created_at else None,
                    "sent_at": last.sent_at.isoformat() if last.sent_at else None,
                }
                if last
                else None,
            }
        )

    rows.sort(key=lambda item: -item["days_overdue"])
    upcoming.sort(key=lambda item: item["days_left"])
    by_customer: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["customer_tax_id"] or row["customer_name"] or "?"
        entry = by_customer.setdefault(
            key,
            {"customer_name": row["customer_name"], "customer_tax_id": row["customer_tax_id"], "amount": 0.0, "count": 0, "max_days": 0, "average_delay": row["average_delay"]},
        )
        entry["amount"] = round(entry["amount"] + row["total"], 2)
        entry["count"] += 1
        entry["max_days"] = max(entry["max_days"], row["days_overdue"])

    total_overdue = sum(row["total"] for row in rows)
    return {
        "overdue": rows,
        "upcoming": upcoming,
        "customers": sorted(by_customer.values(), key=lambda item: -item["amount"]),
        "summary": {
            "count": len(rows),
            "amount": round(total_overdue, 2),
            "interest": round(sum(row["interest"] for row in rows), 2),
            "pending_actions": sum(1 for row in rows if row["next_action"]),
            "weighted_days": round(sum(row["total"] * row["days_overdue"] for row in rows) / total_overdue, 1) if total_overdue else 0,
        },
        "levels": {str(key): value for key, value in LEVELS.items()},
    }


# ---------------------------------------------------------------------
# Redacción de los mensajes
# ---------------------------------------------------------------------


def format_eur(value: Any) -> str:
    from app.sales_service import format_eur as fmt

    return fmt(value)


def reminder_text(level: int, invoice: Invoice, company: CompanyProfile | None, due: date, today: date, override: Decimal | None) -> tuple[str, str]:
    from app.sales_service import format_day

    company_name = (company.name if company else None) or ""
    number = invoice.invoice_number or "sin número"
    amount = format_eur(invoice.total)
    days = (today - due).days
    iban = f"\n\nCuenta para el pago: {company.iban}" if company and company.iban else ""

    if level == 1:
        subject = f"Recordatorio: factura {number} pendiente de pago"
        body = (
            f"Hola,\n\n"
            f"Te escribimos porque, según nuestros registros, la factura {number} de "
            f"{format_day(invoice.invoice_date)} por {amount} venció el {format_day(due)} y todavía "
            f"no nos consta el pago.\n\n"
            f"Si ya la habéis pagado, ignora este mensaje (y gracias). Si no, ¿nos confirmas cuándo "
            f"podréis hacerlo?{iban}\n\n"
            f"Un saludo,\n{company_name}"
        )
    elif level == 2:
        subject = f"Segundo aviso: factura {number} vencida hace {days} días"
        body = (
            f"Hola,\n\n"
            f"Seguimos sin recibir el pago de la factura {number} por {amount}, vencida el "
            f"{format_day(due)} ({days} días de retraso).\n\n"
            f"Te agradeceríamos que regularizaseis el pago en los próximos 7 días o que nos "
            f"indiques si hay alguna incidencia con la factura.{iban}\n\n"
            f"Un saludo,\n{company_name}"
        )
    else:
        interest = late_interest(money(invoice.total), due, today, override)
        compensation = (
            f"\n- Indemnización por costes de cobro (art. 8 Ley 3/2004): {format_eur(COMPENSATION)}"
            if is_business(invoice.customer_tax_id)
            else ""
        )
        subject = f"Requerimiento de pago · factura {number}"
        body = (
            f"Estimados señores:\n\n"
            f"A pesar de nuestros avisos anteriores, la factura {number} por {amount}, vencida el "
            f"{format_day(due)}, continúa impagada ({days} días de retraso).\n\n"
            f"Les requerimos formalmente su pago en el plazo de 10 días. Conforme a la Ley 3/2004, "
            f"de medidas de lucha contra la morosidad en las operaciones comerciales, la deuda "
            f"devenga:\n"
            f"- Principal: {amount}\n"
            f"- Intereses de demora al {interest['rate']:.2f} % anual hasta hoy: {format_eur(interest['amount'])}"
            f"{compensation}\n\n"
            f"Adjuntamos la carta de requerimiento. De no recibir el pago, nos veremos obligados a "
            f"iniciar las acciones legales oportunas para su reclamación.{iban}\n\n"
            f"Atentamente,\n{company_name}"
        )

    return subject, body


def prepare_reminders(
    database: Session,
    *,
    today: date | None = None,
    invoice_ids: set[int] | None = None,
    created_by: str = "agent",
) -> list[OutboxMessage]:
    """Prepara, para cada factura vencida, la reclamación del nivel que toca."""
    today = today or date.today()
    company = database.scalar(select(CompanyProfile).limit(1))
    override = company_override(database)
    created: list[OutboxMessage] = []

    for invoice in unpaid_issued(database):
        if invoice_ids is not None and invoice.id not in invoice_ids:
            continue
        due = due_date_of(invoice)
        if not due or due >= today:
            continue

        level = target_level((today - due).days)
        existing = reminders_for(database, invoice.id)
        if any((item.level or 0) >= level for item in existing):
            continue
        # No se salta de nivel si el anterior sigue sin enviar: se sustituye.
        for item in existing:
            if item.status in {"DRAFT", "FAILED"}:
                item.status = "DISCARDED"

        customer = find_customer(database, invoice)
        subject, body = reminder_text(level, invoice, company, due, today, override)
        attachments: list[dict[str, Any]] = []
        sales = database.scalar(select(SalesInvoice).where(SalesInvoice.invoice_id == invoice.id))
        if sales:
            attachments.append({"type": "sales_invoice", "id": sales.id, "filename": f"{sales.code}.pdf"})
        elif invoice.document_id:
            attachments.append({"type": "document", "id": invoice.document_id, "filename": f"factura_{invoice.invoice_number or invoice.id}.pdf"})
        if level == 3:
            attachments.append({"type": "dunning_letter", "id": invoice.id, "filename": f"requerimiento_{invoice.invoice_number or invoice.id}.pdf"})

        created.append(
            create_message(
                database,
                kind="DUNNING",
                subject=subject,
                body=body,
                to_email=customer.email if customer else None,
                to_name=invoice.customer_name,
                attachments=attachments,
                entity_type="invoice",
                entity_id=invoice.id,
                level=level,
                created_by=created_by,
            )
        )

    return created


def build_letter_pdf(database: Session, invoice_id: int, today: date | None = None) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    from app.sales_service import format_day

    today = today or date.today()
    invoice = database.get(Invoice, invoice_id)
    if invoice is None:
        raise ValueError("Factura no encontrada.")
    company = database.scalar(select(CompanyProfile).limit(1))
    customer = find_customer(database, invoice)
    due = due_date_of(invoice) or today
    interest = late_interest(money(invoice.total), due, today, company_override(database))
    compensation = COMPENSATION if is_business(invoice.customer_tax_id) else ZERO
    total_claim = money(invoice.total) + interest["amount"] + compensation

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left, right = 22 * mm, width - 22 * mm
    ink, muted, accent = HexColor("#1c1a17"), HexColor("#6b675f"), HexColor("#8c1d33")
    pdf.setTitle(f"Requerimiento de pago {invoice.invoice_number or ''}")

    y = height - 25 * mm
    pdf.setFont("Helvetica-Bold", 12)
    pdf.setFillColor(ink)
    pdf.drawString(left, y, (company.name if company else None) or "")
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(muted)
    for text in filter(None, [
        f"NIF {company.tax_id}" if company and company.tax_id else None,
        company.address if company else None,
        " ".join(filter(None, [company.postal_code, company.city])) if company else None,
    ]):
        y -= 4.5 * mm
        pdf.drawString(left, y, text)

    y -= 14 * mm
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(width / 2, y, invoice.customer_name or "")
    pdf.setFont("Helvetica", 9)
    for text in filter(None, [
        f"NIF {invoice.customer_tax_id}" if invoice.customer_tax_id else None,
        customer.address if customer else None,
        " ".join(filter(None, [customer.postal_code, customer.city])) if customer else None,
    ]):
        y -= 4.5 * mm
        pdf.drawString(width / 2, y, text)

    y -= 14 * mm
    city = (company.city if company else None) or ""
    pdf.drawRightString(right, y, f"{city + ', ' if city else ''}{format_day(today)}")
    y -= 12 * mm
    pdf.setFillColor(accent)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(left, y, "REQUERIMIENTO FORMAL DE PAGO")
    pdf.setFillColor(ink)
    pdf.setFont("Helvetica", 10)

    paragraphs = [
        "Muy señores nuestros:",
        f"Por medio de la presente les requerimos el pago de la factura {invoice.invoice_number or ''} "
        f"de fecha {format_day(invoice.invoice_date)}, vencida el {format_day(due)} y pendiente de pago "
        f"a día de hoy ({interest['days']} días de retraso).",
        "De acuerdo con la Ley 3/2004, de 29 de diciembre, por la que se establecen medidas de lucha contra "
        "la morosidad en las operaciones comerciales, el importe adeudado es el siguiente:",
    ]
    for paragraph in paragraphs:
        y -= 9 * mm
        y = draw_wrapped(pdf, paragraph, left, y, right - left, 10)

    y -= 8 * mm
    rows = [
        ("Principal (factura)", format_eur(invoice.total)),
        (f"Intereses de demora ({interest['rate']:.2f} % anual, {interest['days']} días)", format_eur(interest["amount"])),
    ]
    if compensation:
        rows.append(("Indemnización por costes de cobro (art. 8)", format_eur(compensation)))
    for label, value in rows:
        pdf.setFont("Helvetica", 10)
        pdf.drawString(left + 6 * mm, y, label)
        pdf.drawRightString(right - 6 * mm, y, value)
        y -= 6 * mm
    pdf.setStrokeColor(accent)
    pdf.line(left + 6 * mm, y + 3.5 * mm, right - 6 * mm, y + 3.5 * mm)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left + 6 * mm, y - 1 * mm, "TOTAL RECLAMADO")
    pdf.drawRightString(right - 6 * mm, y - 1 * mm, format_eur(total_claim))

    y -= 14 * mm
    closing = [
        "Les rogamos procedan al pago en el plazo máximo de DIEZ DÍAS desde la recepción de esta carta"
        + (f", mediante transferencia a la cuenta {company.iban}." if company and company.iban else "."),
        "Transcurrido dicho plazo sin haber recibido el pago, nos reservamos el derecho a ejercitar las "
        "acciones judiciales que correspondan para el cobro de la deuda, incluido el proceso monitorio, "
        "con los gastos que ello conlleve.",
        "Atentamente,",
    ]
    for paragraph in closing:
        y = draw_wrapped(pdf, paragraph, left, y, right - left, 10) - 7 * mm

    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left, y - 6 * mm, (company.name if company else None) or "")
    if interest["estimated_rate"]:
        pdf.setFont("Helvetica", 7)
        pdf.setFillColor(muted)
        pdf.drawString(left, 15 * mm, "Tipo de interés estimado con el último publicado en el BOE; revísalo en «Mi empresa» si ya hay uno nuevo.")

    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def draw_wrapped(pdf: Any, text: str, x: float, y: float, width: float, size: float) -> float:
    from reportlab.pdfbase.pdfmetrics import stringWidth

    words = text.split()
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if stringWidth(candidate, "Helvetica", size) > width:
            pdf.drawString(x, y, line)
            y -= size * 0.5 * 2.83
            line = word
        else:
            line = candidate
    if line:
        pdf.drawString(x, y, line)
    return y
