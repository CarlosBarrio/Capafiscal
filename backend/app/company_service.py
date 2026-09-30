from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.extractor import is_valid_spanish_tax_id
from app.extractor import normalize_tax_id
from app.models import CompanyProfile


LEGAL_FORMS = ("AUTONOMO", "SOCIEDAD")

DEFAULT_HOURLY_COST = Decimal("25.00")


def get_company_profile(database: Session) -> CompanyProfile | None:
    return database.scalar(select(CompanyProfile).limit(1))


def company_tax_ids(database: Session) -> set[str]:
    """
    NIF/CIF de la propia empresa: el de «Mi empresa» más los que haya en
    la configuración (.env). Sirven para distinguir emitidas de recibidas.
    """
    values: set[str] = set()

    profile = get_company_profile(database)

    if profile and profile.tax_id:
        values.add(profile.tax_id)

    for raw in (settings.company_tax_ids, settings.company_tax_id):
        for part in (raw or "").replace(";", ",").split(","):
            normalized = normalize_tax_id(part)

            if normalized:
                values.add(normalized)

    return values


def company_name(database: Session) -> str | None:
    profile = get_company_profile(database)

    if profile and profile.name:
        return profile.name

    return settings.company_name


def legal_form(database: Session) -> str:
    profile = get_company_profile(database)

    if profile and profile.legal_form in LEGAL_FORMS:
        return profile.legal_form

    return "SOCIEDAD"


def hourly_cost(database: Session) -> Decimal:
    profile = get_company_profile(database)

    if profile and profile.hourly_cost:
        return Decimal(profile.hourly_cost)

    return DEFAULT_HOURLY_COST


def serialize_profile(database: Session) -> dict[str, Any]:
    profile = get_company_profile(database)
    tax_ids = sorted(company_tax_ids(database))

    return {
        "name": company_name(database),
        "tax_id": profile.tax_id if profile else (tax_ids[0] if tax_ids else None),
        "tax_id_valid": is_valid_spanish_tax_id(
            profile.tax_id if profile else (tax_ids[0] if tax_ids else None)
        ),
        "legal_form": legal_form(database),
        "activity": profile.activity if profile else None,
        "email": profile.email if profile else None,
        "hourly_cost": float(hourly_cost(database)),
        "iban": profile.iban if profile else None,
        "bic": profile.bic if profile else None,
        "at_ep_rate": float(profile.at_ep_rate) if profile and profile.at_ep_rate is not None else 1.5,
        "address": profile.address if profile else None,
        "postal_code": profile.postal_code if profile else None,
        "city": profile.city if profile else None,
        "province": profile.province if profile else None,
        "phone": profile.phone if profile else None,
        "invoice_footer": profile.invoice_footer if profile else None,
        "default_payment_days": profile.default_payment_days if profile else None,
        "advisor_email": profile.advisor_email if profile else None,
        "late_interest_rate": float(profile.late_interest_rate) if profile and profile.late_interest_rate is not None else None,
        "configured": bool(tax_ids),
        "all_tax_ids": tax_ids,
    }


def update_company_profile(
    database: Session,
    payload: dict[str, Any],
) -> CompanyProfile:
    profile = get_company_profile(database)

    if profile is None:
        profile = CompanyProfile()
        database.add(profile)

    if "name" in payload:
        profile.name = (payload["name"] or "").strip() or None

    if "tax_id" in payload:
        profile.tax_id = normalize_tax_id(payload["tax_id"])

    if "legal_form" in payload:
        value = (payload["legal_form"] or "").strip().upper()

        if value and value not in LEGAL_FORMS:
            raise ValueError("La forma jurídica debe ser AUTONOMO o SOCIEDAD.")

        profile.legal_form = value or None

    if "activity" in payload:
        profile.activity = (payload["activity"] or "").strip() or None

    if "email" in payload:
        profile.email = (payload["email"] or "").strip() or None

    if "hourly_cost" in payload:
        value = payload["hourly_cost"]
        profile.hourly_cost = (
            Decimal(str(value)) if value not in (None, "") else None
        )

    if "iban" in payload:
        from app.payroll_service import iban_is_valid
        from app.payroll_service import normalize_iban

        value = normalize_iban(payload["iban"])

        if value and not iban_is_valid(value):
            raise ValueError("El IBAN de la empresa no es válido.")

        profile.iban = value or None

    if "bic" in payload:
        profile.bic = (payload["bic"] or "").strip().upper() or None

    if "at_ep_rate" in payload:
        value = payload["at_ep_rate"]
        profile.at_ep_rate = Decimal(str(value)) if value not in (None, "") else None

    for field in ("address", "postal_code", "city", "province", "phone", "invoice_footer", "advisor_email"):
        if field in payload:
            setattr(profile, field, (payload[field] or "").strip() or None)

    if "default_payment_days" in payload:
        profile.default_payment_days = payload["default_payment_days"]

    if "late_interest_rate" in payload:
        value = payload["late_interest_rate"]
        profile.late_interest_rate = Decimal(str(value)) if value not in (None, "") else None

    database.flush()

    return profile
