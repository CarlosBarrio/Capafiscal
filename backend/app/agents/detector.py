"""
Detector de anomalías: un motor de comprobaciones reutilizable.

Cruza facturas, banco e histórico y avisa cuando algo no cuadra con lo
habitual. Estadística robusta (mediana y MAD), sin cajas negras: cada aviso
es un ``Finding`` con qué detectó, por qué, con qué datos, nivel de riesgo,
confianza y qué debería pasar después.

El mismo motor se usa de tres formas:
  · ``check_invoice``   una factura concreta (al llegar: caso «factura sospechosa»)
  · ``scan``            barrido de toda la empresa (cada mañana)
  · el agente, dentro de cualquier ruta del orquestador

Tipos (ANOMALY_TYPES): importe atípico, duplicado, IVA atípico, pago sin
factura, cobro sin factura, factura sin pago, proveedor nuevo, cambio de
comportamiento, factura que falta, patrón interrumpido y tendencia del IVA.

Aprende de las personas: si alguien ya revisó algo parecido del mismo
proveedor y lo dio por correcto, el aviso baja de riesgo y lo dice.
"""
from __future__ import annotations

from app import clock
import math
import statistics
from collections import defaultdict
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import RISK_ORDER
from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import Finding
from app.agents.base import StepResult
from app.agents.base import eur
from app.agents.base import evidence
from app.agents.base import jsonable
from app.agents.base import run_step
from app.calendar_es import add_months
from app.models import AgentRun
from app.models import BankTransaction
from app.models import Case
from app.models import CaseEvent
from app.models import Invoice

RECENT_DAYS = 60
MAD_THRESHOLD = 3.5
BANK_MIN_AMOUNT = 300
BANK_MIN_AGE_DAYS = 10
UNPAID_MIN_AMOUNT = 300
UNPAID_GRACE_DAYS = 15
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")

SEVERITY = {"high": 60, "medium": 45, "low": 30}

ANOMALY_TYPES: dict[str, dict[str, str]] = {
    "IMPORTE_ATIPICO": {"label": "Importe atípico", "next": "Comprueba el concepto con el proveedor antes de pagarla o aprobarla."},
    "POSIBLE_DUPLICADO": {"label": "Posible duplicado", "next": "Compara las dos facturas y, si es el mismo servicio, rechaza una y pide el abono."},
    "IVA_INUSUAL": {"label": "IVA atípico", "next": "Revisa el tipo de IVA de la factura; si es un error, pide una rectificativa antes de deducirlo."},
    "PAGO_SIN_FACTURA": {"label": "Pago sin factura", "next": "Localiza la factura del pago y súbela, o márcalo como no deducible."},
    "COBRO_SIN_FACTURA": {"label": "Cobro sin factura", "next": "Emite o registra la factura de ese cobro."},
    "MOVIMIENTO_SIN_FACTURA": {"label": "Movimiento sin factura", "next": "Localiza o emite la factura del movimiento."},
    "FACTURA_SIN_PAGO": {"label": "Factura sin pago", "next": "Confirma si está pagada (y concilia el pago) o prográmala antes de que genere recargos."},
    "PROVEEDOR_NUEVO": {"label": "Proveedor nuevo", "next": "Verifica el NIF y la cuenta de pago del proveedor antes de pagar."},
    "CAMBIO_COMPORTAMIENTO": {"label": "Cambio de comportamiento", "next": "Pregunta al proveedor por qué ha cambiado su forma de facturar."},
    "FACTURA_FALTA": {"label": "Factura que falta", "next": "Pídela al proveedor o búscala en el correo: sin ella no puedes deducir el IVA."},
    "PATRON_INTERRUMPIDO": {"label": "Patrón interrumpido", "next": "Confirma si el servicio o la relación se ha dado de baja o si faltan facturas."},
    "IVA_TENDENCIA": {"label": "IVA fuera de tendencia", "next": "Revisa si faltan facturas recibidas o si hay ventas atípicas antes de presentar el 303."},
    "IMPORTE_DIFERENTE": {"label": "Pago con importe distinto", "next": "Compara el pago con la factura: puede faltar un abono, un recargo o haber un error."},
    "PAGO_DUPLICADO": {"label": "Pago duplicado", "next": "Comprueba si se ha pagado dos veces y pide la devolución al proveedor."},
    "OBLIGACION_INCOMPLETA": {"label": "Obligación incompleta", "next": "Completa lo que falta antes del plazo para que el borrador sea fiable."},
    "GASTO_ANORMAL": {"label": "Gasto anormal", "next": "Revisa qué proveedores explican la subida y si es puntual o un cambio de tendencia."},
    "CAIDA_FACTURACION": {"label": "Caída de facturación", "next": "Comprueba si faltan facturas por emitir o si ha caído la actividad, y prevé la tesorería."},
    "CLIENTE_DEJA_DE_PAGAR": {"label": "Cliente que deja de pagar", "next": "Reclama las facturas vencidas y valora pedir pago anticipado antes de seguir sirviéndole."},
}


def received_invoices(database: Session) -> list[Invoice]:
    return list(
        database.scalars(
            select(Invoice).where(
                (Invoice.direction.is_(None)) | (Invoice.direction != "ISSUED"),
                Invoice.review_status != "REJECTED",
                Invoice.total.is_not(None),
                Invoice.invoice_date.is_not(None),
            )
        ).all()
    )


def supplier_key(invoice: Invoice) -> str | None:
    return invoice.supplier_tax_id or (invoice.supplier_name or "").strip().upper() or None


