"""Deuda y embargo: cifras deterministas (no las decide la IA)."""
from __future__ import annotations

from decimal import Decimal as D

from app.debt import amount_to_retain
from app.debt import is_successive
from app.debt import parse_debt


def test_credit_greater_than_debt_retains_only_the_debt():
    retention = amount_to_retain(D("2850"), D("4100"))
    assert retention.retain_now == D("2850") and retention.release == D("1250") and retention.pending_after == 0


def test_successive_payments_until_the_debt_is_covered():
    retention = amount_to_retain(D("6200"), D("1250"), successive=True, periodic_payment=D("1250"))
    assert retention.retain_now == D("1250") and retention.pending_after == D("4950") and retention.payments_needed == 4


def test_nothing_owed_nothing_retained():
    retention = amount_to_retain(D("2850"), D("0"))
    assert retention.retain_now == 0 and retention.release == 0 and retention.pending_after == D("2850")


def test_unknown_debt_retains_everything_until_confirmed():
    assert amount_to_retain(None, D("900")).retain_now == D("900")


def test_debt_breakdown_uses_the_outstanding_total_not_the_principal():
    text = "Deudas\nPrincipal\n2.375,00 €\nRecargo de apremio\n475,00 €\nIntereses de demora\n0,00 €\nCostas\n0,00 €\nImporte pendiente\n2.850,00 €"
    debt = parse_debt(text)
    assert debt.total_outstanding == D("2850.00") and debt.principal == D("2375.00") and debt.surcharge == D("475.00")
    assert debt.consistent is True and debt.source == "total"
    assert "Total pendiente: 2.850,00 €" in debt.explanation()


def test_debt_without_total_is_the_sum_of_its_parts():
    debt = parse_debt("Principal: 1.000,00 €\nRecargo (20 %): 200,00 €")
    assert debt.total_outstanding == D("1200.00") and debt.source == "suma"


def test_a_request_without_debt_has_no_amount():
    assert parse_debt("Aporte las facturas de importe superior a 1.000 euros en 10 días hábiles.") is None


def test_successive_payment_wording():
    assert is_successive("retener los pagos sucesivos que deba efectuar (créditos de tracto sucesivo)")
    assert not is_successive("Se declaran embargados los créditos pendientes.")
