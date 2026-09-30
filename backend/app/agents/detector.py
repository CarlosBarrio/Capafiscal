"""
Detector de anomalías: cruza facturas, banco e histórico y avisa cuando algo
no cuadra con lo habitual. Estadística robusta (mediana y MAD), sin cajas
negras: cada aviso trae los números que lo justifican.

Comprobaciones:
  1. Importe atípico para ese proveedor
  2. Proveedor nuevo con un importe alto
  3. Tipo de IVA distinto del habitual del proveedor
  4. Posible factura duplicada (mismo proveedor e importe, número distinto)
  5. Factura recurrente que no ha llegado este mes
  6. Movimiento bancario relevante sin factura
  7. Cambio brusco del IVA a ingresar frente a trimestres anteriores
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.base import eur
from app.agents.base import evidence
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
MONTHS = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre")

SEVERITY = {"high": 60, "medium": 45, "low": 30}


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


def invoice_rate(invoice: Invoice) -> float | None:
    if invoice.tax_lines:
        rates = {float(line.tax_rate) for line in invoice.tax_lines if line.tax_rate is not None}
        if len(rates) == 1:
            return rates.pop()
    if invoice.subtotal and invoice.tax_total is not None and float(invoice.subtotal):
        return round(float(invoice.tax_total) / float(invoice.subtotal) * 100)
    return None


def anomaly(fingerprint: str, procedure: str, title: str, detail: str, severity: str, *, amount: Any = None, facts: dict[str, Any] | None = None, evidence_items: list | None = None) -> dict[str, Any]:
    return {
        "fingerprint": fingerprint,
        "procedure": procedure,
        "title": title,
        "detail": detail,
        "severity": severity,
        "amount": float(amount) if amount is not None else None,
        "facts": facts or {},
        "evidence": evidence_items or [],
    }


def scan(database: Session, today: date) -> list[dict[str, Any]]:
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

    for key, items in by_supplier.items():
        items.sort(key=lambda invoice: invoice.invoice_date)
        name = items[-1].supplier_name or key
        recent = [invoice for invoice in items if invoice.invoice_date >= recent_from]

        # 1) Importe atípico (MAD sobre log del importe)
        for invoice in recent:
            history = [float(item.total) for item in items if item.id != invoice.id and item.invoice_date < invoice.invoice_date and float(item.total) > 0]
            if len(history) < 4 or float(invoice.total) <= 0:
                continue
            logs = [math.log(value) for value in history]
            median = statistics.median(logs)
            mad = statistics.median([abs(value - median) for value in logs]) or 0.05
            z = 0.6745 * (math.log(float(invoice.total)) - median) / mad
            usual = math.exp(median)
            if abs(z) >= MAD_THRESHOLD and abs(float(invoice.total) - usual) >= 50:
                ratio = float(invoice.total) / usual
                findings.append(
                    anomaly(
                        f"atipico:{invoice.id}",
                        "IMPORTE_ATIPICO",
                        f"Factura de {name} fuera de lo habitual",
                        f"La factura {invoice.invoice_number or ''} de {invoice.invoice_date:%d/%m/%Y} es de {eur(invoice.total)}, "
                        f"{'un ' + format(ratio, '.1f').replace('.', ',') + ' veces' if ratio > 1 else 'muy por debajo de'} lo habitual "
                        f"({eur(usual)} de mediana en {len(history)} facturas).",
                        "high" if ratio >= 3 or ratio <= 0.2 else "medium",
                        amount=invoice.total,
                        facts={"invoice_id": invoice.id, "document_id": invoice.document_id, "median": usual, "history": len(history), "z": round(z, 2)},
                        evidence_items=[evidence("invoice", f"{name} · {invoice.invoice_number}", document_id=invoice.document_id)],
                    )
                )

        # 2) Proveedor nuevo con importe alto
        first = items[0]
        if len(items) == 1 and first.invoice_date >= recent_from and p90 and float(first.total) >= max(p90, 1000):
            findings.append(
                anomaly(
                    f"nuevo:{first.id}",
                    "PROVEEDOR_NUEVO",
                    f"Proveedor nuevo con un importe alto: {name}",
                    f"Primera factura de {name} y ya es de {eur(first.total)} (más que el 90 % de tus facturas). Comprueba que el proveedor y la cuenta de pago son correctos.",
                    "medium",
                    amount=first.total,
                    facts={"invoice_id": first.id, "document_id": first.document_id, "p90": p90},
                    evidence_items=[evidence("invoice", f"{name} · {first.invoice_number}", document_id=first.document_id)],
                )
            )

        # 3) IVA distinto del habitual
        rates = [invoice_rate(item) for item in items]
        known = [rate for rate in rates if rate is not None]
        if len(known) >= 4:
            usual_rate = max(set(known), key=known.count)
            share = known.count(usual_rate) / len(known)
            for invoice, rate in zip(items, rates):
                if invoice.invoice_date < recent_from or rate is None or rate == usual_rate or share < 0.75:
                    continue
                findings.append(
                    anomaly(
                        f"iva:{invoice.id}",
                        "IVA_INUSUAL",
                        f"IVA inusual en una factura de {name}",
                        f"La factura {invoice.invoice_number or ''} aplica un {rate:g} % de IVA, pero {name} factura al {usual_rate:g} % en el {share:.0%} de los casos. Puede ser un error en la factura o en la lectura.",
                        "medium",
                        amount=invoice.tax_total,
                        facts={"invoice_id": invoice.id, "document_id": invoice.document_id, "rate": rate, "usual_rate": usual_rate},
                        evidence_items=[evidence("invoice", f"{name} · {invoice.invoice_number}", document_id=invoice.document_id)],
                    )
                )

        # 4) Posible duplicado: mismo importe en 45 días con número distinto
        for index, invoice in enumerate(items):
            if invoice.invoice_date < recent_from:
                continue
            for other in items[:index]:
                if (
                    other.total == invoice.total
                    and other.invoice_number != invoice.invoice_number
                    and abs((invoice.invoice_date - other.invoice_date).days) <= 45
                    and invoice.duplicate_status == "NONE"
                ):
                    findings.append(
                        anomaly(
                            f"duplicado:{other.id}:{invoice.id}",
                            "POSIBLE_DUPLICADO",
                            f"Posible factura duplicada de {name}",
                            f"{invoice.invoice_number or 's/n'} ({invoice.invoice_date:%d/%m}) y {other.invoice_number or 's/n'} ({other.invoice_date:%d/%m}) tienen el mismo importe, {eur(invoice.total)}. Comprueba que no se trata del mismo servicio facturado dos veces.",
                            "medium",
                            amount=invoice.total,
                            facts={"invoice_ids": [other.id, invoice.id], "document_id": invoice.document_id},
                            evidence_items=[evidence("invoice", f"{name} · {item.invoice_number}", document_id=item.document_id) for item in (other, invoice)],
                        )
                    )
                    break

        # 5) Factura recurrente que no ha llegado
        last_month_end = today.replace(day=1) - timedelta(days=1)
        last_month = (last_month_end.year, last_month_end.month)
        months_seen = {(item.invoice_date.year, item.invoice_date.month) for item in items}
        previous_months = [
            (add_months(last_month_end.replace(day=1), -offset).year, add_months(last_month_end.replace(day=1), -offset).month)
            for offset in range(1, 4)
        ]
        if today.day >= 10 and all(month in months_seen for month in previous_months) and last_month not in months_seen:
            usual = statistics.median(float(item.total) for item in items[-6:])
            findings.append(
                anomaly(
                    f"falta:{key}:{last_month[0]}-{last_month[1]:02d}",
                    "FACTURA_FALTA",
                    f"No ha llegado la factura de {MONTHS[last_month[1] - 1]} de {name}",
                    f"{name} te factura todos los meses (aprox. {eur(usual)}) y la de {MONTHS[last_month[1] - 1]} no está. Pídela o búscala en el correo: sin ella no puedes deducirte el IVA.",
                    "low",
                    amount=usual,
                    facts={"supplier": name, "month": f"{last_month[0]}-{last_month[1]:02d}"},
                )
            )

    # 6) Movimientos bancarios sin factura
    limit_date = today - timedelta(days=BANK_MIN_AGE_DAYS)
    transactions = database.scalars(
        select(BankTransaction).where(
            BankTransaction.match_status == "UNMATCHED",
            BankTransaction.booking_date <= limit_date,
            BankTransaction.booking_date >= today - timedelta(days=120),
        )
    ).all()
    for transaction in transactions:
        amount = float(transaction.amount)
        if abs(amount) < BANK_MIN_AMOUNT:
            continue
        outflow = amount < 0
        findings.append(
            anomaly(
                f"banco:{transaction.id}",
                "MOVIMIENTO_SIN_FACTURA",
                f"{'Pago' if outflow else 'Cobro'} de {eur(abs(amount))} sin factura",
                f"El {transaction.booking_date:%d/%m/%Y}: «{transaction.description[:90]}». "
                + ("Si es un gasto de la actividad, falta la factura para deducirlo." if outflow else "Si es una venta, falta emitir o registrar la factura."),
                "medium" if abs(amount) >= 1000 else "low",
                amount=abs(amount),
                facts={"transaction_id": transaction.id, "direction": "out" if outflow else "in"},
            )
        )

    # 7) IVA trimestral con cambio brusco
    findings += vat_trend(database, today)
    return findings


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
            f"El borrador del 303 va en {eur(current)} frente a una media de {eur(average)} en los trimestres anteriores. Revisa si faltan facturas recibidas o si hay ventas atípicas.",
            "medium",
            amount=current,
            facts={"current": current, "average": average},
        )
    ]


class DetectorAnomalias(Agent):
    code = "detector"
    name = "Detector de anomalías"
    role = "Cruza facturas, banco e histórico y avisa de lo que no cuadra."
    icon = "alert"

    def run(self, ctx: AgentContext) -> StepResult:
        findings = scan(ctx.database, ctx.today)
        ctx.facts["findings"] = findings
        by_type: dict[str, int] = defaultdict(int)
        for item in findings:
            by_type[item["procedure"]] += 1
        return StepResult(
            summary=f"{len(findings)} posible(s) anomalía(s) en facturas, banco e IVA." if findings else "Todo cuadra: sin anomalías.",
            output={"by_type": dict(by_type), "count": len(findings)},
            evidence=[evidence("anomaly", item["title"]) for item in findings[:10]],
            engine="estadística (mediana/MAD)",
        )


def run_anomaly_scan(database: Session, *, trigger: str = "schedule", today: date | None = None) -> dict[str, Any]:
    from app.agents.director import prioritize
    from app.agents.expedientes import next_case_code

    today = today or date.today()
    ctx = AgentContext(database=database, today=today, now=datetime.now(timezone.utc), trigger=trigger)
    run = AgentRun(pipeline="anomalies", trigger=trigger, status="RUNNING")
    database.add(run)
    database.flush()
    run_step(ctx, run, DetectorAnomalias(), 1)
    findings = ctx.facts.get("findings", [])

    created = 0
    fingerprints = set()
    for item in findings:
        fingerprints.add(item["fingerprint"])
        case = database.scalar(select(Case).where(Case.fingerprint == item["fingerprint"]))
        if case is not None:
            continue  # ya avisado (abierto o descartado por una persona)
        case = Case(
            code=next_case_code(database, today.year),
            kind="ANOMALY",
            procedure=item["procedure"],
            title=item["title"][:255],
            status="WAITING_HUMAN",
            summary=item["detail"],
            amount=Decimal(str(item["amount"])) if item["amount"] is not None else None,
            fingerprint=item["fingerprint"],
            facts={**item["facts"], "severity": item["severity"], "severity_score": SEVERITY[item["severity"]], "evidence": item["evidence"]},
            required_documents=[],
            proposed_actions=[{"label": "Revisarlo y confirmar si es correcto o un error", "done": False}],
            antecedents=[],
            subject_type="company",
        )
        database.add(case)
        database.flush()
        prioritize(database, case, today)
        database.add(CaseEvent(case_id=case.id, kind="agent", actor="detector", title=f"Detector de anomalías · {item['title']}", detail=item["detail"], data={"run_id": run.id}))
        created += 1

    # Las que ya no se dan se cierran solas.
    closed = 0
    for case in database.scalars(select(Case).where(Case.kind == "ANOMALY", Case.status == "WAITING_HUMAN")).all():
        if case.fingerprint and case.fingerprint not in fingerprints:
            case.status = "RESOLVED"
            case.resolution = "Resuelta automáticamente: la condición ya no se da (por ejemplo, llegó la factura o se concilió el movimiento)."
            case.resolved_at = datetime.now(timezone.utc)
            database.add(CaseEvent(case_id=case.id, kind="agent", actor="detector", title="Detector de anomalías · Ya no se da la condición: cerrada automáticamente", data={}))
            closed += 1

    run.status = "OK"
    run.finished_at = datetime.now(timezone.utc)
    run.summary = f"{created} anomalía(s) nuevas, {closed} cerrada(s)"
    database.flush()
    return {"found": len(findings), "created": created, "closed": closed, "run_id": run.id}