def supplier_invoices(database: Session, invoice: Invoice) -> list[Invoice]:
    """Histórico del proveedor de una factura (incluida ella), por fecha."""
    key = supplier_key(invoice)
    if not key:
        return [invoice]
    condition = Invoice.supplier_tax_id == key if invoice.supplier_tax_id else Invoice.supplier_name == invoice.supplier_name
    items = [
        item for item in database.scalars(
            select(Invoice).where(
                condition,
                (Invoice.direction.is_(None)) | (Invoice.direction != "ISSUED"),
                Invoice.total.is_not(None),
                Invoice.invoice_date.is_not(None),
            )
        ).all()
        if item.id == invoice.id or item.review_status != "REJECTED"
    ]
    if invoice not in items:
        items.append(invoice)
    return sorted(items, key=lambda item: (item.invoice_date or date.min, item.id))


def invoice_rate(invoice: Invoice) -> float | None:
    if invoice.tax_lines:
        rates = {float(line.tax_rate) for line in invoice.tax_lines if line.tax_rate is not None}
        if len(rates) == 1:
            return rates.pop()
    if invoice.subtotal and invoice.tax_total is not None and float(invoice.subtotal):
        return round(float(invoice.tax_total) / float(invoice.subtotal) * 100)
    return None


def invoice_label(invoice: Invoice) -> str:
    return f"{invoice.supplier_name or 'Proveedor'} · {invoice.invoice_number or 's/n'}"


def anomaly(
    fingerprint: str,
    procedure: str,
    title: str,
    detail: str,
    severity: str,
    *,
    amount: Any = None,
    facts: dict[str, Any] | None = None,
    evidence_items: list | None = None,
    confidence: float = 0.8,
    next_step: str | None = None,
    document_id: int | None = None,
    when: date | None = None,
) -> dict[str, Any]:
    """Un aviso del detector. Lleva dentro su ``Finding`` estándar."""
    facts = facts or {}
    finding = Finding(
        agente="detector",
        tipo=procedure,
        resultado=title,
        por_que=detail,
        riesgo=severity,
        confianza=confidence,
        datos={key: value for key, value in facts.items() if key not in {"document_id"}},
        evidencia=evidence_items or [],
        documento_origen=document_id if document_id is not None else facts.get("document_id"),
        fecha=when.isoformat() if when else None,
        siguiente=next_step or ANOMALY_TYPES.get(procedure, {}).get("next"),
    )
    return {
        "fingerprint": fingerprint,
        "procedure": procedure,
        "title": title,
        "detail": detail,
        "severity": severity,
        "amount": float(amount) if amount is not None else None,
        "facts": facts,
        "evidence": evidence_items or [],
        "finding": finding.to_dict(),
    }


def to_finding(item: dict[str, Any]) -> Finding:
    data = item["finding"]
    return Finding(
        agente=data["agente"], tipo=data["tipo"], resultado=data["resultado"], por_que=data["por_que"],
        riesgo=data["riesgo"], confianza=data["confianza"], datos=data["datos"], evidencia=data["evidencia"],
        documento_origen=data["documento_origen"], fecha=data["fecha"], siguiente=data["siguiente"],
    )


# ---------------------------------------------------------------------
# Comprobaciones sobre una factura (con el histórico de su proveedor)
# ---------------------------------------------------------------------


def check_atypical(invoice: Invoice, items: list[Invoice]) -> list[dict[str, Any]]:
    history_items = [item for item in items if item.id != invoice.id and item.invoice_date < invoice.invoice_date and float(item.total) > 0]
    history = [float(item.total) for item in history_items]
    if len(history) < 4 or float(invoice.total) <= 0:
        return []
    logs = [math.log(value) for value in history]
    median = statistics.median(logs)
    mad = statistics.median([abs(value - median) for value in logs]) or 0.05
    z = 0.6745 * (math.log(float(invoice.total)) - median) / mad
    usual = math.exp(median)
    if abs(z) < MAD_THRESHOLD or abs(float(invoice.total) - usual) < 50:
        return []
    ratio = float(invoice.total) / usual
    name = invoice.supplier_name or supplier_key(invoice)
    percent = round((ratio - 1) * 100)
    sources = [item.invoice_number or f"#{item.id}" for item in history_items[-4:]]
    comparison = f"un {percent} % superior a" if ratio > 1 else "muy por debajo de"
    return [
        anomaly(
            f"atipico:{invoice.id}",
            "IMPORTE_ATIPICO",
            f"Factura de {name} fuera de lo habitual",
            f"La factura {invoice.invoice_number or ''} de {invoice.invoice_date:%d/%m/%Y} es de {eur(invoice.total)}, "
            f"{comparison} lo habitual ({eur(usual)} de mediana en {len(history)} facturas).",
            "high" if ratio >= 3 or ratio <= 0.2 else "medium",
            amount=invoice.total,
            facts={
                "invoice_id": invoice.id, "document_id": invoice.document_id, "supplier_key": supplier_key(invoice),
                "median": round(usual, 2), "history": len(history), "z": round(z, 2), "ratio": round(ratio, 2),
                "percent": percent, "sources": sources,
            },
            evidence_items=[evidence("invoice", invoice_label(invoice), document_id=invoice.document_id)]
            + [evidence("invoice", f"Histórico: {invoice_label(item)} · {eur(item.total)}", document_id=item.document_id) for item in history_items[-3:]],
            confidence=min(0.95, 0.6 + len(history) * 0.05),
            when=invoice.invoice_date,
        )
    ]


def check_new_supplier(invoice: Invoice, items: list[Invoice], p90: float | None) -> list[dict[str, Any]]:
    others = [item for item in items if item.id != invoice.id]
    if others or not p90 or float(invoice.total) < max(p90, 1000):
        return []
    name = invoice.supplier_name or supplier_key(invoice)
    return [
        anomaly(
            f"nuevo:{invoice.id}",
            "PROVEEDOR_NUEVO",
            f"Proveedor nuevo con un importe alto: {name}",
            f"Primera factura de {name} y ya es de {eur(invoice.total)} (más que el 90 % de tus facturas, {eur(p90)}).",
            "medium",
            amount=invoice.total,
            facts={"invoice_id": invoice.id, "document_id": invoice.document_id, "supplier_key": supplier_key(invoice), "p90": p90},
            evidence_items=[evidence("invoice", invoice_label(invoice), document_id=invoice.document_id)],
            confidence=0.7,
            when=invoice.invoice_date,
        )
    ]


