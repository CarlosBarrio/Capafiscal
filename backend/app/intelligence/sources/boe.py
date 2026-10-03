"""
BOE: sumario diario y ficha de cada disposición, de la API oficial de datos abiertos.

    sumario    GET https://www.boe.es/datosabiertos/api/boe/sumario/AAAAMMDD   (Accept: application/json)
    documento  GET https://www.boe.es/diario_boe/xml.php?id=BOE-A-AAAA-NNNNN   (XML con metadatos, análisis y texto)

El sumario da título, sección, departamento, epígrafe y enlaces. La ficha añade lo que no
se debe deducir: rango, fecha de entrada en vigor, materias oficiales y el texto. La API
devuelve un objeto o una lista según haya uno o varios elementos: aquí se aceptan ambos.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from datetime import datetime
from typing import Any

SUMMARY_URL = "https://www.boe.es/datosabiertos/api/boe/sumario/{day:%Y%m%d}"
DOCUMENT_URL = "https://www.boe.es/diario_boe/xml.php?id={id}"
HTML_URL = "https://www.boe.es/diario_boe/txt.php?id={id}"
SECTIONS = {
    "1": "I. Disposiciones generales", "2A": "II-A. Nombramientos", "2B": "II-B. Oposiciones y concursos",
    "3": "III. Otras disposiciones", "4": "IV. Administración de Justicia", "5A": "V-A. Contratación del Sector Público",
    "5B": "V-B. Otros anuncios oficiales", "5C": "V-C. Anuncios particulares", "T": "Tribunal Constitucional",
}


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def text_of(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("texto") or value.get("#text")
    return value or None


def parse_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:8], "%Y%m%d").date()
    except ValueError:
        return None


def parse_summary(payload: bytes | str | dict[str, Any], day: date) -> list[dict[str, Any]]:
    """Las disposiciones de un sumario, planas: una por identificador."""
    data = payload if isinstance(payload, dict) else json.loads(payload)
    summary = (data.get("data") or {}).get("sumario") or {}
    published = parse_day((summary.get("metadatos") or {}).get("fecha_publicacion")) or day
    items: list[dict[str, Any]] = []

    def add(raw: dict[str, Any], section: dict[str, Any], department: dict[str, Any], category: str | None) -> None:
        identifier = raw.get("identificador")
        if not identifier:
            return
        code = str(section.get("codigo") or "")
        items.append({
            "external_id": identifier, "title": (raw.get("titulo") or "").strip(),
            "section_code": code, "section": section.get("nombre") or SECTIONS.get(code),
            "department": department.get("nombre"), "category": category,
            "publication_date": published, "html_url": raw.get("url_html") or HTML_URL.format(id=identifier),
            "pdf_url": text_of(raw.get("url_pdf")), "xml_url": raw.get("url_xml") or DOCUMENT_URL.format(id=identifier),
            "raw": raw,
        })

    for diary in as_list(summary.get("diario")):
        for section in as_list(diary.get("seccion")):
            for department in as_list(section.get("departamento")):
                for raw in as_list(department.get("item")):
                    add(raw, section, department, None)
                for heading in as_list(department.get("epigrafe")):
                    for raw in as_list(heading.get("item")):
                        add(raw, section, department, heading.get("nombre"))
    return items


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_document(payload: bytes | str) -> dict[str, Any]:
    """Ficha oficial de una disposición: rango, fechas, materias y texto."""
    root = ET.fromstring(payload)
    meta = root.find("metadatos")
    analysis = root.find("analisis")
    body = root.find("texto")

    def field(name: str) -> str | None:
        node = meta.find(name) if meta is not None else None
        return clean(node.text) if node is not None and node.text else None

    paragraphs = [clean("".join(node.itertext())) for node in (body.iter() if body is not None else []) if node.tag == "p"]
    return {
        "rank": field("rango"),
        "department": field("departamento"),
        "provision_date": parse_day(field("fecha_disposicion")),
        "publication_date": parse_day(field("fecha_publicacion")),
        "effective_date": parse_day(field("fecha_vigencia")),
        "subjects": [clean(node.text) for node in (analysis.iter("materia") if analysis is not None else []) if node.text],
        "notes": [clean(node.text) for node in (analysis.iter("nota") if analysis is not None else []) if node.text],
        "text": "\n".join(item for item in paragraphs if item),
    }
