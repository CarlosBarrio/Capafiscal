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

Además del estado, cada movimiento lleva un NIVEL DE CONFIANZA y su evidencia:

    SEGURO      importe exacto + una prueba de identidad (nº de factura, NIF, IBAN,
                o nombre con fecha coherente) + una sola factura posible.
                Es lo único que se concilia sin persona.
    PROBABLE    hay una factura que encaja, pero la evidencia no basta
                (solo el importe, o el importe dentro de la tolerancia).
    CONFLICTO   la evidencia se contradice: dos o más facturas plausibles,
                el importe no cuadra con la factura identificada o el pago
                parece duplicado. Nunca se concilia solo.
    SIN MATCH   ninguna factura lo justifica.

Regla de producto: CapaFiscal nunca se inventa seguridad. Cada decisión
lista las comprobaciones hechas (✓/✗) y los candidatos considerados.
Las excepciones las convierte el Detector en hallazgos.
"""
from __future__ import annotations

import re
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
    "CONCILIADO": "Conciliado",
    "POSIBLE": "Posible coincidencia",
    "IMPORTE_DISTINTO": "Importe diferente",
    "DUPLICADO": "Pago duplicado",
    "SIN_FACTURA": "Sin factura",
    "IGNORADO": "Ignorado",
    "FACTURA_SIN_PAGO": "Factura sin pago",
}
LEVELS = ("SEGURO", "PROBABLE", "CONFLICTO", "SIN_MATCH")
LEVEL_LABELS = {"SEGURO": "Seguro", "PROBABLE": "Probable", "CONFLICTO": "Conflicto", "SIN_MATCH": "Sin match"}
PROBABLE_CAP = 84     # un PROBABLE nunca se muestra con más confianza que esto
AMBIGUITY_GAP = 15    # dos candidatos a menos de esta distancia son igual de plausibles
DUPLICATE_DAYS = 10
UNPAID_GRACE_DAYS = 15
DATE_WINDOW = 10      # días alrededor del vencimiento que se consideran fecha coherente
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){4,7}\b")
# Calibrado para que cada combinación SEGURA (importe exacto + prueba de identidad) dé al menos 85 por sí sola.
WEIGHTS = {"amount": 50, "number": 35, "tax_id": 35, "iban": 35, "name": 20, "date": 15}


def eur(value: Any) -> str:
    text = f"{Decimal(str(value)):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def tolerance(amount: Decimal) -> Decimal:
    """Diferencia admisible por comisiones o redondeos: 1 € o el 0,5 %, lo que sea mayor."""
    return max(Decimal("1.00"), (amount * Decimal("0.005")).quantize(Decimal("0.01")))


def ibans(text: str | None) -> set[str]:
    return {match.replace(" ", "") for match in IBAN_RE.findall((text or "").upper())}


class IbanIndex:
    """IBAN que aparecen en el texto de cada factura (se lee bajo demanda, solo si el banco trae un IBAN)."""

    def __init__(self, database: Session):
        self.database = database
        self.cache: dict[int, set[str]] = {}

    def of(self, invoice: Invoice) -> set[str]:
        if invoice.id not in self.cache:
            from app.models import ExtractionRun

            text = self.database.scalar(
                select(ExtractionRun.raw_text).where(ExtractionRun.document_id == invoice.document_id).order_by(ExtractionRun.id.desc()).limit(1)
            ) if invoice.document_id else None
            self.cache[invoice.id] = ibans(text)
        return self.cache[invoice.id]


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


def date_coherent(transaction: BankTransaction, invoice: Invoice) -> bool:
    if invoice.due_date:
        return abs((transaction.booking_date - invoice.due_date).days) <= DATE_WINDOW
    if invoice.invoice_date:
        return 0 <= (transaction.booking_date - invoice.invoice_date).days <= 45
    return False


def checks_for(item: dict[str, Any], amount: Decimal) -> list[dict[str, Any]]:
    """Las comprobaciones de un candidato, en lenguaje de persona."""
    evidence = item["evidence"]
    difference = abs(Decimal(str(item["difference"])))
    if item["exact"]:
        amount_check = {"key": "amount", "label": "importe exacto", "ok": True}
    elif item["near"]:
        amount_check = {"key": "amount", "label": f"importe dentro de la tolerancia (diferencia {eur(difference)})", "ok": None}
    else:
        amount_check = {"key": "amount", "label": f"importe distinto (diferencia {eur(difference)})", "ok": False}
    return [
        amount_check,
        {"key": "number", "label": "nº de factura en el concepto", "ok": evidence["number"]},
        {"key": "tax_id", "label": "NIF en el concepto", "ok": evidence["tax_id"]},
        {"key": "iban", "label": "IBAN de la factura", "ok": evidence["iban"]},
        {"key": "name", "label": "nombre del proveedor o cliente", "ok": evidence["name"]},
        {"key": "date", "label": "fecha coherente con el vencimiento", "ok": evidence["date"]},
    ]


def candidates(database: Session, transaction: BankTransaction, invoices: list[Invoice], iban_index: IbanIndex | None = None) -> list[dict[str, Any]]:
    """Facturas del mismo sentido que el movimiento, con su evidencia y su puntuación."""
    amount = abs(Decimal(str(transaction.amount)))
    inflow = transaction.amount > 0
    bank_ibans = ibans(transaction.description)
    margin = tolerance(amount)
    result = []
    for invoice in invoices:
        if invoice.total is None or is_issued(invoice) != inflow:
            continue
        if invoice.invoice_date and not (invoice.invoice_date - timedelta(days=5) <= transaction.booking_date <= invoice.invoice_date + timedelta(days=180)):
            continue
        evidence = mentions(transaction, invoice)
        evidence["iban"] = bool(bank_ibans and iban_index and bank_ibans & iban_index.of(invoice))
        evidence["date"] = date_coherent(transaction, invoice)
        gap = abs(abs(Decimal(str(invoice.total))) - amount)
        exact = gap < Decimal("0.01")
        near = not exact and gap <= margin
        identity = evidence["name"] or evidence["tax_id"] or evidence["number"] or evidence["iban"]
        if not exact and not identity:
            continue
        score = (WEIGHTS["amount"] if exact else WEIGHTS["amount"] // 2 if near else 0) + sum(WEIGHTS[key] for key in ("number", "tax_id", "iban", "name") if evidence[key])
        if evidence["date"] and (exact or near or identity):
            score += WEIGHTS["date"]
        item = {"invoice": invoice, "exact": exact, "near": near, "identity": identity, "evidence": evidence, "score": min(score, 100),
                "difference": float(amount - abs(Decimal(str(invoice.total))))}
        item["checks"] = checks_for(item, amount)
        result.append(item)
    result.sort(key=lambda item: (-item["exact"], -item["near"], -item["score"]))
    return result


def label(invoice: Invoice) -> str:
    return f"{invoice.invoice_number or 's/n'} · {counterparty(invoice)[0] or ''}".strip(" ·")


def candidate_view(item: dict[str, Any]) -> dict[str, Any]:
    invoice = item["invoice"]
    return {"invoice_id": invoice.id, "label": label(invoice), "amount": float(invoice.total), "score": item["score"],
            "approved": invoice.review_status == "APPROVED", "checks": item["checks"]}


def classify(options: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide el nivel con la evidencia de los candidatos. Nunca sube la confianza por encima de lo que la evidencia sostiene."""
    matching = [item for item in options if item["exact"] or item["near"]]
    if not matching:
        if options:  # se reconoce la factura, pero el importe no cuadra
            best = options[0]
            return {"level": "CONFLICTO", "state": "IMPORTE_DISTINTO", "best": best, "confidence": best["score"],
                    "decision": f"No se concilia: el importe no cuadra con {label(best['invoice'])} (diferencia {eur(abs(best['difference']))})."}
        return {"level": "SIN_MATCH", "state": "SIN_FACTURA", "best": None, "confidence": 0,
                "decision": "Ninguna factura lo justifica: falta la factura o es un gasto sin factura."}
    best = matching[0]
    rivals = [item for item in matching[1:] if item["exact"] == best["exact"] and item["score"] >= best["score"] - AMBIGUITY_GAP]
    if rivals:
        names = ", ".join(label(item["invoice"]) for item in [best, *rivals][:3])
        return {"level": "CONFLICTO", "state": "POSIBLE", "best": best, "confidence": min(best["score"], 60),
                "decision": f"No se concilia automáticamente: {len(rivals) + 1} facturas igual de plausibles ({names})."}
    evidence = best["evidence"]
    strong = evidence["number"] or evidence["tax_id"] or evidence["iban"] or (evidence["name"] and evidence["date"])
    if best["exact"] and strong:
        # La seguridad la dan las reglas de evidencia, no la suma de puntos.
        return {"level": "SEGURO", "state": "POSIBLE", "best": best, "confidence": best["score"], "decision": "Evidencia suficiente para conciliar."}
    missing = "solo coincide el importe" if not best["identity"] else "el importe no es exacto" if not best["exact"] else "falta una prueba de identidad"
    return {"level": "PROBABLE", "state": "POSIBLE", "best": best, "confidence": min(best["score"], PROBABLE_CAP),
            "decision": f"Se propone, no se concilia: {missing}."}