def check_vat(invoice: Invoice, items: list[Invoice]) -> list[dict[str, Any]]:
    rate = invoice_rate(invoice)
    known = [value for value in (invoice_rate(item) for item in items if item.id != invoice.id) if value is not None]
    if rate is None or len(known) < 3:
        return []
    usual_rate = max(set(known), key=known.count)
    share = known.count(usual_rate) / len(known)
    if rate == usual_rate or share < 0.75:
        return []
    name = invoice.supplier_name or supplier_key(invoice)
    return [
        anomaly(
            f"iva:{invoice.id}",
            "IVA_INUSUAL",
            f"IVA inusual en una factura de {name}",
            f"La factura {invoice.invoice_number or ''} aplica un {rate:g} % de IVA, pero {name} factura al {usual_rate:g} % en el {share:.0%} de los casos. Puede ser un error en la factura o en la lectura.",
            "medium",
            amount=invoice.tax_total,
            facts={"invoice_id": invoice.id, "document_id": invoice.document_id, "supplier_key": supplier_key(invoice), "rate": rate, "usual_rate": usual_rate, "share": round(share, 2)},
            evidence_items=[evidence("invoice", invoice_label(invoice), document_id=invoice.document_id)],
            confidence=round(0.5 + share * 0.4, 2),
            when=invoice.invoice_date,
        )
    ]


def check_duplicate(invoice: Invoice, items: list[Invoice], *, include_flagged: bool = False) -> list[dict[str, Any]]:
    name = invoice.supplier_name or supplier_key(invoice)
    if include_flagged and invoice.duplicate_status in {"STRONG", "PROBABLE"}:
        original = next((item for item in items if item.id == invoice.duplicate_of_invoice_id), None)
        same_number = invoice.duplicate_status == "STRONG"
        return [
            anomaly(
                f"duplicado:{invoice.duplicate_of_invoice_id}:{invoice.id}",
                "POSIBLE_DUPLICADO",
                f"Factura duplicada de {name}",
                (f"Ya tienes registrada la factura {invoice.invoice_number} de {name}: mismo proveedor y mismo número."
                 if same_number else
                 f"Ya hay una factura de {name} del mismo día y por el mismo importe ({eur(invoice.total)})."),
                "high",
                amount=invoice.total,
                facts={"invoice_id": invoice.id, "invoice_ids": [invoice.duplicate_of_invoice_id, invoice.id], "document_id": invoice.document_id, "supplier_key": supplier_key(invoice), "match": invoice.duplicate_status.lower()},
                evidence_items=[evidence("invoice", invoice_label(item), document_id=item.document_id) for item in (original, invoice) if item],
                confidence=0.95 if same_number else 0.85,
                when=invoice.invoice_date,
            )
        ]
    if invoice.duplicate_status != "NONE":
        return []  # ya lo avisa la revisión de facturas
    same_amount = [item for item in items if item.id != invoice.id and item.total == invoice.total]
    if len(same_amount) >= 3:
        return []  # cuota fija (alquiler, gestoría, suscripción): repetir importe es lo normal
    for other in items:
        if (
            other.id != invoice.id
            and (other.invoice_date, other.id) < (invoice.invoice_date, invoice.id)
            and other.total == invoice.total
            and other.invoice_number != invoice.invoice_number
            and abs((invoice.invoice_date - other.invoice_date).days) <= 45
        ):
            return [
                anomaly(
                    f"duplicado:{other.id}:{invoice.id}",
                    "POSIBLE_DUPLICADO",
                    f"Posible factura duplicada de {name}",
                    f"{invoice.invoice_number or 's/n'} ({invoice.invoice_date:%d/%m}) y {other.invoice_number or 's/n'} ({other.invoice_date:%d/%m}) tienen el mismo importe, {eur(invoice.total)}. Comprueba que no se trata del mismo servicio facturado dos veces.",
                    "medium",
                    amount=invoice.total,
                    facts={"invoice_id": invoice.id, "invoice_ids": [other.id, invoice.id], "document_id": invoice.document_id, "supplier_key": supplier_key(invoice), "days_apart": abs((invoice.invoice_date - other.invoice_date).days)},
                    evidence_items=[evidence("invoice", invoice_label(item), document_id=item.document_id) for item in (other, invoice)],
                    confidence=0.6,
                    when=invoice.invoice_date,
                )
            ]
    return []


def check_behavior(invoice: Invoice, items: list[Invoice]) -> list[dict[str, Any]]:
    """Proveedor que factura una vez al mes y de pronto factura varias veces seguidas."""
    earlier = [item for item in items if (item.invoice_date, item.id) < (invoice.invoice_date, invoice.id)]
    if len(earlier) < 4:
        return []
    gaps = [(b.invoice_date - a.invoice_date).days for a, b in zip(earlier, earlier[1:])]
    usual_gap = statistics.median(gaps) if gaps else 0
    if usual_gap < 25:
        return []
    burst = [item for item in items if abs((item.invoice_date - invoice.invoice_date).days) <= 10 and item.id != invoice.id and item.invoice_date <= invoice.invoice_date]
    if len(burst) < 2:
        return []
    name = invoice.supplier_name or supplier_key(invoice)
    return [
        anomaly(
            f"comportamiento:{invoice.id}",
            "CAMBIO_COMPORTAMIENTO",
            f"{name} ha cambiado su forma de facturar",
            f"Suele facturarte cada {round(usual_gap)} días y en los últimos 10 días te ha enviado {len(burst) + 1} facturas.",
            "medium",
            amount=invoice.total,
            facts={"invoice_id": invoice.id, "document_id": invoice.document_id, "supplier_key": supplier_key(invoice), "usual_gap_days": round(usual_gap), "recent_count": len(burst) + 1},
            evidence_items=[evidence("invoice", invoice_label(item), document_id=item.document_id) for item in [*burst, invoice]],
            confidence=0.65,
            when=invoice.invoice_date,
        )
    ]


