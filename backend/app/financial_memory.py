"""
Memoria financiera: cómo funciona normalmente esta empresa.

Un perfil por proveedor y por cliente, recalculado en cada barrido:

    importe        habitual (mediana) y rango normal (p10–p90)
    frecuencia     mensual, trimestral… e intervalo típico; cuándo se espera la siguiente
    IVA            tipo habitual y cuántas facturas lo cumplen
    categoría      la habitual
    pago           forma habitual, días medios hasta el pago y % de pagos tarde
    anomalías      la última y su estado
    decisiones     lo que decidió una persona la última vez

El Detector lo usa para dejar de comparar números aislados ("6 veces más que
la mediana") y explicar contra el comportamiento conocido ("suele facturar
380–430 € al mes por transferencia"). Todo es estadística descriptiva sobre
los datos de la empresa: sin IA.
"""
from __future__ import annotations

from app import clock
import statistics
from collections import Counter
from collections import defaultdict
from datetime import date
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Case
from app.models import CounterpartyProfile
from app.models import Invoice

FREQUENCIES = ((5, 9, "semanal"), (12, 18, "quincenal"), (25, 35, "mensual"), (55, 70, "bimestral"), (80, 100, "trimestral"), (170, 200, "semestral"), (340, 390, "anual"))


def party_key(invoice: Invoice, party: str) -> str | None:
    if party == "supplier":
        return (invoice.supplier_tax_id or (invoice.supplier_name or "").strip().upper()) or None
    return (invoice.customer_tax_id or (invoice.customer_name or "").strip().upper()) or None


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return ordered[index]


def frequency_label(gap: float | None) -> str:
    if gap is None:
        return "sin datos"
    for low, high, label in FREQUENCIES:
        if low <= gap <= high:
            return label
    return "irregular"


def vat_rate(invoice: Invoice) -> float | None:
    if not invoice.subtotal or not invoice.tax_total or float(invoice.subtotal) == 0:
        return None
    return round(float(invoice.tax_total) / float(invoice.subtotal) * 100)


def build_profile(items: list[Invoice], party: str, today: date) -> dict[str, Any]:
    items = sorted(items, key=lambda item: (item.invoice_date, item.id))
    totals = [abs(float(item.total)) for item in items if item.total is not None]
    gaps = [(b.invoice_date - a.invoice_date).days for a, b in zip(items, items[1:]) if (b.invoice_date - a.invoice_date).days > 0]
    gap = statistics.median(gaps) if gaps else None
    rates = Counter(rate for rate in (vat_rate(item) for item in items) if rate is not None)
    categories = Counter(item.category for item in items if item.category)
    methods = Counter(item.payment_method for item in items if item.payment_method)
    paid = [item for item in items if item.paid_at and item.invoice_date]
    days_to_pay = [(item.paid_at - item.invoice_date).days for item in paid]
    late = [item for item in paid if item.paid_at > (item.due_date or item.invoice_date + timedelta(days=60))]
    unpaid_overdue = [item for item in items if not item.paid_at and item.review_status == "APPROVED" and (item.due_date or item.invoice_date + timedelta(days=60)) < today]
    last = items[-1]
    profile: dict[str, Any] = {
        "invoices": len(items),
        "first_date": items[0].invoice_date.isoformat(),
        "last_date": last.invoice_date.isoformat(),
        "amount": {
            "median": round(statistics.median(totals), 2) if totals else None,
            "low": round(percentile(totals, 0.1), 2) if len(totals) >= 3 else (min(totals) if totals else None),
            "high": round(percentile(totals, 0.9), 2) if len(totals) >= 3 else (max(totals) if totals else None),
            "total_12m": round(sum(abs(float(item.total or 0)) for item in items if item.invoice_date >= today - timedelta(days=365)), 2),
        },
        "frequency": {"label": frequency_label(gap), "gap_days": round(gap) if gap else None,
                      "next_expected": (last.invoice_date + timedelta(days=round(gap))).isoformat() if gap else None},
        "vat": {"usual_rate": rates.most_common(1)[0][0] if rates else None, "share": round(rates.most_common(1)[0][1] / sum(rates.values()), 2) if rates else None},
        "category": categories.most_common(1)[0][0] if categories else None,
        "payment": {
            "method": methods.most_common(1)[0][0] if methods else None,
            "days_to_pay": round(statistics.median(days_to_pay)) if days_to_pay else None,
            "late_share": round(len(late) / len(paid), 2) if paid else None,
            "unpaid_overdue": len(unpaid_overdue),
            "unpaid_overdue_amount": round(sum(float(item.total or 0) for item in unpaid_overdue), 2),
        },
    }
    profile["summary"] = describe(profile, party)
    return profile


