"""
Conciliación banco ↔ facturas.

Cada movimiento del banco queda en uno de estos estados:

    CONCILIADO        enlazado con su factura (por una persona o automáticamente)
    POSIBLE           hay una factura que encaja; falta confirmarlo
    IMPORTE_DISTINTO  se reconoce al proveedor/cliente (o la factura), pero el importe no cuadra
    DUPLICADO         mismo pago que otro movimiento: misma contraparte e importe, días de diferencia
    SIN_FACTURA       pago o cobro sin ninguna factura que lo justifique
    IGNORADO          descartado por una persona (comisiones, traspasos…)

Y cada factura aprobada y vencida sin movimiento queda como FACTURA_SIN_PAGO
(solo si el extracto llega hasta después del vencimiento).

Se concilia sin persona solo con evidencia inequívoca: importe exacto y el
número de factura en el concepto, o importe exacto con el nombre del
proveedor y una única factura posible. Todo lo demás se propone o se
investiga. Las excepciones las convierte el Detector en hallazgos.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.reports_service import counterparty
from app.reports_service import is_issued
from app.bank_service import significant_words
from app.extractor import normalize_search_text
from app.models import BankTransaction
from app.models import Invoice

STATES = ("CONCILIADO", "POSIBLE", "IMPORTE_DISTINTO", "DUPLICADO", "SIN_FACTURA", "IGNORADO")
STATE_LABELS = {
    "CONCILIADO": "✅ Conciliado",
    "POSIBLE": "⚠️ Posible coincidencia",
    "IMPORTE_DISTINTO": "🔴 Importe diferente",
    "DUPLICADO": "🔴 Pago duplicado",
    "SIN_FACTURA": "🔴 Sin factura",
    "IGNORADO": "Ignorado",
    "FACTURA_SIN_PAGO": "🔴 Factura sin pago",
}
AUTO_SCORE = 95  # por encima, se concilia sin persona
DUPLICATE_DAYS = 10
UNPAID_GRACE_DAYS = 15


def eur(value: Any) -> str:
    text = f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def mentions(transaction: BankTransaction, invoice: Invoice) -> dict[str, bool]:
    """Qué evidencia hay en el concepto del banco de que el movimiento es de esa factura."""
    description = normalize_search_text(transaction.description)
    compact = description.replace(" ", "")
    name, tax_id = counterparty(invoice)
    words = significant_words(name)
    return {
        "name": bool(words) and (words[0] in description or sum(word in description for word in words[:3]) >= 2),
        "tax_id": bool(tax_id) and tax_id.lower() in compact,
        "number": bool(invoice.invoice_number) and len(invoice.invoice_number) >= 3 and normalize_search_text(invoice.invoice_number).replace(" ", "") in compact,
    }


def candidates(database: Session, transaction: BankTransaction, invoices: list[Invoice]) -> list[dict[str, Any]]:
    """Facturas del mismo sentido que el movimiento, con su evidencia y su puntuación."""
    amount = abs(Decimal(str(transaction.amount)))
    inflow = transaction.amount > 0
    result = []
    for invoice in invoices:
        if invoice.total is None or is_issued(invoice) != inflow:
            continue
        if invoice.invoice_date and not (invoice.invoice_date - timedelta(days=5) <= transaction.booking_date <= invoice.invoice_date + timedelta(days=180)):
            continue
        evidence = mentions(transaction, invoice)
        exact = abs(abs(Decimal(str(invoice.total))) - amount) < Decimal("0.01")
        if not exact and not (evidence["name"] or evidence["tax_id"] or evidence["number"]):
            continue
        score = (60 if exact else 0) + (25 if evidence["name"] else 0) + (10 if evidence["tax_id"] else 0) + (30 if evidence["number"] else 0)
        result.append({"invoice": invoice, "exact": exact, "evidence": evidence, "score": min(score, 100),
                       "difference": float(amount - abs(Decimal(str(invoice.total))))})
    result.sort(key=lambda item: (-item["exact"], -item["score"]))
    return result


def reconcile(database: Session, *, today: date | None = None, auto: bool = True, actor: str = "conciliacion-automatica") -> dict[str, Any]:
    """Clasifica todos los movimientos, concilia lo inequívoco y devuelve el estado."""
    from app.bank_service import confirm_match

    today = today or date.today()
    invoices = database.scalars(select(Invoice).where(Invoice.review_status != "REJECTED", Invoice.total.is_not(None))).all()
    transactions = database.scalars(select(BankTransaction).order_by(BankTransaction.booking_date, BankTransaction.id)).all()
    matched_invoice_ids = {item.matched_invoice_id for item in transactions if item.match_status == "MATCHED" and item.matched_invoice_id}

    rows: list[dict[str, Any]] = []
    seen_payments: dict[tuple[str, Decimal], list[BankTransaction]] = {}
    auto_matched = 0
    for transaction in transactions:
        row: dict[str, Any] = {"transaction_id": transaction.id, "date": transaction.booking_date.isoformat(), "description": transaction.description,
                               "amount": float(transaction.amount), "invoice_id": None, "invoice_label": None, "difference": None, "evidence": []}
        key = (normalize_search_text(transaction.description)[:40], abs(Decimal(str(transaction.amount))))
        earlier = [item for item in seen_payments.get(key, []) if 0 <= (transaction.booking_date - item.booking_date).days <= DUPLICATE_DAYS]
        seen_payments.setdefault(key, []).append(transaction)

        if transaction.match_status == "IGNORED":
            row["state"] = "IGNORADO"
        elif transaction.match_status == "MATCHED":
            row["state"] = "CONCILIADO"
            row["invoice_id"] = transaction.matched_invoice_id
        elif earlier and abs(transaction.amount) >= 1:
            row["state"] = "DUPLICADO"
            row["duplicate_of"] = earlier[0].id
            row["evidence"] = [f"Mismo concepto e importe que el movimiento del {earlier[0].booking_date:%d/%m/%Y}"]
        else:
            options = [item for item in candidates(database, transaction, invoices) if item["invoice"].id not in matched_invoice_ids]
            exact = [item for item in options if item["exact"]]
            if exact:
                best = exact[0]
                unique = len(exact) == 1 or exact[1]["score"] < best["score"] - 10
                invoice = best["invoice"]
                row.update(invoice_id=invoice.id, invoice_label=f"{invoice.invoice_number or 's/n'} · {counterparty(invoice)[0] or ''}".strip(" ·"),
                           evidence=[name for name, present in best["evidence"].items() if present])
                certain = best["evidence"]["number"] or (best["evidence"]["name"] and unique)
                if auto and certain and best["score"] >= AUTO_SCORE and invoice.review_status == "APPROVED":
                    confirm_match(database, transaction=transaction, invoice_id=invoice.id, actor=actor)
                    matched_invoice_ids.add(invoice.id)
                    auto_matched += 1
                    row["state"] = "CONCILIADO"
                    row["automatic"] = True
                else:
                    row["state"] = "POSIBLE"
                    if transaction.match_status == "UNMATCHED" and invoice.review_status == "APPROVED":
                        transaction.matched_invoice_id, transaction.match_status, transaction.match_score = invoice.id, "SUGGESTED", best["score"]
            elif options:
                best = options[0]
                invoice = best["invoice"]
                row.update(state="IMPORTE_DISTINTO", invoice_id=invoice.id, difference=round(best["difference"], 2),
                           invoice_label=f"{invoice.invoice_number or 's/n'} · {counterparty(invoice)[0] or ''}".strip(" ·"),
                           evidence=[name for name, present in best["evidence"].items() if present])
            else:
                row["state"] = "SIN_FACTURA"
        rows.append(row)

    # Facturas aprobadas, vencidas y sin movimiento (solo si el banco llega hasta después del vencimiento).
    last_bank_date = max((item.booking_date for item in transactions), default=None)
    pointed = {row["invoice_id"] for row in rows if row["invoice_id"]}
    unpaid = []
    for invoice in invoices:
        if invoice.review_status != "APPROVED" or invoice.paid_at or invoice.id in pointed or not invoice.invoice_date:
            continue
        due = invoice.due_date or invoice.invoice_date + timedelta(days=60)
        if last_bank_date is None or last_bank_date < due + timedelta(days=UNPAID_GRACE_DAYS) or today < due + timedelta(days=UNPAID_GRACE_DAYS):
            continue
        unpaid.append({"invoice_id": invoice.id, "invoice_label": f"{invoice.invoice_number or 's/n'} · {counterparty(invoice)[0] or ''}".strip(" ·"),
                       "amount": float(invoice.total), "due": due.isoformat(), "direction": "cobro" if is_issued(invoice) else "pago", "state": "FACTURA_SIN_PAGO"})
    database.flush()
    counts = Counter(row["state"] for row in rows)
    counts["FACTURA_SIN_PAGO"] = len(unpaid)
    return {
        "movements": rows, "unpaid_invoices": unpaid, "counts": dict(counts), "auto_matched": auto_matched,
        "labels": STATE_LABELS, "total": len(rows),
        "reconciled_rate": round(counts.get("CONCILIADO", 0) / max(1, len(rows) - counts.get("IGNORADO", 0)), 3) if rows else None,
    }


def amount_mismatches(database: Session, date_from: date, date_to: date) -> list[dict[str, Any]]:
    """Movimientos del periodo que se reconocen como de una factura pero no cuadran en importe."""
    report = reconcile(database, auto=False)
    result = []
    for row in report["movements"]:
        if row["state"] != "IMPORTE_DISTINTO" or not (date_from.isoformat() <= row["date"] <= date_to.isoformat()):
            continue
        result.append({**row, "label": f"{row['invoice_label']}: el banco dice {eur(abs(row['amount']))} (diferencia {eur(abs(row['difference']))})",
                       "difference": abs(row["difference"])})
    return result