def check_unpaid(invoice: Invoice, today: date, last_bank_date: date | None) -> list[dict[str, Any]]:
    """Factura recibida vencida y sin pago en el banco (solo si el banco llega hasta después del vencimiento)."""
    if invoice.paid_at or invoice.review_status == "REJECTED" or float(invoice.total or 0) < UNPAID_MIN_AMOUNT:
        return []
    due = invoice.due_date or invoice.invoice_date + timedelta(days=60)
    if today < due + timedelta(days=UNPAID_GRACE_DAYS) or last_bank_date is None or last_bank_date < due + timedelta(days=UNPAID_GRACE_DAYS):
        return []
    name = invoice.supplier_name or supplier_key(invoice)
    days = (today - due).days
    return [
        anomaly(
            f"sinpago:{invoice.id}",
            "FACTURA_SIN_PAGO",
            f"Factura de {name} vencida y sin pago",
            f"La factura {invoice.invoice_number or ''} de {eur(invoice.total)} venció el {due:%d/%m/%Y} (hace {days} días) y no aparece ningún pago en el banco.",
            "medium" if days > 30 or float(invoice.total) >= 1000 else "low",
            amount=invoice.total,
            facts={"invoice_id": invoice.id, "document_id": invoice.document_id, "supplier_key": supplier_key(invoice), "due": due, "days_overdue": days, "bank_until": last_bank_date},
            evidence_items=[evidence("invoice", invoice_label(invoice), document_id=invoice.document_id)],
            confidence=0.7 if invoice.due_date else 0.55,
            when=due,
        )
    ]


def check_invoice(database: Session, invoice: Invoice, today: date, *, include_flagged: bool = True) -> list[dict[str, Any]]:
    """Todas las comprobaciones de una factura concreta."""
    if invoice.total is None or invoice.invoice_date is None:
        return []
    items = supplier_invoices(database, invoice)
    totals = sorted(float(item.total) for item in received_invoices(database) if item.total)
    p90 = totals[int(len(totals) * 0.9)] if len(totals) >= 10 else None
    findings = (
        check_duplicate(invoice, items, include_flagged=include_flagged)
        + check_atypical(invoice, items)
        + check_new_supplier(invoice, items, p90)
        + check_vat(invoice, items)
        + check_behavior(invoice, items)
    )
    return apply_feedback(database, findings)


# ---------------------------------------------------------------------
# Comprobaciones periódicas (patrones, banco, IVA)
# ---------------------------------------------------------------------


def check_missing(key: str, items: list[Invoice], today: date, *, party: str = "supplier") -> list[dict[str, Any]]:
    """Factura mensual que no ha llegado (1 mes) o patrón que se ha cortado (2+ meses)."""
    if today.day < 10 or not items:
        return []
    name = (items[-1].supplier_name if party == "supplier" else items[-1].customer_name) or key
    months_seen = {(item.invoice_date.year, item.invoice_date.month) for item in items}
    first_of_month = today.replace(day=1)

    def month(offset: int) -> tuple[int, int]:
        value = add_months(first_of_month, -offset)
        return value.year, value.month

    # Cuántos meses seguidos faltan (desde el pasado) y si antes era regular.
    gap = 0
    while gap < 6 and month(gap + 1) not in months_seen:
        gap += 1
    if gap == 0:
        return []
    regular = all(month(gap + offset) in months_seen for offset in range(1, 4))
    if not regular:
        return []
    usual = statistics.median(float(item.total) for item in items[-6:])
    last = month(1)
    if gap == 1 and party == "supplier":
        return [
            anomaly(
                f"falta:{key}:{last[0]}-{last[1]:02d}",
                "FACTURA_FALTA",
                f"No ha llegado la factura de {MONTHS[last[1] - 1]} de {name}",
                f"{name} te factura todos los meses (aprox. {eur(usual)}) y la de {MONTHS[last[1] - 1]} no está. Sin ella no puedes deducirte el IVA.",
                "low",
                amount=usual,
                facts={"supplier": name, "supplier_key": key, "month": f"{last[0]}-{last[1]:02d}"},
                confidence=0.75,
            )
        ]
    if gap < 2:
        return []
    since = month(gap)
    who = "te facturaba" if party == "supplier" else "te compraba"
    return [
        anomaly(
            f"patron:{party}:{key}:{since[0]}-{since[1]:02d}",
            "PATRON_INTERRUMPIDO",
            f"{name} ya no {'factura' if party == 'supplier' else 'compra'} como antes",
            f"{name} {who} todos los meses (aprox. {eur(usual)}) y lleva {gap} meses sin {'facturas' if party == 'supplier' else 'ventas'} (desde {MONTHS[since[1] - 1]}).",
            "medium" if party == "customer" else "low",
            amount=usual,
            facts={"party": party, "name": name, "supplier_key": key, "months_missing": gap, "since": f"{since[0]}-{since[1]:02d}"},
            confidence=0.7,
            next_step=None if party == "supplier" else "Contacta con el cliente: puede haberse ido a la competencia o tener un problema de pago.",
        )
    ]


