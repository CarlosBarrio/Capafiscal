"""
Conciliación profesional: lo que una conciliación 1 movimiento ↔ 1 factura no resuelve.

    MULTI        un pago que liquida varias facturas del mismo proveedor (o un cobro de varias)
    PARCIAL      un pago a cuenta: la factura queda con saldo pendiente; varios pagos la completan
    ANTICIPO     un cobro de un cliente conocido antes de su factura
    DEVOLUCION   el banco devuelve un recibo / un cobro: se anula contra el movimiento original
    COMISION     comisiones y gastos bancarios
    TRASPASO     movimiento entre cuentas propias (sale de una y entra en otra)
    NOMINA       pago de nóminas (cuadra con el líquido de la nómina aprobada)
    SEG_SOCIAL   seguros sociales (cuadra con las cuotas de la nómina)
    IMPUESTO     pago de un modelo a Hacienda (cuadra con lo presentado)

Cada propuesta lleva nivel (SEGURO / PROBABLE), las comprobaciones hechas y las partes en que
se reparte el movimiento. Solo lo SEGURO se aplica sin persona, igual que en la conciliación 1:1.
Lo aplicado se guarda en BankAllocation; la suma nunca supera el movimiento.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from itertools import combinations
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extractor import normalize_search_text
from app.models import BankAllocation
from app.models import BankTransaction
from app.models import Invoice

KINDS = {
    "FACTURA": "Factura", "MULTI": "Varias facturas", "PARCIAL": "Pago parcial", "ANTICIPO": "Anticipo", "DEVOLUCION": "Devolución",
    "COMISION": "Comisión bancaria", "TRASPASO": "Traspaso entre cuentas", "NOMINA": "Nóminas", "SEG_SOCIAL": "Seguridad Social",
    "IMPUESTO": "Impuestos", "OTRO": "Otro",
}
JUSTIFY_KINDS = ("ANTICIPO", "DEVOLUCION", "COMISION", "TRASPASO", "NOMINA", "SEG_SOCIAL", "IMPUESTO", "OTRO")
CENT = Decimal("0.01")
MAX_MULTI = 6          # facturas como mucho en un mismo pago (búsqueda acotada)
MAX_OPEN = 14          # facturas abiertas de una misma contraparte que se combinan
SMALL_FEE = Decimal("100")

# Palabras del concepto del banco (ya normalizado: minúsculas y sin acentos).
NATURE = (
    ("TRASPASO", re.compile(r"\btraspaso|\btrasp\b|entre cuentas|cuenta propia")),
    ("DEVOLUCION", re.compile(r"\bdevol|retrocesion|recibo devuelto|\bdevuelto")),
    ("COMISION", re.compile(r"\bcomision|mantenimiento|gastos bancarios|cuota tarjeta|\bcom\.? ")),
    ("SEG_SOCIAL", re.compile(r"\btgss\b|seguridad social|seg\.? social|seguros sociales|\brlc\b")),
    ("NOMINA", re.compile(r"\bnomina")),
    ("IMPUESTO", re.compile(r"\baeat\b|agencia tributaria|hacienda|\bimpuesto|modelo \d{3}|\bmod\.? ?\d{3}")),
)


def money(value: Any) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT)


def eur(value: Any) -> str:
    text = f"{money(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{text} €"


def nature(transaction: BankTransaction) -> str | None:
    text = normalize_search_text(transaction.description) + " "
    for kind, pattern in NATURE:
        if pattern.search(text):
            return kind
    return None


def allocated_by_transaction(database: Session) -> dict[int, Decimal]:
    rows = database.execute(select(BankAllocation.transaction_id, func.sum(BankAllocation.amount)).group_by(BankAllocation.transaction_id)).all()
    return {transaction_id: money(total) for transaction_id, total in rows}


def outstanding_by_invoice(database: Session, invoices: list[Invoice]) -> dict[int, Decimal]:
    """Lo que queda por pagar o cobrar de cada factura (0 si ya está pagada)."""
    paid = dict(database.execute(
        select(BankAllocation.invoice_id, func.sum(BankAllocation.amount)).where(BankAllocation.invoice_id.is_not(None)).group_by(BankAllocation.invoice_id)
    ).all())
    result = {}
    for invoice in invoices:
        if invoice.total is None:
            continue
        result[invoice.id] = Decimal("0") if invoice.paid_at else max(Decimal("0"), abs(money(invoice.total)) - money(paid.get(invoice.id)))
    return result


def check(label: str, ok: bool | None) -> dict[str, Any]:
    return {"label": label, "ok": ok}


def proposal(kind: str, level: str, explanation: str, checks: list[dict[str, Any]], parts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"kind": kind, "label": KINDS[kind], "level": level, "explanation": explanation, "checks": checks, "parts": parts}


class Context:
    """Lo que hace falta para proponer, leído una sola vez por conciliación."""

    def __init__(self, database: Session, transactions: list[BankTransaction], invoices: list[Invoice]):
        from app.models import Customer
        from app.models import PayrollRun
        from app.models import TaxFiling
        from app.payroll_service import run_totals

        self.transactions = transactions
        self.invoices = [item for item in invoices if item.review_status == "APPROVED" and item.total is not None]
        self.outstanding = outstanding_by_invoice(database, self.invoices)
        self.allocated = allocated_by_transaction(database)
        self.filings = [money(item.amount) for item in database.scalars(select(TaxFiling)).all() if item.amount]
        self.payroll: list[tuple[str, Decimal, Decimal]] = []
        for run in database.scalars(select(PayrollRun).where(PayrollRun.status.in_(("APPROVED", "PAID")))).all():
            totals = run_totals(run)
            self.payroll.append((f"{run.month:02d}/{run.year}", money(totals["net"]), money(totals["ss_employee"] + totals["ss_employer"])))
        self.customers = [normalize_search_text(name) for name in database.scalars(select(Customer.name)).all() if name]

    def remaining(self, transaction: BankTransaction) -> Decimal:
        return abs(money(transaction.amount)) - self.allocated.get(transaction.id, Decimal("0"))


def counterpart(ctx: Context, transaction: BankTransaction, *, days: int, opposite: bool = True, before_only: bool = False) -> BankTransaction | None:
    amount = money(transaction.amount)
    for other in ctx.transactions:
        if other.id == transaction.id:
            continue
        if money(other.amount) != (-amount if opposite else amount):
            continue
        gap = (transaction.booking_date - other.booking_date).days
        if (0 <= gap <= days) if before_only else abs(gap) <= days:
            return other
    return None


def propose(ctx: Context, transaction: BankTransaction) -> dict[str, Any] | None:
    """La mejor explicación de un movimiento que no casa con una sola factura. No escribe."""
    from app.reconciliation import mentions

    remaining = ctx.remaining(transaction)
    if remaining < CENT:
        return None
    amount = money(transaction.amount)
    kind = nature(transaction)

    if kind is None:
        returned = next((other for other in ctx.transactions if other.id != transaction.id and money(other.amount) == -amount
                         and 0 < (other.booking_date - transaction.booking_date).days <= 60 and nature(other) == "DEVOLUCION"), None)
        if returned is not None:
            return proposal("DEVOLUCION", "SEGURO", f"El banco lo devolvió el {returned.booking_date:%d/%m/%Y}: este movimiento y su devolución se anulan.",
                            [check("devolución posterior por el mismo importe", True)],
                            [{"kind": "DEVOLUCION", "amount": float(remaining), "related_transaction_id": returned.id}])
    if kind == "TRASPASO":
        pair = counterpart(ctx, transaction, days=3)
        own = pair is not None and (pair.account_label or "") != (transaction.account_label or "")
        checks = [check("concepto de traspaso", True), check("movimiento contrario por el mismo importe en otra cuenta" if pair else "sin el movimiento contrario en otra cuenta importada", bool(pair))]
        return proposal("TRASPASO", "SEGURO" if pair else "PROBABLE",
                        "Traspaso entre cuentas propias: no es gasto ni ingreso." + ("" if own or not pair else " (misma cuenta en el extracto)"),
                        checks, [{"kind": "TRASPASO", "amount": float(remaining), "related_transaction_id": pair.id if pair else None}])
    if kind == "DEVOLUCION":
        original = counterpart(ctx, transaction, days=60, before_only=True)
        checks = [check("concepto de devolución", True), check(f"movimiento original del {original.booking_date:%d/%m/%Y}" if original else "sin el movimiento original en el extracto", bool(original))]
        return proposal("DEVOLUCION", "SEGURO" if original else "PROBABLE",
                        f"Devolución de {original.description[:60]}: anula aquel movimiento." if original else "Devolución: falta localizar el movimiento original.",
                        checks, [{"kind": "DEVOLUCION", "amount": float(remaining), "related_transaction_id": original.id if original else None}])
    if kind == "COMISION" and amount < 0:
        small = remaining < SMALL_FEE
        return proposal("COMISION", "SEGURO" if small else "PROBABLE", "Comisión o gasto del banco: se justifica con la liquidación del banco, sin factura de proveedor.",
                        [check("concepto de comisión o gasto bancario", True), check(f"importe pequeño (menos de {eur(SMALL_FEE)})", small)],
                        [{"kind": "COMISION", "amount": float(remaining)}])
    if kind in ("NOMINA", "SEG_SOCIAL") and amount < 0:
        index = 1 if kind == "NOMINA" else 2
        match = next((row[0] for row in ctx.payroll if abs(row[index] - remaining) <= CENT), None)
        what = "el líquido" if kind == "NOMINA" else "las cuotas"
        return proposal(kind, "SEGURO" if match else "PROBABLE",
                        (f"Cuadra con {what} de la nómina de {match}." if match else f"Por el concepto es {KINDS[kind].lower()}, pero no cuadra con {what} de ninguna nómina aprobada."),
                        [check(f"concepto de {KINDS[kind].lower()}", True), check(f"importe igual a {what} de una nómina aprobada", bool(match))],
                        [{"kind": kind, "amount": float(remaining)}])
    if kind == "IMPUESTO" and amount < 0:
        match = any(abs(value - remaining) <= CENT for value in ctx.filings)
        return proposal("IMPUESTO", "SEGURO" if match else "PROBABLE",
                        "Cuadra con un modelo presentado." if match else "Pago a Hacienda que no cuadra con ningún modelo registrado como presentado.",
                        [check("concepto de Hacienda / impuesto", True), check("importe igual a un modelo presentado", match)],
                        [{"kind": "IMPUESTO", "amount": float(remaining)}])

    # Facturas de la misma contraparte: varias en un pago, o una pagada en parte.
    inflow = amount > 0
    open_invoices = [invoice for invoice in ctx.invoices if ctx.outstanding.get(invoice.id, 0) >= CENT and (invoice.direction == "ISSUED") == inflow
                     and (not invoice.invoice_date or invoice.invoice_date - timedelta(days=5) <= transaction.booking_date)]
    identified = []
    for invoice in open_invoices:
        evidence = mentions(transaction, invoice)
        if evidence["name"] or evidence["tax_id"] or evidence["number"]:
            identified.append((invoice, evidence))
    if identified:
        identified.sort(key=lambda pair: (pair[0].invoice_date or transaction.booking_date))
        pool = identified[:MAX_OPEN]
        subsets = []
        for size in range(2, min(MAX_MULTI, len(pool)) + 1):
            for combo in combinations(pool, size):
                if abs(sum(ctx.outstanding[invoice.id] for invoice, _ in combo) - remaining) <= CENT:
                    subsets.append(combo)
                    if len(subsets) > 1:
                        break
            if len(subsets) > 1:
                break
        if subsets:
            combo = subsets[0]
            unique = len(subsets) == 1
            numbers = sum(1 for _, evidence in combo if evidence["number"])
            names = ", ".join(invoice.invoice_number or "s/n" for invoice, _ in combo)
            return proposal("MULTI", "SEGURO" if unique else "PROBABLE",
                            f"Un solo {'cobro' if inflow else 'pago'} por {len(combo)} facturas ({names})." + ("" if unique else " Hay otra combinación con el mismo total: elige tú."),
                            [check("suma exacta de las facturas pendientes", True), check("proveedor o cliente identificado en el concepto", True),
                             check(f"{numbers} de {len(combo)} números de factura en el concepto", numbers == len(combo) or None), check("una única combinación posible", unique)],
                            [{"kind": "FACTURA", "invoice_id": invoice.id, "amount": float(ctx.outstanding[invoice.id]), "label": invoice.invoice_number} for invoice, _ in combo])
        invoice, evidence = identified[0]
        pending = ctx.outstanding[invoice.id]
        if len(identified) == 1 and abs(remaining - pending) <= CENT and pending < abs(money(invoice.total)):
            strong = evidence["number"] or evidence["tax_id"]
            return proposal("PARCIAL", "SEGURO" if strong else "PROBABLE", f"Último pago de {invoice.invoice_number or 'la factura'}: con este queda pagada.",
                            [check("contraparte identificada en el concepto", True), check("número de factura o NIF en el concepto", strong),
                             check(f"importe igual a lo pendiente ({eur(pending)})", True)],
                            [{"kind": "FACTURA", "invoice_id": invoice.id, "amount": float(remaining), "label": invoice.invoice_number}])
        if len(identified) == 1 and remaining < pending - CENT:
            return proposal("PARCIAL", "PROBABLE", f"Pago a cuenta de {invoice.invoice_number or 'la factura'}: quedarían {eur(pending - remaining)} pendientes.",
                            [check("contraparte identificada en el concepto", True), check("número de factura en el concepto", evidence["number"]),
                             check(f"importe menor que lo pendiente ({eur(pending)})", True)],
                            [{"kind": "FACTURA", "invoice_id": invoice.id, "amount": float(remaining), "label": invoice.invoice_number}])
    if inflow:
        text = normalize_search_text(transaction.description)
        customer = next((name for name in ctx.customers if name and name.split(" ")[0] in text and len(name.split(" ")[0]) >= 4), None)
        if customer:
            return proposal("ANTICIPO", "PROBABLE", "Cobro de un cliente sin factura emitida: puede ser un anticipo a cuenta de la próxima factura.",
                            [check("cliente conocido en el concepto", True), check("factura pendiente de ese cliente", False)],
                            [{"kind": "ANTICIPO", "amount": float(remaining)}])
    return None


def apply(database: Session, transaction: BankTransaction, parts: list[dict[str, Any]], *, actor: str, note: str | None = None) -> list[BankAllocation]:
    """Aplica el movimiento (o parte) a facturas o a una justificación. Valida antes de escribir."""
    from app.bank_service import payment_method_from_description
    from app.invoice_service import add_audit_event
    from app.reports_service import is_issued

    if not parts:
        raise ValueError("Indica a qué se aplica el movimiento.")
    ctx_allocated = money(database.scalar(select(func.sum(BankAllocation.amount)).where(BankAllocation.transaction_id == transaction.id)))
    available = abs(money(transaction.amount)) - ctx_allocated
    total = sum(money(part.get("amount")) for part in parts)
    if any(money(part.get("amount")) <= 0 for part in parts):
        raise ValueError("Cada parte debe tener un importe positivo.")
    if total - available > CENT:
        raise ValueError(f"Lo aplicado ({eur(total)}) supera lo que queda del movimiento ({eur(available)}).")

    created = []
    touched: dict[int, Invoice] = {}
    for part in parts:
        kind = part.get("kind") or ("FACTURA" if part.get("invoice_id") else "OTRO")
        if kind not in KINDS or kind == "MULTI" or kind == "PARCIAL":
            raise ValueError(f"Tipo no válido: {kind}.")
        invoice = None
        if kind == "FACTURA":
            invoice = database.get(Invoice, part.get("invoice_id"))
            if invoice is None:
                raise ValueError("Factura no encontrada.")
            if invoice.review_status != "APPROVED":
                raise ValueError(f"La factura {invoice.invoice_number or invoice.id} no está aprobada.")
            if is_issued(invoice) != (transaction.amount > 0):
                raise ValueError("El sentido no coincide: los cobros se aplican a facturas emitidas y los pagos a recibidas.")
            pending = outstanding_by_invoice(database, [invoice])[invoice.id]
            if money(part["amount"]) - pending > CENT:
                raise ValueError(f"A la factura {invoice.invoice_number or invoice.id} solo le quedan {eur(pending)} pendientes.")
            touched[invoice.id] = invoice
        allocation = BankAllocation(transaction_id=transaction.id, invoice_id=invoice.id if invoice else None, kind=kind, amount=money(part["amount"]),
                                    related_transaction_id=part.get("related_transaction_id"), note=part.get("note") or note, created_by=actor)
        database.add(allocation)
        created.append(allocation)
    database.flush()

    for invoice in touched.values():
        if outstanding_by_invoice(database, [invoice])[invoice.id] < CENT:
            invoice.paid_at = transaction.booking_date
            invoice.payment_method = invoice.payment_method or payment_method_from_description(transaction.description)
        add_audit_event(database, action="bank.reconciled", entity_type="invoice", entity_id=invoice.id, actor=actor,
                        event_data={"document_id": invoice.document_id, "transaction_id": transaction.id, "amount": float(sum(item.amount for item in created if item.invoice_id == invoice.id)),
                                    "booking_date": transaction.booking_date, "description": transaction.description, "partial": invoice.paid_at is None})
    if available - total <= CENT:
        transaction.match_status = "MATCHED"
        transaction.matched_invoice_id = next(iter(touched), None)
    add_audit_event(database, action="bank.allocated", entity_type="bank_transaction", entity_id=transaction.id, actor=actor,
                    event_data={"parts": [{"kind": item.kind, "invoice_id": item.invoice_id, "amount": float(item.amount)} for item in created], "note": note})
    database.flush()
    return created


def clear(database: Session, transaction: BankTransaction) -> int:
    """Deshace lo aplicado de un movimiento (al desconciliar). Las facturas vuelven a quedar pendientes."""
    allocations = database.scalars(select(BankAllocation).where(BankAllocation.transaction_id == transaction.id)).all()
    for allocation in allocations:
        if allocation.invoice_id:
            invoice = database.get(Invoice, allocation.invoice_id)
            if invoice is not None:
                invoice.paid_at = None
        database.delete(allocation)
    database.flush()
    return len(allocations)


def allocations_of(database: Session, transaction_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if not transaction_ids:
        return result
    for item in database.scalars(select(BankAllocation).where(BankAllocation.transaction_id.in_(transaction_ids)).order_by(BankAllocation.id)).all():
        invoice = database.get(Invoice, item.invoice_id) if item.invoice_id else None
        result[item.transaction_id].append({"kind": item.kind, "label": KINDS[item.kind], "amount": float(item.amount), "invoice_id": item.invoice_id,
                                            "invoice_number": invoice.invoice_number if invoice else None, "note": item.note})
    return result


def accept_proposal(database: Session, transaction: BankTransaction, *, actor: str) -> dict[str, Any]:
    """Una persona acepta la propuesta (también las PROBABLE): se aplica tal cual se mostró."""
    if transaction.match_status in ("MATCHED", "IGNORED"):
        raise ValueError("Ese movimiento ya está conciliado o descartado.")
    transactions = database.scalars(select(BankTransaction).order_by(BankTransaction.booking_date, BankTransaction.id)).all()
    invoices = database.scalars(select(Invoice).where(Invoice.review_status != "REJECTED", Invoice.total.is_not(None))).all()
    plan = propose(Context(database, transactions, invoices), transaction)
    if plan is None:
        raise ValueError("No hay ninguna propuesta para este movimiento: aplícalo a mano.")
    apply(database, transaction, plan["parts"], actor=actor, note=f"Aceptado: {plan['label'].lower()}")
    return plan
