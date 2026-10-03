"""
Resumen con IA, validado contra el texto oficial.

La IA solo recibe candidatos que ya pasaron las reglas, y solo el texto de la disposición.
Devuelve afirmaciones con una CITA LITERAL del texto. Cada cita se busca en el texto
oficial (sin mayúsculas, tildes ni espacios de más): la afirmación cuya cita no aparece
se descarta y se cuenta. Si no queda ninguna afirmación comprobada, no hay resumen.
«Qué revisar» es una sugerencia de trabajo y se muestra como tal, no como contenido de la norma.
"""
from __future__ import annotations

import json
from typing import Any

from app.intelligence.relevance import norm

MAX_CHARS = 24_000
SYSTEM = (
    "Eres un asistente para profesionales del derecho en España. Recibes una disposición del BOE. "
    "Responde SOLO con lo que dice el texto. Cada afirmación debe incluir una cita literal y exacta "
    "del texto (copiada carácter a carácter, de 5 a 40 palabras). Si el texto no dice algo, no lo afirmes: "
    "deja el campo vacío. No inventes fechas, importes ni requisitos."
)
SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["que_cambia", "a_quien_afecta", "entrada_en_vigor", "que_revisar"],
        "properties": {
            "que_cambia": {"type": "array", "items": {"$ref": "#/$defs/claim"}},
            "a_quien_afecta": {"type": "array", "items": {"$ref": "#/$defs/claim"}},
            "entrada_en_vigor": {"type": "array", "items": {"$ref": "#/$defs/claim"}},
            "que_revisar": {"type": "array", "items": {"type": "string"}},
        },
        "$defs": {"claim": {"type": "object", "additionalProperties": False, "required": ["texto", "cita"],
                            "properties": {"texto": {"type": "string"}, "cita": {"type": "string"}}}},
    },
}
CLAIM_FIELDS = ("que_cambia", "a_quien_afecta", "entrada_en_vigor")


def quote_found(quote: str, text: str) -> bool:
    quote = norm(quote).strip(" .,;:«»\"'")
    return len(quote) >= 15 and quote in norm(text)


def validate(raw: dict[str, Any], text: str) -> dict[str, Any] | None:
    """Solo sobreviven las afirmaciones cuya cita está en el texto oficial."""
    result: dict[str, Any] = {"discarded": 0}
    kept = 0
    for field in CLAIM_FIELDS:
        claims = []
        for claim in raw.get(field) or []:
            if isinstance(claim, dict) and claim.get("texto") and quote_found(claim.get("cita") or "", text):
                claims.append({"text": claim["texto"].strip(), "quote": claim["cita"].strip()})
            else:
                result["discarded"] += 1
        result[field] = claims
        kept += len(claims)
    if not kept:
        return None
    result["que_revisar"] = [item.strip() for item in raw.get("que_revisar") or [] if isinstance(item, str) and item.strip()][:5]
    return result


def summarize(title: str, text: str, areas: list[str]) -> dict[str, Any] | None:
    """None si no hay IA, no hay texto o nada se pudo comprobar. Nunca inventa un resumen."""
    from app.agents import llm

    if not text or not llm.available():
        return None
    prompt = (f"Áreas del despacho: {', '.join(areas) or 'sin indicar'}.\n\n<titulo>{title}</titulo>\n<texto>\n{text[:MAX_CHARS]}\n</texto>\n\n"
              "Devuelve: qué cambia, a quién afecta, cuándo entra en vigor (cada afirmación con su cita literal) "
              "y hasta 5 puntos concretos que un profesional debería revisar.")
    answer, meta = llm._complete(system=SYSTEM, content=prompt, effort="low", max_tokens=3000, output_format=SCHEMA, operation="resumen_boe")
    if not answer:
        return {"engine": "reglas", "fallback": meta.get("fallback"), "meta": meta}
    try:
        validated = validate(json.loads(answer), text)
    except (json.JSONDecodeError, AttributeError):
        validated = None
    if validated is None:
        return {"engine": "reglas", "fallback": "ninguna afirmación con cita comprobable", "meta": meta}
    return {**validated, "engine": meta.get("served_by") or meta.get("model"), "meta": meta,
            "truncated": len(text) > MAX_CHARS}