def bank_findings(database: Session, today: date) -> list[dict[str, Any]]:
    """Excepciones de la conciliación: sin factura, importe distinto y pago duplicado."""
    from app.reconciliation import reconcile

    report = reconcile(database, today=today, auto=False)
    transactions = {item.id: item for item in database.scalars(select(BankTransaction).where(BankTransaction.id.in_([row["transaction_id"] for row in report["movements"]]))).all()}
    findings = []
    for row in report["movements"]:
        transaction = transactions.get(row["transaction_id"])
        if transaction is None or transaction.booking_date < today - timedelta(days=120):
            continue
        amount = float(transaction.amount)
        outflow = amount < 0
        when = f"El {transaction.booking_date:%d/%m/%Y}: «{transaction.description[:90]}». "
        bank_evidence = [evidence("bank", f"{transaction.booking_date:%d/%m/%Y} · {transaction.description[:60]} · {eur(amount)}", transaction_id=transaction.id)]
        base_facts = {"transaction_id": transaction.id, "direction": "out" if outflow else "in", "description": transaction.description[:120], "booking_date": transaction.booking_date}
        if row["state"] == "SIN_FACTURA":
            if abs(amount) < BANK_MIN_AMOUNT or transaction.booking_date > today - timedelta(days=BANK_MIN_AGE_DAYS):
                continue
            findings.append(anomaly(
                f"banco:{transaction.id}", "PAGO_SIN_FACTURA" if outflow else "COBRO_SIN_FACTURA",
                f"{'Pago' if outflow else 'Cobro'} de {eur(abs(amount))} sin factura",
                when + ("Si es un gasto de la actividad, falta la factura para deducirlo." if outflow else "Si es una venta, falta emitir o registrar la factura."),
                "medium" if abs(amount) >= 1000 else "low", amount=abs(amount), facts=base_facts, evidence_items=bank_evidence, confidence=0.7, when=transaction.booking_date,
            ))
        elif row["state"] == "IMPORTE_DISTINTO":
            difference = abs(row["difference"] or 0)
            findings.append(anomaly(
                f"importe:{transaction.id}:{row['invoice_id']}", "IMPORTE_DIFERENTE",
                f"{'Pago' if outflow else 'Cobro'} de {eur(abs(amount))} que no cuadra con la factura {row['invoice_label']}",
                when + f"Se reconoce la factura {row['invoice_label']} ({', '.join(row['evidence'])}), pero la diferencia es de {eur(difference)}.",
                "medium" if difference >= 100 else "low", amount=difference, facts={**base_facts, "invoice_id": row["invoice_id"], "difference": row["difference"]},
                evidence_items=bank_evidence, confidence=0.75, when=transaction.booking_date,
            ))
        elif row["state"] == "DUPLICADO":
            findings.append(anomaly(
                f"duplicado:{transaction.id}", "PAGO_DUPLICADO",
                f"Posible {'pago' if outflow else 'cobro'} duplicado de {eur(abs(amount))}",
                when + f"Mismo concepto e importe que otro movimiento de pocos días antes (movimiento {row['duplicate_of']}).",
                "high" if abs(amount) >= 1000 else "medium", amount=abs(amount), facts={**base_facts, "duplicate_of": row["duplicate_of"]},
                evidence_items=bank_evidence, confidence=0.8, when=transaction.booking_date,
            ))
    return findings


OBLIGATION_LEAD_DAYS = 25


def obligation_findings(database: Session, today: date) -> list[dict[str, Any]]:
    """Vigilancia proactiva: obligaciones que vencen pronto y a las que les falta información."""
    from app.fiscal_position import positions

    report = positions(database, today=today)
    findings = []
    for item in report["models"]:
        if item["filed"] or item["status"] == "COMPLETE" or not 0 <= item["days_left"] <= OBLIGATION_LEAD_DAYS:
            continue
        missing = [gap["label"] for gap in item["gaps"]] + [gap["label"] for gap in item["discrepancies"]]
        findings.append(anomaly(
            f"obligacion:{item['model']}:{item['year']}-{item['quarter']}", "OBLIGACION_INCOMPLETA",
            f"Faltan {len(missing)} cosa(s) para cerrar el {item['model']} del {item['period_label']}",
            f"Vence el {date.fromisoformat(item['due_date']):%d/%m/%Y} (en {item['days_left']} días). {item['headline']} · {item['summary']}. Falta: " + "; ".join(missing[:5]) + ("…" if len(missing) > 5 else "."),
            "high" if item["days_left"] <= 7 else "medium", amount=item["result"],
            facts={"model": item["model"], "year": item["year"], "quarter": item["quarter"], "gaps": item["gaps"], "discrepancies": item["discrepancies"],
                   "information_available": item["information_available"], "due": item["due_date"]},
            confidence=0.9,
        ))
    return findings


def monthly_totals(invoices: list[Invoice]) -> dict[tuple[int, int], float]:
    totals: dict[tuple[int, int], float] = defaultdict(float)
    for invoice in invoices:
        totals[(invoice.invoice_date.year, invoice.invoice_date.month)] += abs(float(invoice.total or 0))
    return totals


