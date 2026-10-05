"""
Posición fiscal continua: cómo va el trimestre HOY, no el día 20.

Para cada modelo que aplica a la empresa (303 / 130 / 111 / 115) y periodo:

    resultado estimado      el borrador con lo aprobado
    con lo pendiente        cuánto cambiaría si se aprueba lo que falta revisar
    información disponible  % del importe conocido del periodo que ya está aprobado
    lo que falta            facturas por revisar, facturas habituales que no han
                            llegado y movimientos del banco sin factura
    discrepancias           pagos que no cuadran con su factura y diferencia con
                            lo ya presentado

Es la base de la vigilancia de obligaciones: «Faltan 2 facturas para cerrar
el 303» se calcula aquí. Todo es determinista; no interviene la IA.
"""
from __future__ import annotations

from app import clock
import statistics
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calendar_es import add_months
from app.models import BankTransaction
from app.models import Invoice
from app.reports_service import quarter_range

QUARTERLY_MODELS = ("303", "130", "111", "115")
MIN_MOVEMENT = Decimal("50")  # movimientos menores no cuentan como hueco de información
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")


def eur(value: Any) -> str:
    text = f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def current_obligation_period(today: date) -> tuple[int, int]:
    """El trimestre que toca presentar ahora (el anterior al actual)."""
    quarter = (today.month - 1) // 3 + 1
    return (today.year, quarter - 1) if quarter > 1 else (today.year - 1, 4)


def build_draft(database: Session, model: str, year: int, quarter: int) -> dict[str, Any]:
    from app.tax_service import build_model_111
    from app.tax_service import build_model_115
    from app.tax_service import build_model_130
    from app.tax_service import build_model_303

    builders = {"303": build_model_303, "130": build_model_130, "111": build_model_111, "115": build_model_115}
    return builders[model](database, year=year, quarter=quarter)


def applicable_models(database: Session, year: int, quarter: int, today: date) -> list[str]:
    """Los modelos trimestrales que el calendario fiscal de la empresa incluye para ese periodo."""
    from app.tax_service import build_tax_calendar

    due_year = year + 1 if quarter == 4 else year
    entries = build_tax_calendar(database, year=due_year, today=today)["entries"]
    models = [entry["model"] for entry in entries if entry["model"] in QUARTERLY_MODELS and entry["period_year"] == year and entry["period"] == quarter]
    return [model for model in QUARTERLY_MODELS if model in models]


def missing_recurring(database: Session, year: int, quarter: int, today: date) -> list[dict[str, Any]]:
    """Proveedores que facturan cada mes y de los que falta algún mes ya cerrado del periodo."""
    date_from, date_to = quarter_range(year, quarter)
    history_from = add_months(date_from, -6)
    invoices = database.scalars(
        select(Invoice).where(
            Invoice.invoice_date.between(history_from, date_to), Invoice.review_status != "REJECTED",
            (Invoice.direction.is_(None)) | (Invoice.direction != "ISSUED"),
        )
    ).all()
    by_supplier: dict[str, list[Invoice]] = defaultdict(list)
    for invoice in invoices:
        key = invoice.supplier_tax_id or (invoice.supplier_name or "").strip().upper()
        if key:
            by_supplier[key].append(invoice)
    missing = []
    for key, items in by_supplier.items():
        months = {(item.invoice_date.year, item.invoice_date.month) for item in items}
        before = [(add_months(date_from, -offset).year, add_months(date_from, -offset).month) for offset in (1, 2, 3)]
        if not all(month in months for month in before):
            continue  # no es un proveedor mensual
        usual = Decimal(str(statistics.median(float(item.total or 0) for item in items)))
        name = items[-1].supplier_name or key
        for offset in range(3):
            month_start = add_months(date_from, offset)
            month_end = add_months(month_start, 1)
            if month_end > today:  # el mes aún no se ha cerrado
                continue
            if (month_start.year, month_start.month) not in months:
                missing.append({"supplier": name, "supplier_key": key, "month": f"{month_start.year}-{month_start.month:02d}",
                                "label": f"{name} · {MONTHS[month_start.month - 1]}", "usual": float(usual)})
    return missing


