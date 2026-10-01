"""
Deudas y embargos: cifras deterministas y comprobables.

- ``parse_debt``: desglose de la deuda de una notificación (principal,
  recargo, intereses, costas y total pendiente). La cifra que cuenta para
  decidir es ``total_outstanding``; el resto se conserva para explicarla.
- ``amount_to_retain``: cuánto retener de lo que se le debe al embargado.

Ni la IA ni el texto libre deciden una cantidad: salen de aquí y tienen tests.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from dataclasses import dataclass
from decimal import ROUND_CEILING
from decimal import Decimal
from typing import Any

AMOUNT = r"(-?\d{1,3}(?:\.\d{3})*,\d{2}|-?\d+,\d{2}|-?\d+\.\d{2})"
GAP = r"[^\d\n]{0,45}\n?[^\d\n]{0,20}"  # etiqueta e importe en la misma línea o en la siguiente

COMPONENTS = {
    "principal": (r"principal(?: \(cuotas\))?", r"cuota(?:s)? (?:no ingresadas?|debidas?)"),
    "surcharge": (r"recargo(?: de apremio| ordinario| reducido)?(?: \(\d+ ?%\))?",),
    "interest": (r"intereses(?: de demora)?(?: \(propuesta\))?",),
    "costs": (r"costas",),
}
TOTALS = (
    r"importe pendiente", r"importe total a ingresar", r"total a ingresar", r"deuda pendiente", r"importe total de la deuda",
    r"total propuesta", r"total deuda", r"importe a ingresar", r"total pendiente",
)


def to_decimal(raw: str) -> Decimal:
    raw = raw.strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    return Decimal(raw).quantize(Decimal("0.01"))


def first_amount(labels: tuple[str, ...], text: str) -> Decimal | None:
    for label in labels:
        match = re.search(rf"(?<![a-z]){label}{GAP}{AMOUNT}", text, re.IGNORECASE)
        if match:
            return to_decimal(match.group(1))
    return None


@dataclass
class Debt:
    principal: Decimal | None = None
    surcharge: Decimal | None = None
    interest: Decimal | None = None
    costs: Decimal | None = None
    total_outstanding: Decimal | None = None
    source: str = ""  # "total" (leído) o "suma" (calculado)
    consistent: bool | None = None  # ¿cuadra el total con los conceptos?

    def as_dict(self) -> dict[str, Any]:
        return {key: (float(value) if isinstance(value, Decimal) else value) for key, value in asdict(self).items()}

    def explanation(self) -> str:
        from app.agents.base import eur

        parts = []
        if self.principal is not None:
            parts.append(f"Principal: {eur(self.principal)}")
        extras = sum((value for value in (self.surcharge, self.interest) if value), Decimal("0"))
        if extras:
            parts.append(f"Recargos/intereses: {eur(extras)}")
        if self.costs:
            parts.append(f"Costas: {eur(self.costs)}")
        if self.total_outstanding is not None:
            parts.append(f"Total pendiente: {eur(self.total_outstanding)}")
        return " · ".join(parts)


def parse_debt(text: str) -> Debt | None:
    """Desglose de la deuda. None si el documento no trae importes de deuda."""
    debt = Debt(**{name: first_amount(labels, text) for name, labels in COMPONENTS.items()})
    total = first_amount(TOTALS, text)
    components = [value for value in (debt.principal, debt.surcharge, debt.interest, debt.costs) if value is not None]
    computed = sum(components, Decimal("0")) if debt.principal is not None else None
    if total is not None:
        debt.total_outstanding, debt.source = total, "total"
        debt.consistent = abs(computed - total) <= Decimal("0.02") if computed is not None and len(components) > 1 else None
    elif computed is not None:
        debt.total_outstanding, debt.source = computed, "suma"
    if debt.total_outstanding is None:
        return None
    return debt


@dataclass
class Retention:
    retain_now: Decimal  # de lo que debemos hoy al embargado
    release: Decimal  # lo que se le puede pagar
    pending_after: Decimal  # deuda que queda sin cubrir
    successive: bool  # embargo de pagos sucesivos
    payments_needed: int | None  # pagos futuros para cubrir lo pendiente (si se conoce el importe periódico)

    def as_dict(self) -> dict[str, Any]:
        return {key: (float(value) if isinstance(value, Decimal) else value) for key, value in asdict(self).items()}


def amount_to_retain(debt_outstanding: Decimal | None, available_credit: Decimal, *, successive: bool = False, periodic_payment: Decimal | None = None) -> Retention:
    """Retener lo que se debe al embargado, con el límite de la deuda.

    Sin deuda conocida se retiene todo el crédito (lo prudente) y el resto
    queda pendiente de confirmar.
    """
    credit = max(Decimal(available_credit), Decimal("0"))
    if debt_outstanding is None:
        return Retention(retain_now=credit, release=Decimal("0"), pending_after=Decimal("0"), successive=successive, payments_needed=None)
    debt = max(Decimal(debt_outstanding), Decimal("0"))
    retain = min(debt, credit)
    pending = debt - retain
    needed = None
    if successive and pending > 0 and periodic_payment and periodic_payment > 0:
        needed = int((pending / periodic_payment).to_integral_value(rounding=ROUND_CEILING))
    return Retention(retain_now=retain, release=credit - retain, pending_after=pending, successive=successive, payments_needed=needed)


SUCCESSIVE_PATTERN = re.compile(r"pagos sucesivos|tracto sucesivo|a medida que venzan|pagos peri[oó]dicos|sucesivos vencimientos", re.IGNORECASE)


def is_successive(text: str) -> bool:
    return bool(SUCCESSIVE_PATTERN.search(text))