def trend_findings(database: Session, today: date) -> list[dict[str, Any]]:
    """Cambios globales: gasto del último mes anormalmente alto o facturación anormalmente baja."""
    last = add_months(today.replace(day=1), -1)
    previous = [add_months(last, -offset) for offset in range(1, 7)]
    findings = []
    invoices = database.scalars(select(Invoice).where(Invoice.invoice_date >= previous[-1], Invoice.invoice_date < today.replace(day=1),
                                                      Invoice.review_status != "REJECTED", Invoice.total.is_not(None))).all()
    for direction in ("RECEIVED", "ISSUED"):
        selected = [item for item in invoices if (item.direction == "ISSUED") == (direction == "ISSUED")]
        totals = monthly_totals(selected)
        history = [totals.get((month.year, month.month), 0.0) for month in previous]
        if sum(1 for value in history if value > 0) < 4:
            continue
        usual = statistics.median(history)
        current = totals.get((last.year, last.month), 0.0)
        label = f"{MONTHS[last.month - 1]} {last.year}"
        if direction == "RECEIVED" and usual > 0 and current > usual * 1.5 and current - usual >= 500:
            by_supplier: dict[str, float] = defaultdict(float)
            usual_by_supplier: dict[str, list[float]] = defaultdict(list)
            for item in selected:
                key = item.supplier_name or supplier_key(item) or "¿?"
                if (item.invoice_date.year, item.invoice_date.month) == (last.year, last.month):
                    by_supplier[key] += abs(float(item.total))
            for month in previous:
                month_items = [item for item in selected if (item.invoice_date.year, item.invoice_date.month) == (month.year, month.month)]
                for key in by_supplier:
                    usual_by_supplier[key].append(sum(abs(float(item.total)) for item in month_items if (item.supplier_name or supplier_key(item) or "¿?") == key))
            drivers = sorted(((key, value - statistics.median(usual_by_supplier[key])) for key, value in by_supplier.items()), key=lambda pair: -pair[1])[:3]
            explanation = "; ".join(f"{key} (+{eur(delta)})" for key, delta in drivers if delta > 0)
            findings.append(anomaly(
                f"gasto:{last.year}-{last.month:02d}", "GASTO_ANORMAL",
                f"El gasto de {label} es {round(current / usual, 1)} veces el habitual",
                f"Gastos recibidos en {label}: {eur(current)}; lo normal en los 6 meses anteriores es {eur(usual)}. Lo explican sobre todo: {explanation or 'varios proveedores'}.",
                "high" if current > usual * 2.5 else "medium", amount=current - usual,
                facts={"month": f"{last.year}-{last.month:02d}", "current": round(current, 2), "median": round(usual, 2), "drivers": drivers, "ratio": round(current / usual, 2)},
                confidence=0.75,
            ))
        if direction == "ISSUED" and usual >= 500 and current < usual * 0.6:
            findings.append(anomaly(
                f"facturacion:{last.year}-{last.month:02d}", "CAIDA_FACTURACION",
                f"La facturación de {label} ha caído un {round((1 - current / usual) * 100)} %",
                f"Facturado en {label}: {eur(current)}; lo normal en los 6 meses anteriores es {eur(usual)}.",
                "high" if current < usual * 0.4 else "medium", amount=usual - current,
                facts={"month": f"{last.year}-{last.month:02d}", "current": round(current, 2), "median": round(usual, 2), "party": "customer"},
                confidence=0.7,
            ))
    return findings


def customer_payment_findings(database: Session, today: date) -> list[dict[str, Any]]:
    """Clientes que pagaban y ahora acumulan facturas vencidas sin cobrar."""
    issued = database.scalars(select(Invoice).where(Invoice.direction == "ISSUED", Invoice.invoice_date.is_not(None), Invoice.total.is_not(None),
                                                    Invoice.review_status != "REJECTED")).all()
    by_customer: dict[str, list[Invoice]] = defaultdict(list)
    for invoice in issued:
        key = invoice.customer_tax_id or (invoice.customer_name or "").strip().upper()
        if key:
            by_customer[key].append(invoice)
    findings = []
    for key, items in by_customer.items():
        paid = [item for item in items if item.paid_at]
        overdue = [item for item in items if not item.paid_at and (item.due_date or item.invoice_date + timedelta(days=30)) < today - timedelta(days=15)]
        if len(paid) < 2 or not overdue:
            continue
        usual_days = statistics.median((item.paid_at - item.invoice_date).days for item in paid)
        oldest = min(overdue, key=lambda item: item.invoice_date)
        waiting = (today - oldest.invoice_date).days
        amount = sum(float(item.total) for item in overdue)
        if not (len(overdue) >= 2 or waiting > usual_days + 30) or amount < 300:
            continue
        name = items[-1].customer_name or key
        findings.append(anomaly(
            f"cliente_pago:{key}:{oldest.id}", "CLIENTE_DEJA_DE_PAGAR",
            f"{name} ha dejado de pagar a tiempo",
            f"Solía pagar en unos {round(usual_days)} días y ahora tiene {len(overdue)} factura(s) vencida(s) sin cobrar por {eur(amount)}; la más antigua lleva {waiting} días.",
            "high" if amount >= 3000 or waiting > usual_days + 60 else "medium", amount=amount,
            facts={"party": "customer", "name": name, "supplier_key": key, "usual_days_to_pay": round(usual_days), "overdue": len(overdue), "oldest_days": waiting},
            evidence_items=[evidence("invoice", f"{item.invoice_number or 's/n'} · {item.invoice_date:%d/%m/%Y} · {eur(item.total)}", document_id=item.document_id) for item in overdue[:4]],
            confidence=0.75,
        ))
    return findings


def attach_profiles(database: Session, findings: list[dict[str, Any]]) -> None:
    """Cada hallazgo se explica contra el comportamiento habitual del proveedor o cliente."""
    from app.financial_memory import get_profile

    for item in findings:
        key = item["facts"].get("supplier_key")
        party = "customer" if item["facts"].get("party") == "customer" else "supplier"
        profile = get_profile(database, key, party)
        if not profile or profile.get("invoices", 0) < 3:
            continue
        item["facts"]["profile"] = {name: profile.get(name) for name in ("summary", "amount", "frequency", "vat", "payment", "last_decision")}
        item["detail"] += f" Lo habitual: {profile['summary'][0].lower() + profile['summary'][1:]}"
        item["finding"]["por_que"] = item["detail"]
        item["finding"]["datos"]["profile"] = item["facts"]["profile"]


