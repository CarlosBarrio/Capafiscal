"""
Tesorería predictiva: el Detector pasa de «esto es raro» a «esto va a pasar».

Parte de la previsión de caja de siempre (facturas pendientes, nóminas, impuestos) y la
corrige con lo que CapaFiscal sabe del negocio:

    retraso real     cada cliente cobra cuando suele pagar, no cuando vence la factura
    recurrentes      cargos que se repiten cada mes en el banco sin factura (alquiler,
                     préstamo, seguros, cuotas): se proyectan aunque aún no hayan llegado
    riesgo           punto más bajo de la caja, cuándo, por qué (qué sale antes) y qué
                     se puede hacer (cobros vencidos que reclamar, pagos que aplazar)

Solo lee. Las cifras son previsiones con supuestos a la vista, no certezas.
"""
from __future__ import annotations

import re
import statistics
from collections import defaultdict
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BankTransaction
from app.models import Invoice

LOOKBACK_DAYS = 125
MIN_MONTHS = 3
AMOUNT_SPREAD = Decimal("0.10")
CUSHION_SHARE = Decimal("0.25")  # colchón: una semana de gastos medios
ASSUMPTIONS_NOTE = ("Previsión con el comportamiento real: cada cliente cobra cuando suele pagar y se suman los cargos habituales",
                    "sin factura detectados en el banco. Pagos al vencimiento (sin vencimiento, factura + 30 días), nóminas, seguros",
                    "sociales e impuestos estimados.")


def eur(value: Any) -> str:
    text = f"{Decimal(str(value or 0)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def signature(description: str) -> str:
    """Concepto del banco sin números ni fechas: «RECIBO ALQUILER NAVE 09/2026» → «recibo alquiler nave»."""
    from app.extractor import normalize_search_text

    words = [word for word in re.split(r"[^a-z]+", normalize_search_text(description)) if len(word) >= 3]
    return " ".join(words[:4])


def add_month(value: date) -> date:
    import calendar

    year, month = (value.year, value.month + 1) if value.month < 12 else (value.year + 1, 1)
    return date(year, month, min(value.day, calendar.monthrange(year, month)[1]))


def recurring_outflows(database: Session, today: date, horizon: date, *, skip_payroll: bool) -> list[dict[str, Any]]:
    """Cargos mensuales sin factura detectados en el banco y su próxima fecha."""
    from app.allocations import nature

    since = today - timedelta(days=LOOKBACK_DAYS)
    rows = database.scalars(select(BankTransaction).where(BankTransaction.booking_date >= since, BankTransaction.amount < 0)).all()
    groups: dict[str, list[BankTransaction]] = defaultdict(list)
    for row in rows:
        if row.matched_invoice_id:  # lo que paga facturas ya lo prevén las facturas
            continue
        key = signature(row.description)
        if key:
            groups[key].append(row)
    result = []
    for key, items in groups.items():
        kind = nature(items[0])
        if kind in ("IMPUESTO", "TRASPASO", "DEVOLUCION") or (skip_payroll and kind in ("NOMINA", "SEG_SOCIAL")):
            continue  # los impuestos y las nóminas los prevé su propio calendario
        months = {(item.booking_date.year, item.booking_date.month) for item in items}
        if len(months) < MIN_MONTHS:
            continue
        amounts = [abs(Decimal(str(item.amount))) for item in items]
        median = Decimal(str(statistics.median(amounts)))
        if any(abs(amount - median) > median * AMOUNT_SPREAD for amount in amounts):
            continue  # importes que cambian mucho: no se puede prever con honestidad
        last = max(items, key=lambda item: item.booking_date)
        when = add_month(last.booking_date)
        while when < today:
            when = add_month(when)
        while when <= horizon:
            result.append({"date": when.isoformat(), "original_date": when.isoformat(), "overdue": False, "type": "recurring",
                           "label": f"Cargo habitual · {last.description[:50]}", "amount": -float(median), "document_id": None,
                           "estimated_date": True, "why": f"se ha cargado {len(months)} meses seguidos por {eur(median)}"})
            when = add_month(when)
    return result


