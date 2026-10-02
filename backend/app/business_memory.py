"""
Memoria del negocio: «esto es raro para ti», no «esto es raro en general».

Compara una factura con lo que ese proveedor o cliente hace normalmente CON ESTA EMPRESA
(sus facturas anteriores, sin contar la que se mira):

    nuevo          primera factura de ese proveedor
    importe        fuera de su rango habitual (p10–p90), con cuántas veces lo normal
    IVA            tipo distinto del que aplica siempre
    categoría      distinta de la habitual
    frecuencia     llega mucho antes de lo esperado (¿duplicado o cambio de condiciones?)
    IBAN           la cuenta de pago cambia respecto a sus facturas anteriores (fraude típico)

Estadística descriptiva sobre los datos de la empresa: sin IA. Solo lee.
"""
from __future__ import annotations

from app import clock
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Invoice

MIN_HISTORY = 3


def eur(value: Any) -> str:
    from app.agents.base import eur as format_eur

    return format_eur(value)


def invoice_ibans(database: Session, invoice: Invoice) -> set[str]:
    from app.models import ExtractionRun
    from app.reconciliation import ibans

    if not invoice.document_id:
        return set()
    text = database.scalar(select(ExtractionRun.raw_text).where(ExtractionRun.document_id == invoice.document_id).order_by(ExtractionRun.id.desc()).limit(1))
    own = set()
    from app.models import CompanyProfile

    company = database.scalar(select(CompanyProfile).limit(1))
    if company is not None and getattr(company, "iban", None):
        own = {company.iban.replace(" ", "").upper()}
    return ibans(text) - own


def unusual(database: Session, invoice: Invoice, *, today: date | None = None, pool: list[Invoice] | None = None) -> dict[str, Any]:
    """Lo que esta factura tiene de raro para esta empresa, en frases, con el dato que lo sostiene."""
    from app.financial_memory import build_profile
    from app.financial_memory import party_key
    from app.financial_memory import vat_rate

    today = today or clock.today()
    party = "customer" if invoice.direction == "ISSUED" else "supplier"
    key = party_key(invoice, party)
    name = (invoice.customer_name if party == "customer" else invoice.supplier_name) or "este proveedor"
    if not key or invoice.total is None or invoice.invoice_date is None:
        return {"key": key, "history": 0, "signals": [], "summary": None}
    if pool is None:
        pool = list(database.scalars(select(Invoice).where(Invoice.invoice_date.is_not(None), Invoice.total.is_not(None), Invoice.review_status != "REJECTED")).all())
    history = [item for item in pool
               if item.id != invoice.id and item.invoice_date and item.total is not None and item.review_status != "REJECTED"
               and party_key(item, party) == key and ("customer" if item.direction == "ISSUED" else "supplier") == party
               and item.invoice_date <= invoice.invoice_date]
    signals: list[dict[str, Any]] = []
    if not history:
        signals.append({"kind": "nuevo", "severity": "info", "text": f"Primera factura de {name}: no hay historial con el que compararla."})
        return {"key": key, "history": 0, "signals": signals, "summary": None}

    profile = build_profile(history, party, today)
    amount, total = profile["amount"], abs(float(invoice.total))
    if len(history) >= MIN_HISTORY and amount["low"] is not None and amount["high"] is not None:
        if total > amount["high"] * 1.25 or total < amount["low"] * 0.75:
            ratio = total / amount["median"] if amount["median"] else None
            usual = (f"entre {eur(amount['low'])} y {eur(amount['high'])}" if amount["high"] > amount["low"] else eur(amount["median"]))
            times = (str(round(ratio)) if ratio and abs(ratio - round(ratio)) < 0.05 else str(round(ratio, 1)).replace(".", ",")) if ratio else None
            signals.append({"kind": "importe", "severity": "high" if ratio and (ratio >= 2 or ratio <= 0.5) else "medium",
                            "text": f"{name} suele facturarte {usual}; esta es de {eur(total)}" + (f" ({times} veces lo habitual)." if times else ".")})
    usual_rate, share = profile["vat"]["usual_rate"], profile["vat"]["share"] or 0
    rate = vat_rate(invoice)
    if usual_rate is not None and rate is not None and share >= 0.8 and len(history) >= MIN_HISTORY and abs(rate - usual_rate) >= 1:
        signals.append({"kind": "iva", "severity": "medium", "text": f"Siempre aplica un IVA del {usual_rate} % y esta factura lleva un {rate} %."})
    if profile["category"] and invoice.category and invoice.category != profile["category"] and len(history) >= MIN_HISTORY:
        signals.append({"kind": "categoria", "severity": "low", "text": f"Sus facturas suelen ir a «{profile['category']}» y esta está en «{invoice.category}»."})
    gap_days = profile["frequency"]["gap_days"]
    last = max(item.invoice_date for item in history)
    since = (invoice.invoice_date - last).days
    if gap_days and gap_days >= 25 and 0 <= since < gap_days * 0.4 and len(history) >= MIN_HISTORY:
        signals.append({"kind": "frecuencia", "severity": "medium",
                        "text": f"Factura con frecuencia {profile['frequency']['label']} y esta llega {since} días después de la anterior: ¿duplicado o cambio de condiciones?"})
    if party == "supplier":
        current = invoice_ibans(database, invoice)
        previous = set().union(*(invoice_ibans(database, item) for item in history[-6:])) if history else set()
        if current and previous and not current & previous:
            masked = ", ".join(f"…{iban[-4:]}" for iban in sorted(current))
            signals.append({"kind": "iban", "severity": "high",
                            "text": f"Cambia la cuenta de pago: esta factura pide pagar en {masked} y las anteriores en otra cuenta. Confírmalo por teléfono antes de pagar."})
    return {"key": key, "history": len(history), "signals": signals, "summary": profile["summary"]}
