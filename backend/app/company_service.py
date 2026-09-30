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

    database.flush()

    return profile