def scan(database: Session, today: date) -> list[dict[str, Any]]:
    """Barrido completo de la empresa con todas las comprobaciones."""
    from app.financial_memory import refresh_profiles

    refresh_profiles(database, today=today)
    findings: list[dict[str, Any]] = []
    invoices = received_invoices(database)
    recent_from = today - timedelta(days=RECENT_DAYS)
    by_supplier: dict[str, list[Invoice]] = defaultdict(list)
    for invoice in invoices:
        key = supplier_key(invoice)
        if key:
            by_supplier[key].append(invoice)

    all_totals = sorted(float(invoice.total) for invoice in invoices if invoice.total)
    p90 = all_totals[int(len(all_totals) * 0.9)] if len(all_totals) >= 10 else None
    last_bank_date = database.scalar(select(BankTransaction.booking_date).order_by(BankTransaction.booking_date.desc()).limit(1))

    for key, items in by_supplier.items():
        items.sort(key=lambda invoice: (invoice.invoice_date, invoice.id))
        for invoice in items:
            if invoice.invoice_date < recent_from:
                continue
            findings += check_atypical(invoice, items)
            findings += check_new_supplier(invoice, items, p90)
            findings += check_vat(invoice, items)
            findings += check_duplicate(invoice, items)
            findings += check_behavior(invoice, items)
        for invoice in items:
            findings += check_unpaid(invoice, today, last_bank_date)
        findings += check_missing(key, items, today)

    # Clientes que compraban cada mes y han dejado de hacerlo.
    issued = database.scalars(
        select(Invoice).where(Invoice.direction == "ISSUED", Invoice.invoice_date.is_not(None), Invoice.total.is_not(None))
    ).all()
    by_customer: dict[str, list[Invoice]] = defaultdict(list)
    for invoice in issued:
        key = invoice.customer_tax_id or (invoice.customer_name or "").strip().upper()
        if key:
            by_customer[key].append(invoice)
    for key, items in by_customer.items():
        items.sort(key=lambda invoice: (invoice.invoice_date, invoice.id))
        findings += [item for item in check_missing(key, items, today, party="customer") if item["procedure"] == "PATRON_INTERRUMPIDO"]

    findings += bank_findings(database, today)
    findings += obligation_findings(database, today)
    findings += vat_trend(database, today)
    findings += trend_findings(database, today)
    findings += customer_payment_findings(database, today)
    attach_profiles(database, findings)
    return apply_feedback(database, findings)


def vat_trend(database: Session, today: date) -> list[dict[str, Any]]:
    from app.tax_service import build_model_303

    quarter = (today.month - 1) // 3 + 1
    try:
        current = build_model_303(database, year=today.year, quarter=quarter).get("result")
        history = []
        year, q = today.year, quarter
        for _ in range(4):
            q -= 1
            if q == 0:
                q, year = 4, year - 1
            value = build_model_303(database, year=year, quarter=q).get("result")
            if value:
                history.append(value)
    except Exception:
        return []
    if current is None or len(history) < 2:
        return []
    average = sum(history) / len(history)
    if abs(average) < 200 or abs(current - average) < 1000 or abs(current - average) / abs(average) < 0.6:
        return []
    return [
        anomaly(
            f"iva303:{today.year}-{quarter}",
            "IVA_TENDENCIA",
            f"El IVA del {quarter}T {today.year} se sale de lo habitual",
            f"El borrador del 303 va en {eur(current)} frente a una media de {eur(average)} en los trimestres anteriores.",
            "medium",
            amount=current,
            facts={"current": current, "average": average, "quarters": len(history)},
            confidence=0.6,
        )
    ]


# ---------------------------------------------------------------------
# Lo que las personas ya decidieron
# ---------------------------------------------------------------------


def human_feedback(database: Session) -> dict[tuple[str, str], dict[str, Any]]:
    """(proveedor, tipo) → la última vez que una persona lo descartó."""
    feedback: dict[tuple[str, str], dict[str, Any]] = {}
    for case in database.scalars(select(Case).where(Case.kind == "ANOMALY", Case.status == "DISMISSED").order_by(Case.id)).all():
        facts = case.facts or {}
        key = facts.get("supplier_key")
        if not key:
            continue
        types = facts.get("finding_types") or [case.procedure]
        for kind in types:
            feedback[(key, kind)] = {
                "case_id": case.id,
                "code": case.code,
                "date": (case.resolved_at or case.updated_at or case.created_at).date().isoformat() if (case.resolved_at or case.updated_at or case.created_at) else None,
                "resolution": case.resolution,
            }
    return feedback


