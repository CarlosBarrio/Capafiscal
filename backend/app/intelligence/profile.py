"""
Perfil de la empresa para Inteligencia: un modelo común y la configuración de cada radar.

    {
      "common":       {"sector", "cnae", "region", "province", "city", "radius_km", "employees", "revenue"},
      "juridico":     {"areas": ["laboral", "mercantil", …], "client_types": […]},
      "arquitectura": {"specialties": […], "project_types": […], "min_amount": …},
      "subvenciones": {"interests": […]}
    }

Un sector nuevo (automoción, hostelería…) es otra configuración, no otra aplicación.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

REGIONS = (
    "Andalucía", "Aragón", "Asturias", "Illes Balears", "Canarias", "Cantabria", "Castilla y León", "Castilla-La Mancha",
    "Cataluña", "Comunitat Valenciana", "Extremadura", "Galicia", "Comunidad de Madrid", "Región de Murcia", "Navarra",
    "País Vasco", "La Rioja", "Ceuta", "Melilla",
)
LEGAL_AREAS = {
    "laboral": "Laboral y Seguridad Social",
    "mercantil": "Mercantil y societario",
    "fiscal": "Fiscal y tributario",
    "administrativo": "Administrativo y contratación pública",
    "civil": "Civil y consumo",
    "proteccion_datos": "Protección de datos",
}
DEFAULT_PROFILE: dict[str, Any] = {
    "common": {"sector": None, "cnae": None, "region": None, "province": None, "city": None, "radius_km": None, "employees": None, "revenue": None},
    "juridico": {"areas": [], "client_types": []},
    "arquitectura": {"specialties": [], "project_types": [], "min_amount": None},
    "subvenciones": {"interests": []},
}


def merge(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for section, values in (changes or {}).items():
        if section in result and isinstance(values, dict):
            result[section].update({key: value for key, value in values.items() if key in result[section]})
    return result


def get_profile(database: Session) -> dict[str, Any]:
    from app.models import CompanyProfile

    company = database.scalar(select(CompanyProfile).limit(1))
    profile = merge(DEFAULT_PROFILE, (company.intel_profile if company else None) or {})
    common = profile["common"]
    if company is not None:  # lo que ya sabe la ficha de la empresa no se pide dos veces
        common["province"] = common["province"] or company.province
        common["city"] = common["city"] or company.city
        common["sector"] = common["sector"] or company.activity
    profile["juridico"]["areas"] = [area for area in profile["juridico"]["areas"] if area in LEGAL_AREAS]
    return profile


def save_profile(database: Session, changes: dict[str, Any]) -> dict[str, Any]:
    from app.models import CompanyProfile

    company = database.scalar(select(CompanyProfile).limit(1))
    if company is None:
        company = CompanyProfile(name="Mi empresa")
        database.add(company)
    company.intel_profile = merge(merge(DEFAULT_PROFILE, company.intel_profile or {}), changes)
    database.flush()
    return get_profile(database)


def fingerprint(profile: dict[str, Any], radar: str) -> str:
    """Si el perfil cambia, se recalcula la relevancia (cada coincidencia guarda con qué perfil se calculó)."""
    relevant = {"common": profile["common"], radar: profile.get(radar)}
    return hashlib.sha256(json.dumps(relevant, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
