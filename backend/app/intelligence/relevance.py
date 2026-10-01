"""
Motor de relevancia: primero reglas deterministas, y solo los candidatos pasan a la IA.

Para el radar jurídico, una disposición del BOE es relevante si:
    - es de una sección con normas o actos de alcance general (I, III, Tribunal Constitucional);
    - toca un área configurada: palabras del área en el título, el epígrafe o las materias
      oficiales del BOE, o un departamento que legisla esa área;
    - no es una norma de otra comunidad autónoma.

Relevancia (sin porcentajes inventados):
    alta   sección I + rango normativo + el área aparece en el título o en las materias
    media  el área aparece, pero en otra sección, solo por departamento o es una corrección de errores
    baja   no se muestra
Cada decisión deja sus razones ✓/✗ en lenguaje de persona.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from app.intelligence.profile import LEGAL_AREAS
from app.intelligence.profile import REGIONS

AREA_TERMS: dict[str, tuple[str, ...]] = {
    "laboral": ("trabajo", "trabajador", "laboral", "empleo", "salario", "salarial", "cotizacion", "seguridad social", "jornada",
                "convenio colectivo", "despido", "contrato de trabajo", "contratacion laboral", "prestacion por desempleo", "erte",
                "prevencion de riesgos laborales", "inspeccion de trabajo", "vacaciones", "permiso retribuido", "pension", "autonomos",
                "regimen especial de trabajadores", "relaciones laborales", "teletrabajo", "registro de jornada", "smi",
                "salario minimo interprofesional", "incapacidad temporal"),
    "mercantil": ("sociedades de capital", "sociedad anonima", "sociedad limitada", "mercantil", "registro mercantil", "concursal",
                  "insolvencia", "administradores", "cuentas anuales", "auditoria de cuentas", "emprendedores", "fusion", "escision",
                  "morosidad", "operaciones comerciales", "crecimiento empresarial"),
    "fiscal": ("tributario", "tributaria", "impuesto", "iva", "irpf", "impuesto sobre sociedades", "hacienda", "agencia estatal de administracion tributaria",
               "facturacion", "verifactu", "retenciones", "modelo 303", "modelo 111", "modelo 200", "declaracion informativa", "aduanas", "recargo"),
    "administrativo": ("procedimiento administrativo", "contratos del sector publico", "contratacion publica", "regimen juridico del sector publico",
                       "subvenciones", "expropiacion", "responsabilidad patrimonial", "administracion electronica", "funcion publica"),
    "civil": ("codigo civil", "consumidores", "consumo", "arrendamientos urbanos", "vivienda", "propiedad horizontal", "hipotecario",
              "credito inmobiliario", "familia", "sucesiones"),
    "proteccion_datos": ("proteccion de datos", "datos personales", "privacidad", "agencia espanola de proteccion de datos", "ciberseguridad",
                         "servicios digitales", "inteligencia artificial"),
}
AREA_DEPARTMENTS: dict[str, tuple[str, ...]] = {
    "laboral": ("ministerio de trabajo", "ministerio de inclusion, seguridad social", "seguridad social"),
    "fiscal": ("ministerio de hacienda", "agencia estatal de administracion tributaria"),
    "mercantil": ("ministerio de economia",),
    "administrativo": ("ministerio para la transformacion digital y de la funcion publica", "ministerio de hacienda y funcion publica"),
    "proteccion_datos": ("agencia espanola de proteccion de datos",),
}
GENERAL_SECTIONS = {"1": "I · Disposiciones generales", "3": "III · Otras disposiciones", "T": "Tribunal Constitucional"}
NORMATIVE_RANKS = ("ley organica", "ley", "real decreto-ley", "real decreto legislativo", "real decreto", "orden", "resolucion")
REGION_MARKERS = {
    "Andalucía": ("andalucia",), "Aragón": ("aragon",), "Asturias": ("principado de asturias", "asturias"), "Illes Balears": ("illes balears", "islas baleares"),
    "Canarias": ("canarias",), "Cantabria": ("cantabria",), "Castilla y León": ("castilla y leon",), "Castilla-La Mancha": ("castilla-la mancha",),
    "Cataluña": ("cataluna", "catalunya"), "Comunitat Valenciana": ("comunitat valenciana", "comunidad valenciana"), "Extremadura": ("extremadura",),
    "Galicia": ("galicia",), "Comunidad de Madrid": ("comunidad de madrid",), "Región de Murcia": ("region de murcia",), "Navarra": ("navarra",),
    "País Vasco": ("pais vasco", "euskadi"), "La Rioja": ("la rioja",), "Ceuta": ("ceuta",), "Melilla": ("melilla",),
}
assert set(REGION_MARKERS) == set(REGIONS)


def norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", (text or "").lower())
    return re.sub(r"\s+", " ", "".join(char for char in text if not unicodedata.combining(char)))


def term_in(term: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def found_terms(area: str, text: str) -> list[str]:
    return [term for term in AREA_TERMS[area] if term_in(term, text)]


def item_region(department: str | None) -> str | None:
    text = norm(department)
    if not any(word in text for word in ("comunidad", "comunitat", "principado", "illes", "region de", "ciudad de", "junta", "generalitat", "xunta", "gobierno de")):
        return None
    for region, markers in REGION_MARKERS.items():
        if any(marker in text for marker in markers):
            return region
    return None


def assess_regulation(item: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """{relevance, areas, reasons:[{label, ok}]} de una disposición del BOE para el perfil jurídico."""
    areas = [area for area in profile["juridico"]["areas"] if area in LEGAL_AREAS]
    reasons: list[dict[str, Any]] = []
    section = str(item.get("section_code") or "")
    if section not in GENERAL_SECTIONS:
        return {"relevance": "baja", "areas": [], "reasons": [{"label": f"Sección {item.get('section') or section}: no es una norma de alcance general", "ok": False}]}
    if not areas:
        return {"relevance": "baja", "areas": [], "reasons": [{"label": "No hay áreas jurídicas configuradas en el perfil", "ok": False}]}

    title = norm(item.get("title"))
    context = norm(" ".join([item.get("category") or "", " ".join(item.get("subjects") or [])]))
    department = norm(item.get("department"))
    matched: dict[str, dict[str, Any]] = {}
    for area in areas:
        in_title = found_terms(area, title)
        in_context = found_terms(area, context)
        by_department = any(name in department for name in AREA_DEPARTMENTS.get(area, ()))
        if in_title or in_context or by_department:
            matched[area] = {"title": in_title, "context": in_context, "department": by_department}
    if not matched:
        configured = ", ".join(LEGAL_AREAS[area].lower() for area in areas)
        return {"relevance": "baja", "areas": [], "reasons": [{"label": f"No toca tus áreas configuradas ({configured})", "ok": False}]}

    for area, hit in matched.items():
        if hit["title"]:
            reasons.append({"label": f"Área {LEGAL_AREAS[area].lower()} configurada: el título menciona «{hit['title'][0]}»", "ok": True})
        elif hit["context"]:
            reasons.append({"label": f"Área {LEGAL_AREAS[area].lower()} configurada: el BOE la clasifica en «{hit['context'][0]}»", "ok": True})
        else:
            reasons.append({"label": f"Área {LEGAL_AREAS[area].lower()} configurada: la publica {item.get('department')}", "ok": True})
    reasons.append({"label": f"Sección {GENERAL_SECTIONS[section]}", "ok": True})

    rank = norm(item.get("rank"))
    title_rank = next((candidate for candidate in NORMATIVE_RANKS if title.startswith(candidate)), None)
    rank_name = item.get("rank") or (title_rank.capitalize() if title_rank else None)
    if rank_name:
        reasons.append({"label": f"Rango: {rank_name}", "ok": True})

    region = item_region(item.get("department"))
    own_region = profile["common"].get("region")
    if region and own_region and region != own_region:
        reasons.append({"label": f"Norma de {region} (tu perfil: {own_region})", "ok": False})
        return {"relevance": "baja", "areas": list(matched), "reasons": reasons}
    if region:
        reasons.append({"label": f"Norma autonómica de {region}", "ok": True if region == own_region else None})

    correction = title.startswith("correccion de errores") or "correccion de errores" in title[:60]
    strong = any(hit["title"] or hit["context"] for hit in matched.values())
    normative = bool(title_rank or (rank and any(rank.startswith(candidate) for candidate in NORMATIVE_RANKS)))
    if correction:
        reasons.append({"label": "Es una corrección de errores de otra disposición", "ok": None})
        relevance = "media"
    elif section == "1" and normative and strong:
        relevance = "alta"
    else:
        relevance = "media"
    return {"relevance": relevance, "areas": list(matched), "reasons": reasons}