def apply_feedback(database: Session, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    feedback = human_feedback(database)
    if not feedback:
        return findings
    for item in findings:
        key = item["facts"].get("supplier_key")
        previous = feedback.get((key, item["procedure"])) if key else None
        if not previous:
            continue
        lowered = {"high": "medium", "medium": "low", "low": "low"}[item["severity"]]
        note = f" Ya revisaste algo parecido de este proveedor ({previous['code']}) y lo diste por correcto" + (f": «{previous['resolution']}»." if previous.get("resolution") else ".")
        item["severity"] = lowered
        item["detail"] += note
        item["facts"]["previous_review"] = previous
        item["finding"].update(
            riesgo=lowered,
            confianza=round(item["finding"]["confianza"] * 0.6, 2),
            por_que=item["detail"],
        )
        item["finding"]["datos"]["previous_review"] = previous
    return findings


def max_risk(findings: list[dict[str, Any]]) -> str | None:
    if not findings:
        return None
    return max((item["severity"] for item in findings), key=lambda value: RISK_ORDER[value])


class DetectorAnomalias(Agent):
    code = "detector"
    name = "Detector de anomalías"
    role = "Cruza facturas, banco e histórico y avisa de lo que no cuadra."
    icon = "alert"
    handles = ("invoice", "deadline", "scan")
    needs_case = False
    consumes = ("factura o periodo", "histórico del proveedor", "movimientos bancarios")
    produces = ("anomalías (qué, por qué, datos, riesgo, confianza, siguiente paso)", "señal anomaly/severity")

    def run(self, ctx: AgentContext) -> StepResult:
        event = ctx.event
        if event is not None and event.kind == "invoice":
            invoice = ctx.database.get(Invoice, event.ref_id)
            findings = check_invoice(ctx.database, invoice, ctx.today) if invoice else []
            scope = "esta factura"
        else:
            findings = scan(ctx.database, ctx.today)
            if event is not None and event.kind == "deadline":
                findings = [item for item in findings if item["procedure"] not in {"PATRON_INTERRUMPIDO", "CAMBIO_COMPORTAMIENTO"}]
            scope = "facturas, banco e IVA"
        ctx.facts["anomalies"] = findings

        by_type: dict[str, int] = defaultdict(int)
        for item in findings:
            by_type[item["procedure"]] += 1
        risk = max_risk(findings)
        relevant = [item for item in findings if item["severity"] in {"medium", "high"}]
        if not findings:
            summary = f"Todo cuadra en {scope}: sin anomalías."
        elif len(findings) == 1:
            summary = f"{findings[0]['title']}: {findings[0]['detail']}"
        else:
            summary = f"{len(findings)} posible(s) anomalía(s) en {scope}; riesgo máximo {({'high': 'alto', 'medium': 'medio', 'low': 'bajo'})[risk]}."
        return StepResult(
            summary=summary,
            output={"by_type": dict(by_type), "count": len(findings), "max_risk": risk},
            evidence=[evidence("anomaly", item["title"]) for item in findings[:10]],
            engine="estadística (mediana/MAD)",
            findings=[to_finding(item) for item in findings],
            signals={"anomaly": bool(relevant), "severity": risk, "anomaly_count": len(findings)},
        )


def run_anomaly_scan(database: Session, *, trigger: str = "schedule", today: date | None = None) -> dict[str, Any]:
    from app.agents.director import prioritize
    from app.agents.expedientes import next_case_code

    today = today or clock.today()
    ctx = AgentContext(database=database, today=today, now=clock.now(), trigger=trigger)
    run = AgentRun(pipeline="anomalies", trigger=trigger, status="RUNNING")
    database.add(run)
    database.flush()
    run_step(ctx, run, DetectorAnomalias(), 1)
    findings = ctx.facts.get("anomalies", [])

    created = 0
    fingerprints = set()
    for item in findings:
        fingerprints.add(item["fingerprint"])
        case = database.scalar(select(Case).where(Case.fingerprint == item["fingerprint"]))
        if case is not None:
            if item["procedure"] == "OBLIGACION_INCOMPLETA" and case.status == "WAITING_HUMAN":
                # La obligación cambia mientras se completa: el aviso refleja lo que falta HOY.
                case.title, case.summary = item["title"][:255], item["detail"]
                case.facts = jsonable({**(case.facts or {}), **item["facts"], "findings": [item["finding"]]})
            continue  # ya avisado (abierto o descartado por una persona)
        invoice_id = item["facts"].get("invoice_id")
        if invoice_id and database.scalar(select(Case.id).where(Case.fingerprint == f"factura:{invoice_id}")):
            continue  # esa factura ya tiene su expediente (caso «factura sospechosa»)
        case = Case(
            code=next_case_code(database, today.year),
            kind="ANOMALY",
            procedure=item["procedure"],
            title=item["title"][:255],
            status="WAITING_HUMAN",
            summary=item["detail"],
            amount=Decimal(str(item["amount"])) if item["amount"] is not None else None,
            fingerprint=item["fingerprint"],
            document_id=item["facts"].get("document_id"),
            facts=jsonable({
                **item["facts"], "origin": "scan", "severity": item["severity"], "severity_score": SEVERITY[item["severity"]],
                "evidence": item["evidence"], "findings": [item["finding"]], "finding_types": [item["procedure"]],
            }),
            required_documents=[],
            proposed_actions=[{"label": item["finding"]["siguiente"] or "Revisarlo y confirmar si es correcto o un error", "done": False}],
            antecedents=[],
            subject_type="company",
        )
        invoice = database.get(Invoice, invoice_id) if invoice_id else None
        if invoice is not None:
            case.subject_type, case.subject_name, case.subject_tax_id = "supplier", invoice.supplier_name, invoice.supplier_tax_id
        elif item["facts"].get("supplier") or item["facts"].get("name"):
            case.subject_type = "customer" if item["facts"].get("party") == "customer" else "supplier"
            case.subject_name = item["facts"].get("supplier") or item["facts"].get("name")
        database.add(case)
        database.flush()
        prioritize(database, case, today)
        database.add(CaseEvent(case_id=case.id, kind="agent", actor="detector", title=f"Detector de anomalías · {item['title']}"[:255], detail=item["detail"], data={"run_id": run.id}))
        created += 1

    # Las que ya no se dan se cierran solas (solo las que abrió el barrido).
    closed = 0
    for case in database.scalars(select(Case).where(Case.kind == "ANOMALY", Case.status == "WAITING_HUMAN")).all():
        if (case.facts or {}).get("origin", "scan") != "scan":
            continue
        if case.fingerprint and case.fingerprint not in fingerprints:
            case.status = "RESOLVED"
            case.resolution = "Resuelta automáticamente: la condición ya no se da (por ejemplo, llegó la factura o se concilió el movimiento)."
            case.resolved_at = clock.now()
            database.add(CaseEvent(case_id=case.id, kind="agent", actor="detector", title="Detector de anomalías · Ya no se da la condición: cerrada automáticamente", data={}))
            closed += 1

    run.status = "OK"
    run.finished_at = clock.now()
    run.summary = f"{created} anomalía(s) nuevas, {closed} cerrada(s)"
    database.flush()
    return {"found": len(findings), "created": created, "closed": closed, "run_id": run.id}