def position(database: Session, model: str, year: int, quarter: int, *, today: date | None = None) -> dict[str, Any]:
    from app.tax_service import filings_index
    from app.tax_service import quarterly_due_date

    today = today or clock.today()
    date_from, date_to = quarter_range(year, quarter)
    draft = build_draft(database, model, year, quarter)
    filing = filings_index(database).get((model, year, quarter))

    period_invoices = database.scalars(
        select(Invoice).where(Invoice.invoice_date.between(date_from, date_to), Invoice.review_status != "REJECTED")
    ).all()
    if model in {"111", "115"}:  # solo cuentan las facturas con retención
        period_invoices = [item for item in period_invoices if item.withholding_total]
    approved = [item for item in period_invoices if item.review_status == "APPROVED"]
    pending = [item for item in period_invoices if item.review_status != "APPROVED"]

    gaps: list[dict[str, Any]] = []
    for invoice in pending:
        gaps.append({"type": "pendiente_revision", "label": f"Factura {invoice.invoice_number or 's/n'} de {invoice.supplier_name or invoice.customer_name or '¿?'} sin revisar",
                     "amount": float(invoice.total or 0), "invoice_id": invoice.id})
    missing = missing_recurring(database, year, quarter, today) if model == "303" else []
    for item in missing:
        gaps.append({"type": "factura_falta", "label": f"No ha llegado la factura de {item['label']}", "amount": item["usual"], "supplier_key": item["supplier_key"]})
    unmatched = database.scalars(
        select(BankTransaction).where(BankTransaction.booking_date.between(date_from, date_to), BankTransaction.match_status == "UNMATCHED")
    ).all() if model == "303" else []
    for movement in unmatched:
        if abs(movement.amount) < MIN_MOVEMENT:
            continue
        gaps.append({"type": "movimiento_sin_factura", "label": f"{'Pago' if movement.amount < 0 else 'Cobro'} de {eur(abs(movement.amount))} del {movement.booking_date:%d/%m} sin factura",
                     "amount": float(abs(movement.amount)), "transaction_id": movement.id})

    discrepancies: list[dict[str, Any]] = []
    if model == "303":
        from app.reconciliation import amount_mismatches

        for item in amount_mismatches(database, date_from, date_to):
            discrepancies.append({"type": "importe_distinto", "label": item["label"], "amount": item["difference"]})
    result = Decimal(str(draft.get("result") or 0))
    if filing is not None and filing.amount is not None and abs(Decimal(str(filing.amount)) - result) >= 1:
        discrepancies.append({"type": "distinto_de_lo_presentado", "label": f"Presentado {eur(filing.amount)}; hoy tus datos dan {eur(result)}",
                              "amount": float(abs(Decimal(str(filing.amount)) - result))})

    known = sum((Decimal(str(abs(item.total or 0))) for item in approved), Decimal("0"))
    unknown = sum((Decimal(str(gap["amount"])) for gap in gaps), Decimal("0"))
    available = float(known / (known + unknown)) if known + unknown > 0 else 1.0

    # Cuánto cambiaría el 303 si se aprobase lo pendiente (IVA de lo que falta revisar).
    with_pending = None
    if model == "303" and pending:
        delta = Decimal("0")
        for invoice in pending:
            vat = Decimal(str(invoice.tax_total or 0))
            delta += vat if invoice.direction == "ISSUED" else -vat
        with_pending = float(result + delta)

    due = quarterly_due_date(year, quarter)
    complete = not gaps and not discrepancies
    outcome = draft.get("outcome") or ("A ingresar" if result > 0 else "Sin resultado")
    headline = f"{model} estimado: {eur(abs(result))} {outcome.lower()}" if model == "303" else f"{model} estimado: {eur(result)}"
    summary = [f"{round(available * 100)} % de información disponible"]
    counts = defaultdict(int)
    for gap in gaps:
        counts[gap["type"]] += 1
    if counts["pendiente_revision"]:
        summary.append(f"{counts['pendiente_revision']} factura(s) sin revisar")
    if counts["factura_falta"]:
        summary.append(f"faltan {counts['factura_falta']} factura(s) habituales")
    if counts["movimiento_sin_factura"]:
        summary.append(f"{counts['movimiento_sin_factura']} movimiento(s) del banco sin factura")
    if discrepancies:
        summary.append(f"{len(discrepancies)} discrepancia(s)")
    return {
        "model": model, "year": year, "quarter": quarter, "period_label": f"{quarter}T {year}", "due_date": due.isoformat(),
        "days_left": (due - today).days, "status": "FILED" if filing else ("COMPLETE" if complete else "INCOMPLETE"),
        "result": float(result), "outcome": outcome, "result_with_pending": with_pending,
        "information_available": round(available, 3), "gaps": gaps, "discrepancies": discrepancies,
        "headline": headline, "summary": " · ".join(summary), "filed": bool(filing),
        "approved_invoices": len(approved), "warnings": draft.get("warnings") or [],
    }


def positions(database: Session, *, year: int | None = None, quarter: int | None = None, today: date | None = None) -> dict[str, Any]:
    today = today or clock.today()
    if year is None or quarter is None:
        year, quarter = current_obligation_period(today)
    return {
        "year": year, "quarter": quarter, "period_label": f"{quarter}T {year}",
        "models": [position(database, model, year, quarter, today=today) for model in applicable_models(database, year, quarter, today)],
    }
