"""
«¿Qué cambia si…?»: antes de decidir, CapaFiscal enseña el efecto.

    aprobar una factura      → IVA del trimestre (303), caja y cierre del mes
    pagar hoy / aplazar      → caja: punto más bajo y riesgo de liquidez
    descartar una anomalía   → cierre del mes y lo que queda por decidir
    un gasto nuevo           → 303 (si lleva IVA) y caja

Se calcula con la misma lógica que el resto de CapaFiscal, aplicando el cambio dentro de un
SAVEPOINT que se deshace siempre: nada queda guardado.
"""
from __future__ import annotations

from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import Case
from app.models import Invoice

TYPES = ("approve_invoice", "pay_now", "postpone_payment", "collect_now", "dismiss_anomaly", "new_expense")


class SimulationError(ValueError):
    pass


def eur(value: Any) -> str:
    text = f"{Decimal(str(value or 0)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def quarter_of(value: date) -> tuple[int, int]:
    return value.year, (value.month - 1) // 3 + 1


def snapshot(database: Session, today: date, quarter: tuple[int, int]) -> dict[str, Any]:
    from app.closing import default_period
    from app.closing import evaluate
    from app.fiscal_position import position
    from app.treasury import predict

    cash = predict(database, today=today, horizon_days=60)
    vat = position(database, "303", quarter[0], quarter[1], today=today)
    close = evaluate(database, default_period(today), today=today)
    return {
        "cash_lowest": cash["lowest_point"]["balance"] if cash["lowest_point"] else None,
        "cash_lowest_date": cash["lowest_point"]["date"] if cash["lowest_point"] else None,
        "cash_end": cash["projected_balance"], "risk": cash["risk"]["level"],
        "vat_result": vat["result"], "vat_period": vat["period_label"],
        "close_blockers": close["blockers"], "close_percent": close["percent"],
    }


def target_invoice(database: Session, scenario: dict[str, Any]) -> Invoice:
    invoice = database.get(Invoice, scenario.get("invoice_id"))
    if invoice is None:
        raise SimulationError("Factura no encontrada.")
    return invoice


def apply(database: Session, scenario: dict[str, Any], today: date) -> tuple[str, tuple[int, int]]:
    kind = scenario.get("type")
    if kind == "approve_invoice":
        invoice = target_invoice(database, scenario)
        if invoice.review_status == "APPROVED":
            raise SimulationError("La factura ya está aprobada.")
        invoice.review_status = "APPROVED"
        return f"Aprobar la factura {invoice.invoice_number or 's/n'}", quarter_of(invoice.invoice_date or today)
    if kind in ("pay_now", "collect_now", "postpone_payment"):
        invoice = target_invoice(database, scenario)
        if invoice.paid_at:
            raise SimulationError("La factura ya está pagada.")
        if kind == "postpone_payment":
            days = int(scenario.get("days") or 30)
            invoice.due_date = (invoice.due_date or invoice.invoice_date or today) + timedelta(days=days)
            return f"Aplazar {days} días el pago de {invoice.invoice_number or 's/n'}", quarter_of(today)
        invoice.due_date = today
        return f"{'Cobrar' if kind == 'collect_now' else 'Pagar'} hoy {invoice.invoice_number or 's/n'}", quarter_of(today)
    if kind == "dismiss_anomaly":
        case = database.get(Case, scenario.get("case_id"))
        if case is None or case.kind != "ANOMALY":
            raise SimulationError("Anomalía no encontrada.")
        case.status = "DISMISSED"
        return f"Descartar la anomalía «{case.title}»", quarter_of(today)
    if kind == "new_expense":
        from app.models import Document

        amount = Decimal(str(scenario.get("amount") or 0))
        if amount <= 0:
            raise SimulationError("Indica el importe del gasto.")
        when = date.fromisoformat(scenario.get("date") or today.isoformat())
        rate = Decimal(str(scenario.get("vat_rate", 21)))
        base = (amount / (1 + rate / 100)).quantize(Decimal("0.01"))
        document = Document(original_filename="simulacion.pdf", stored_filename="simulacion.pdf", sha256="0" * 64, extension=".pdf", size_bytes=0,
                            status="APPROVED", extraction_status="COMPLETED", kind="INVOICE")
        database.add(document)
        database.flush()
        database.add(Invoice(document_id=document.id, supplier_name=scenario.get("label") or "Gasto simulado", direction="RECEIVED", invoice_number="SIMULADO",
                             invoice_date=when, due_date=when, subtotal=base, tax_total=amount - base, total=amount, currency="EUR", confidence=100,
                             field_confidences={}, validation_status="VALID", validation_messages=[], review_status="APPROVED", duplicate_status="NONE"))
        return f"Un gasto de {eur(amount)} el {when:%d/%m/%Y}", quarter_of(when)
    raise SimulationError(f"Simulación no válida. Opciones: {', '.join(TYPES)}.")


def explain(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    lines = []
    vat = Decimal(str(after["vat_result"])) - Decimal(str(before["vat_result"]))
    if vat:
        effect = f"pagas {eur(vat)} más" if vat > 0 else f"ahorras {eur(-vat)}"
        lines.append(f"Modelo 303 {after['vat_period']}: {eur(before['vat_result'])} → {eur(after['vat_result'])} ({effect}).")
    if before["cash_lowest"] is not None and after["cash_lowest"] is not None and before["cash_lowest"] != after["cash_lowest"]:
        lines.append(f"Punto más bajo de la caja: {eur(before['cash_lowest'])} → {eur(after['cash_lowest'])}"
                     + (f" (el {date.fromisoformat(after['cash_lowest_date']):%d/%m})" if after["cash_lowest_date"] else "") + ".")
    if before["risk"] != after["risk"]:
        lines.append(f"Riesgo de liquidez: {before['risk']} → {after['risk']}.")
    if before["close_blockers"] != after["close_blockers"]:
        lines.append(f"Cierre del mes: {before['close_blockers']} → {after['close_blockers']} bloqueo(s) ({before['close_percent']} % → {after['close_percent']} % cerrado).")
    return lines or ["No cambia ni el IVA, ni la caja prevista, ni el cierre del mes."]


def simulate(database: Session, scenario: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    savepoint = database.begin_nested()
    try:
        title, quarter = apply(database, scenario, today)
        database.flush()
        after = snapshot(database, today, quarter)
    finally:
        savepoint.rollback()
        database.expire_all()
    before = snapshot(database, today, quarter)
    return {"scenario": scenario, "title": title, "before": before, "after": after, "effects": explain(before, after),
            "note": "Simulación: no se ha guardado nada."}