def reconcile(database: Session, *, today: date | None = None, auto: bool = True, actor: str = "conciliacion-automatica",
              persist: bool = True) -> dict[str, Any]:
    """Clasifica todos los movimientos, concilia solo lo SEGURO y devuelve el estado con su evidencia.

    persist=False: solo lee (pantallas y cierre); no deja propuestas ni concilia."""
    auto = auto and persist
    from app.bank_service import confirm_match

    today = today or date.today()
    invoices = database.scalars(select(Invoice).where(Invoice.review_status != "REJECTED", Invoice.total.is_not(None))).all()
    transactions = database.scalars(select(BankTransaction).order_by(BankTransaction.booking_date, BankTransaction.id)).all()
    matched_invoice_ids = {item.matched_invoice_id for item in transactions if item.match_status == "MATCHED" and item.matched_invoice_id}
    iban_index = IbanIndex(database)

    rows: list[dict[str, Any]] = []
    seen_payments: dict[tuple[str, Decimal], list[BankTransaction]] = {}
    auto_matched = 0
    for transaction in transactions:
        row: dict[str, Any] = {"transaction_id": transaction.id, "date": transaction.booking_date.isoformat(), "description": transaction.description,
                               "amount": float(transaction.amount), "invoice_id": None, "invoice_label": None, "difference": None, "evidence": [],
                               "checks": [], "candidates": []}
        key = (normalize_search_text(transaction.description)[:40], abs(Decimal(str(transaction.amount))))
        earlier = [item for item in seen_payments.get(key, []) if 0 <= (transaction.booking_date - item.booking_date).days <= DUPLICATE_DAYS]
        seen_payments.setdefault(key, []).append(transaction)

        if transaction.match_status == "IGNORED":
            row.update(state="IGNORADO", level=None, confidence=None, decision="Descartado por una persona.")
        elif transaction.match_status == "MATCHED":
            invoice = database.get(Invoice, transaction.matched_invoice_id) if transaction.matched_invoice_id else None
            row.update(state="CONCILIADO", level="SEGURO", invoice_id=transaction.matched_invoice_id,
                       invoice_label=label(invoice) if invoice else None, confidence=transaction.match_score,
                       decision="Conciliado." if transaction.match_score is None else f"Conciliado (coincidencia {transaction.match_score} %).")
            if invoice is not None:  # la evidencia que lo sostiene sigue a la vista
                kept = candidates(database, transaction, [invoice], iban_index)
                if kept:
                    row.update(checks=kept[0]["checks"], evidence=[name for name, present in kept[0]["evidence"].items() if present])
        elif earlier and abs(transaction.amount) >= 1:
            row.update(state="DUPLICADO", level="CONFLICTO", duplicate_of=earlier[0].id, confidence=None,
                       evidence=[f"Mismo concepto e importe que el movimiento del {earlier[0].booking_date:%d/%m/%Y}"],
                       decision="No se concilia: parece el mismo pago dos veces.")
        else:
            options = [item for item in candidates(database, transaction, invoices, iban_index) if item["invoice"].id not in matched_invoice_ids]
            verdict = classify(options)
            best = verdict["best"]
            row.update(state=verdict["state"], level=verdict["level"], confidence=verdict["confidence"], decision=verdict["decision"],
                       candidates=[candidate_view(item) for item in options[:3]])
            if best is not None:
                invoice = best["invoice"]
                row.update(invoice_id=invoice.id, invoice_label=label(invoice), checks=best["checks"],
                           evidence=[name for name, present in best["evidence"].items() if present])
                if verdict["state"] == "IMPORTE_DISTINTO":
                    row["difference"] = round(best["difference"], 2)
            if verdict["level"] == "SEGURO" and auto and best["invoice"].review_status == "APPROVED":
                confirm_match(database, transaction=transaction, invoice_id=best["invoice"].id, actor=actor)
                transaction.match_score = verdict["confidence"]
                matched_invoice_ids.add(best["invoice"].id)
                auto_matched += 1
                row.update(state="CONCILIADO", automatic=True, decision="Conciliado automáticamente: evidencia suficiente.")
            elif verdict["level"] == "SEGURO" and not auto and best["invoice"].review_status == "APPROVED":
                row["decision"] = "Evidencia suficiente: se concilia sola en la próxima conciliación."
                if persist and transaction.match_status == "UNMATCHED":
                    transaction.matched_invoice_id, transaction.match_status, transaction.match_score = best["invoice"].id, "SUGGESTED", verdict["confidence"]
            elif verdict["level"] == "SEGURO" and best["invoice"].review_status != "APPROVED":
                row["decision"] = "Evidencia suficiente, pero la factura aún no está aprobada: se concilia al aprobarla."
            elif persist and verdict["state"] == "POSIBLE" and verdict["level"] in {"SEGURO", "PROBABLE"} and transaction.match_status == "UNMATCHED" and best["invoice"].review_status == "APPROVED":
                transaction.matched_invoice_id, transaction.match_status, transaction.match_score = best["invoice"].id, "SUGGESTED", verdict["confidence"]
            elif persist and verdict["level"] == "CONFLICTO" and transaction.match_status == "SUGGESTED":
                # Una propuesta anterior ya no se sostiene: hay otra factura igual de plausible.
                transaction.matched_invoice_id, transaction.match_status, transaction.match_score = None, "UNMATCHED", None
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
        unpaid.append({"invoice_id": invoice.id, "invoice_label": label(invoice),
                       "amount": float(invoice.total), "due": due.isoformat(), "direction": "cobro" if is_issued(invoice) else "pago", "state": "FACTURA_SIN_PAGO"})
    if persist:
        database.flush()
    counts = Counter(row["state"] for row in rows)
    counts["FACTURA_SIN_PAGO"] = len(unpaid)
    levels = Counter(row["level"] for row in rows if row.get("level"))
    return {
        "movements": rows, "unpaid_invoices": unpaid, "counts": dict(counts), "levels": {level: levels.get(level, 0) for level in LEVELS},
        "auto_matched": auto_matched, "labels": STATE_LABELS, "level_labels": LEVEL_LABELS, "total": len(rows),
        "reconciled_rate": round(counts.get("CONCILIADO", 0) / max(1, len(rows) - counts.get("IGNORADO", 0)), 3) if rows else None,
        "rule": "Solo se concilia sin persona lo SEGURO: importe exacto, una prueba de identidad y una única factura posible.",
    }


def amount_mismatches(database: Session, date_from: date, date_to: date) -> list[dict[str, Any]]:
    """Movimientos del periodo que se reconocen como de una factura pero no cuadran en importe."""
    report = reconcile(database, auto=False, persist=False)
    result = []
    for row in report["movements"]:
        if row["state"] != "IMPORTE_DISTINTO" or not (date_from.isoformat() <= row["date"] <= date_to.isoformat()):
            continue
        result.append({**row, "label": f"{row['invoice_label']}: el banco dice {eur(abs(row['amount']))} (diferencia {eur(abs(row['difference']))})",
                       "difference": abs(row["difference"])})
    return result