def predict(database: Session, *, today: date | None = None, horizon_days: int = 60) -> dict[str, Any]:
    from app.bank_service import build_cashflow_forecast
    from app.bank_service import payroll_movements
    from app.dunning_service import average_delay_by_customer

    today = today or date.today()
    horizon = today + timedelta(days=horizon_days)
    base = build_cashflow_forecast(database, horizon_days=horizon_days, today=today)
    delays = average_delay_by_customer(database)
    invoices = {item.document_id: item for item in database.scalars(select(Invoice).where(Invoice.review_status == "APPROVED", Invoice.paid_at.is_(None))).all()}

    movements = []
    for movement in base["movements"]:
        movement = dict(movement)
        invoice = invoices.get(movement.get("document_id"))
        if movement["type"] == "collection" and invoice is not None:
            delay = delays.get(invoice.customer_tax_id or invoice.customer_name or "?")
            if delay and delay > 0:
                predicted = date.fromisoformat(movement["date"]) + timedelta(days=round(delay))
                movement.update(date=predicted.isoformat(), why=f"este cliente paga de media {round(delay)} días tarde")
        if date.fromisoformat(movement["date"]) <= horizon:
            movements.append(movement)
    movements += recurring_outflows(database, today, horizon, skip_payroll=bool(payroll_movements(database, today)))
    movements.sort(key=lambda item: (item["date"], item["amount"]))

    balance = base["current_balance"]
    running = Decimal(str(balance)) if balance is not None else None
    lowest = None
    for movement in movements:
        if running is None:
            break
        running += Decimal(str(movement["amount"]))
        movement["balance_after"] = float(running)
        if lowest is None or running < Decimal(str(lowest["balance"])):
            lowest = {"date": movement["date"], "balance": float(running)}

    outflows_30 = sum((-Decimal(str(item["amount"])) for item in movements if item["amount"] < 0 and item["date"] <= (today + timedelta(days=30)).isoformat()), Decimal("0"))
    cushion = (outflows_30 * CUSHION_SHARE).quantize(Decimal("0.01"))
    risk = risk_of(today, balance, lowest, cushion, movements)
    basis = confidence_of(movements, balance)
    warnings = [f"{risk['headline']}. {risk['explanation']}"] if risk["level"] in ("alto", "medio") else []
    warnings += [text for text in base["warnings"] if "negativo" not in text and "extracto bancario" not in text]
    return {
        "expected_inflows": float(sum(item["amount"] for item in movements if item["amount"] > 0)),
        "expected_outflows": float(-sum(item["amount"] for item in movements if item["amount"] < 0)),
        "warnings": warnings, "note": " ".join(ASSUMPTIONS_NOTE),
        "today": today.isoformat(), "horizon_days": horizon_days, "current_balance": balance, "balance_date": base["balance_date"],
        "projected_balance": float(running) if running is not None else None, "lowest_point": lowest, "cushion": float(cushion),
        "risk": risk, "movements": movements, "confidence": basis,
        "recurring": [item for item in movements if item["type"] == "recurring"],
        "delayed_collections": [item for item in movements if item.get("why", "").startswith("este cliente")],
        "assumptions": [
            "Cobros: en la fecha en que cada cliente suele pagar (retraso medio de sus facturas ya cobradas).",
            "Pagos: al vencimiento; sin vencimiento, fecha de factura + 30 días.",
            f"Cargos habituales sin factura: los que se repiten al menos {MIN_MONTHS} meses con importes parecidos (±10 %).",
            "Impuestos estimados con la posición fiscal; nóminas aprobadas y seguros sociales.",
            f"Colchón de seguridad: una cuarta parte de los pagos de los próximos 30 días ({eur(cushion)}).",
        ],
    }


def confidence_of(movements: list[dict[str, Any]], balance: float | None) -> dict[str, Any]:
    """Una cifra prevista nunca va sola: con qué confianza y de qué está hecha."""
    def count(kind: str) -> int:
        return sum(1 for item in movements if item["type"] == kind)

    total = sum(abs(item["amount"]) for item in movements) or 0
    guessed = sum(abs(item["amount"]) for item in movements
                  if item.get("estimated_date") or item["type"] in ("recurring", "tax") or item.get("why", "").startswith("este cliente"))
    share = guessed / total if total else 0
    level = "baja" if balance is None or share > 0.5 else "media" if share > 0.2 else "alta"
    parts = [(count("collection"), "cobro(s) previstos"), (count("payment"), "pago(s) de facturas"), (count("recurring"), "cargo(s) habituales"),
             (count("tax"), "impuesto(s) estimados"), (count("payroll") + count("social_security"), "nómina(s) y seguros sociales")]
    reasons = " + ".join(f"{number} {label}" for number, label in parts if number) or "sin movimientos previstos"
    why = ("sin saldo del banco" if balance is None else
           f"{round(share * 100)} % del importe es estimado (fechas supuestas, retrasos medios, cargos habituales o impuestos)")
    return {"level": level, "reasons": reasons, "why": why, "estimated_share": round(share, 2)}


def risk_of(today: date, balance: float | None, lowest: dict[str, Any] | None, cushion: Decimal, movements: list[dict[str, Any]]) -> dict[str, Any]:
    if balance is None:
        return {"level": "desconocido", "headline": "Sin saldo del banco no se puede prever la caja",
                "explanation": "Importa un extracto con la columna de saldo (o conecta el banco).", "actions": []}
    if lowest is None or Decimal(str(lowest["balance"])) >= cushion:
        return {"level": "bajo", "headline": "Sin riesgo de liquidez en el horizonte",
                "explanation": f"La caja no baja del colchón de seguridad ({eur(cushion)})." if lowest else "No hay movimientos previstos.", "actions": []}
    when = date.fromisoformat(lowest["date"])
    days = (when - today).days
    level = "alto" if lowest["balance"] < 0 else "medio"
    before = [item for item in movements if item["date"] <= lowest["date"]]
    outs = sorted((item for item in before if item["amount"] < 0), key=lambda item: item["amount"])[:3]
    ins = sum(item["amount"] for item in before if item["amount"] > 0)
    explanation = (f"La caja bajaría a {eur(lowest['balance'])} el {when:%d/%m/%Y} (en {days} días): antes de esa fecha salen "
                   + ", ".join(f"{item['label']} ({eur(-item['amount'])})" for item in outs)
                   + (f", y solo entran {eur(ins)}." if ins else ", y no hay cobros previstos."))
    actions = []
    overdue = [item for item in movements if item["type"] == "collection" and item.get("overdue")]
    if overdue:
        actions.append({"label": f"Reclamar {len(overdue)} cobro(s) vencido(s) por {eur(sum(item['amount'] for item in overdue))}", "tab": "ventas"})
    postponable = [item for item in outs if item["type"] == "payment"]
    for item in postponable[:2]:
        actions.append({"label": f"Negociar el aplazamiento de {item['label']} ({eur(-item['amount'])})", "tab": "negocio", "document_id": item.get("document_id")})
    headline = (f"Riesgo de liquidez en {days} días" if level == "alto" else f"La caja baja del colchón de seguridad en {days} días")
    return {"level": level, "headline": headline, "explanation": explanation, "date": lowest["date"], "days": days, "actions": actions}
