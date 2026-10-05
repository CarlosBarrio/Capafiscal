"""
Búsqueda universal (Ctrl+K): un solo cuadro encuentra cualquier cosa del negocio.

    factura      número, proveedor o cliente, NIF
    tercero      proveedor o cliente (por nombre o NIF)
    expediente   código, título, referencia de la Administración
    movimiento   concepto del banco o importe («1.210», «1210,00»)
    documento    nombre del archivo
    fiscal       «303 septiembre», «modelo 130 3T», «303 2026-T3» → la posición del trimestre
    cierre       «cierre septiembre» → el cierre de ese mes

Solo lee. Los resultados llevan su acción (abrir el detalle o ir a la pantalla).
"""
from __future__ import annotations

from app import clock
import re
from datetime import date
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any

from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extractor import normalize_search_text
from app.models import BankTransaction
from app.models import Case
from app.models import Document
from app.models import Invoice

MONTHS = {"enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9,
          "octubre": 10, "noviembre": 11, "diciembre": 12}
MODELS = ("303", "130", "111", "115", "390", "347", "349", "180", "190")
PER_TYPE = 5


def amount_of(text: str) -> Decimal | None:
    cleaned = text.strip().replace("€", "").replace(" ", "")
    if not re.fullmatch(r"-?\d{1,3}(\.\d{3})*(,\d{1,2})?|-?\d+([.,]\d{1,2})?", cleaned):
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    try:
        return abs(Decimal(cleaned))
    except InvalidOperation:
        return None


def period_hint(text: str, today: date) -> tuple[int, int | None, int | None]:
    """Año, trimestre y mes que se mencionan («septiembre», «3T», «T3», «2026»)."""
    year = int(match.group(0)) if (match := re.search(r"\b20\d{2}\b", text)) else today.year
    month = next((number for name, number in MONTHS.items() if name in text), None)
    quarter = int(match.group(1) or match.group(2)) if (match := re.search(r"\b([1-4])\s*t\b|\bt\s*([1-4])\b", text)) else None
    if quarter is None and month:
        quarter = (month - 1) // 3 + 1
    return year, quarter, month


def fiscal_results(database: Session, text: str, today: date) -> list[dict[str, Any]]:
    from app.fiscal_position import position

    model = next((code for code in MODELS if re.search(rf"\b{code}\b", text)), None)
    results = []
    if model:
        year, quarter, _month = period_hint(text, today)
        quarter = quarter or (today.month - 1) // 3 + 1
        detail = f"{quarter}T {year}"
        if model in ("303", "130", "111", "115"):
            try:
                state = position(database, model, year, quarter, today=today)
                detail = f"{state['period_label']} · {state['headline']}"
            except Exception:  # el buscador nunca falla por un cálculo
                pass
        results.append({"type": "Fiscal", "title": f"Modelo {model} · {quarter}T {year}", "subtitle": detail,
                        "action": {"tab": "impuestos"}, "icon": "receipt"})
    if "cierre" in text or "cerrar" in text:
        year, _quarter, month = period_hint(text, today)
        if month:
            results.append({"type": "Cierre", "title": f"Cierre de {next(name for name, number in MONTHS.items() if number == month)} {year}",
                            "subtitle": "Qué bloquea el cierre y qué ha hecho CapaFiscal", "action": {"tab": "cierre", "period": f"{year}-{month:02d}"}, "icon": "lock"})
    return results


def search(database: Session, query: str, *, today: date | None = None) -> dict[str, Any]:
    from app.reports_service import counterparty

    today = today or clock.today()
    raw = query.strip()
    text = normalize_search_text(raw)
    if len(text) < 2:
        return {"query": raw, "results": []}
    like = f"%{raw}%"
    compact = re.sub(r"[\s.\-]", "", raw).upper()
    results: list[dict[str, Any]] = fiscal_results(database, text, today)

    invoices = database.scalars(select(Invoice).where(or_(
        Invoice.invoice_number.ilike(like), Invoice.supplier_name.ilike(like), Invoice.customer_name.ilike(like),
        Invoice.supplier_tax_id.ilike(f"%{compact}%"), Invoice.customer_tax_id.ilike(f"%{compact}%"), Invoice.concept.ilike(like),
    )).order_by(Invoice.invoice_date.desc().nulls_last(), Invoice.id.desc()).limit(40)).all()
    parties: dict[str, dict[str, Any]] = {}
    for invoice in invoices:
        name, tax_id = counterparty(invoice)
        key = tax_id or (name or "").upper()
        if key and key not in parties and (normalize_search_text(name or "").find(text) >= 0 or (tax_id and compact in tax_id.upper())):
            issued = invoice.direction == "ISSUED"
            parties[key] = {"type": "Cliente" if issued else "Proveedor", "title": name or tax_id, "subtitle": tax_id or "sin NIF",
                            "action": {"tab": "proveedores", "party": "customer" if issued else "supplier", "key": key}, "icon": "briefcase"}
    results += list(parties.values())[:PER_TYPE]
    for invoice in invoices[:PER_TYPE]:
        name, tax_id = counterparty(invoice)
        results.append({"type": "Factura", "title": f"{invoice.invoice_number or 's/n'} · {name or ''}".strip(" ·"),
                        "subtitle": " · ".join(part for part in (invoice.invoice_date.strftime("%d/%m/%Y") if invoice.invoice_date else None,
                                                                    f"{invoice.total:.2f} €".replace(".", ",") if invoice.total is not None else None,
                                                                    {"PENDING": "por revisar", "APPROVED": "aprobada", "REJECTED": "rechazada"}.get(invoice.review_status)) if part),
                        "action": {"document_id": invoice.document_id}, "icon": "file"})

    for case in database.scalars(select(Case).where(or_(Case.code.ilike(like), Case.title.ilike(like), Case.reference.ilike(like), Case.subject_name.ilike(like)))
                                 .order_by(Case.id.desc()).limit(PER_TYPE)).all():
        results.append({"type": "Expediente", "title": f"{case.code} · {case.title}", "subtitle": case.reference or case.status.replace("_", " ").lower(),
                        "action": {"case_id": case.id}, "icon": "archive"})

    amount = amount_of(raw)
    movements = select(BankTransaction)
    movements = movements.where(or_(BankTransaction.amount == amount, BankTransaction.amount == -amount)) if amount is not None else movements.where(BankTransaction.description.ilike(like))
    for row in database.scalars(movements.order_by(BankTransaction.booking_date.desc()).limit(PER_TYPE)).all():
        results.append({"type": "Movimiento", "title": row.description[:80], "subtitle": f"{row.booking_date:%d/%m/%Y} · {row.amount:.2f} €".replace(".", ","),
                        "action": {"tab": "negocio", "anchor": "bankCard"}, "icon": "link"})

    for document in database.scalars(select(Document).where(Document.original_filename.ilike(like)).order_by(Document.id.desc()).limit(PER_TYPE)).all():
        if any(item["action"].get("document_id") == document.id for item in results):
            continue
        results.append({"type": "Documento", "title": document.original_filename, "subtitle": (document.kind or "").lower() or "documento",
                        "action": {"document_id": document.id} if document.kind != "NOTIFICATION" else {"tab": "notificaciones"}, "icon": "file"})
    return {"query": raw, "results": results}