def describe(profile: dict[str, Any], party: str) -> str:
    from app.agents.base import eur

    amount = profile["amount"]
    parts = []
    if amount["median"] is not None:
        if amount["low"] is not None and amount["high"] is not None and amount["high"] > amount["low"]:
            parts.append(f"{'factura' if party == 'supplier' else 'te compra'} entre {eur(amount['low'])} y {eur(amount['high'])}")
        else:
            parts.append(f"{'factura' if party == 'supplier' else 'te compra'} unos {eur(amount['median'])}")
    if profile["frequency"]["label"] not in {"sin datos", "irregular"}:
        parts.append(f"con frecuencia {profile['frequency']['label']}")
    if profile["vat"]["usual_rate"] is not None:
        parts.append(f"IVA del {profile['vat']['usual_rate']} %")
    payment = profile["payment"]
    if payment["method"]:
        parts.append(f"se paga por {payment['method'].lower()}")
    if payment["days_to_pay"] is not None:
        parts.append(f"en {payment['days_to_pay']} días de media")
    return (", ".join(parts) + ".") if parts else "Sin historial suficiente."


def refresh_profiles(database: Session, *, today: date | None = None) -> int:
    """Recalcula y guarda los perfiles de proveedores y clientes."""
    from app.tenancy import TenantError
    from app.tenancy import current_tenant
    from app.tenancy import strict

    tenant = current_tenant(database)
    if tenant is None and strict():
        raise TenantError("No se pueden guardar perfiles sin cliente elegido.")  # la sentencia directa no pasa por before_flush
    tenant = tenant or 0
    profiles = compute_profiles(database, today=today)
    for party, key, name, profile in profiles:
        upsert_profile(database, tenant=tenant, party=party, key=key, name=name, profile=profile)
    database.expire_all()
    return len(profiles)


def compute_profiles(database: Session, *, today: date | None = None) -> list[tuple[str, str, str, dict[str, Any]]]:
    """Los perfiles calculados en memoria (solo lee): (parte, clave, nombre, perfil)."""
    today = today or clock.today()
    invoices = database.scalars(
        select(Invoice).where(Invoice.invoice_date.is_not(None), Invoice.total.is_not(None), Invoice.review_status != "REJECTED")
    ).all()
    groups: dict[tuple[str, str], list[Invoice]] = defaultdict(list)
    for invoice in invoices:
        party = "customer" if invoice.direction == "ISSUED" else "supplier"
        key = party_key(invoice, party)
        if key:
            groups[(party, key)].append(invoice)

    anomalies: dict[str, Case] = {}
    decisions: dict[str, dict[str, Any]] = {}
    for case in database.scalars(select(Case).where(Case.kind == "ANOMALY").order_by(Case.id)).all():
        key = (case.facts or {}).get("supplier_key") or case.subject_tax_id
        if not key:
            continue
        anomalies[key] = case
        if case.status in {"DISMISSED", "RESOLVED"} and case.resolution:
            decisions[key] = {"code": case.code, "decision": "descartada" if case.status == "DISMISSED" else "resuelta", "note": case.resolution[:200],
                              "date": (case.resolved_at or case.updated_at).date().isoformat() if (case.resolved_at or case.updated_at) else None}

    result = []
    for (party, key), items in groups.items():
        profile = build_profile(items, party, today)
        case = anomalies.get(key)
        profile["last_anomaly"] = {"code": case.code, "type": case.procedure, "title": case.title, "status": case.status} if case else None
        profile["last_decision"] = decisions.get(key)
        name = (items[-1].supplier_name if party == "supplier" else items[-1].customer_name) or key
        result.append((party, key, name, profile))
    return result


def upsert_profile(database: Session, *, tenant: int, party: str, key: str, name: str, profile: dict[str, Any]) -> None:
    """Insertar o actualizar en una sola sentencia: dos barridos simultáneos no chocan (PostgreSQL y SQLite)."""
    from datetime import datetime
    from datetime import timezone

    dialect = database.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    values = {"tenant_id": tenant, "party": party, "key": key, "name": name, "profile": profile, "updated_at": clock.now()}
    statement = insert(CounterpartyProfile).values(**values)
    statement = statement.on_conflict_do_update(index_elements=["tenant_id", "party", "key"], set_={"name": name, "profile": profile, "updated_at": values["updated_at"]})
    database.execute(statement)


def get_profile(database: Session, key: str | None, party: str = "supplier") -> dict[str, Any] | None:
    if not key:
        return None
    record = database.scalar(select(CounterpartyProfile).where(CounterpartyProfile.party == party, CounterpartyProfile.key == key))
    return {"name": record.name, **record.profile} if record else None


def serialize(record: CounterpartyProfile) -> dict[str, Any]:
    return {"party": record.party, "key": record.key, "name": record.name, **record.profile,
            "updated_at": record.updated_at.isoformat() if record.updated_at else None}
