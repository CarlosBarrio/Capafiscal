"""
Capa de IA opcional (Claude).

Los agentes funcionan siempre con reglas y plantillas deterministas. Si hay
ANTHROPIC_API_KEY, algunos pasos se apoyan en Claude para leer mejor la
notificación y redactar la respuesta; el paso guarda qué motor participó.
Nunca se envía nada a la IA sin clave configurada, y ante cualquier error o
negativa se vuelve al método determinista.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"

NOTIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Qué pide o comunica la Administración, en 2-3 frases claras para un empresario.",
        },
        "requested_documents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["label", "detail"],
                "additionalProperties": False,
            },
        },
        "tax_references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "year": {"type": "string"},
                    "period": {"type": "string"},
                },
                "required": ["model", "year", "period"],
                "additionalProperties": False,
            },
        },
        "response_days": {"type": "string", "description": "Plazo de respuesta que indica el texto, o cadena vacía."},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "requested_documents", "tax_references", "response_days", "recommended_actions"],
    "additionalProperties": False,
}

SYSTEM_EXTRACT = (
    "Eres un asesor fiscal y laboral español que trabaja en una gestoría. Lees notificaciones de la "
    "Agencia Tributaria, la Seguridad Social y otras administraciones y extraes, sin inventar nada, qué "
    "piden, qué documentos hay que aportar, a qué modelos y periodos se refieren y qué conviene hacer. "
    "Si el texto no lo dice, deja el campo vacío."
)

SYSTEM_DRAFT = (
    "Eres un asesor fiscal español. Redactas escritos formales dirigidos a la Administración (contestación a "
    "requerimientos, alegaciones, contestación a diligencias). Estilo administrativo sobrio con EXPONE y "
    "SOLICITA. Usa solo los datos facilitados; si falta un dato, deja un hueco entre corchetes para que la "
    "persona lo complete. No añadas fundamentos jurídicos que no estén en los datos. Devuelve únicamente el "
    "texto del escrito."
)


def available() -> bool:
    if not settings.anthropic_api_key:
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def engine_label() -> str:
    return settings.agent_model if available() else "reglas"


def _client():
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=90.0, max_retries=2)


def _call(*, system: str, prompt: str, effort: str, max_tokens: int, output_format: dict[str, Any] | None = None) -> str | None:
    import anthropic

    output_config: dict[str, Any] = {"effort": effort}
    if output_format:
        output_config["format"] = output_format

    try:
        response = _client().beta.messages.create(
            model=settings.agent_model,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=system,
            output_config=output_config,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as error:
        logger.warning("La IA devolvió un error (%s); se usan reglas.", error.status_code)
        return None
    except anthropic.APIConnectionError:
        logger.warning("Sin conexión con la IA; se usan reglas.")
        return None

    if response.stop_reason in {"refusal", "max_tokens"}:
        logger.warning("La IA no completó la respuesta (%s); se usan reglas.", response.stop_reason)
        return None

    return next((block.text for block in response.content if block.type == "text"), None)


def extract_notification(text: str) -> dict[str, Any] | None:
    if not available() or not text.strip():
        return None
    raw = _call(
        system=SYSTEM_EXTRACT,
        prompt=f"Texto de la notificación:\n<notificacion>\n{text}\n</notificacion>",
        effort="low",
        max_tokens=4000,
        output_format={"type": "json_schema", "schema": NOTIFICATION_SCHEMA},
    )
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def draft_letter(facts: dict[str, Any], template: str) -> str | None:
    if not available():
        return None
    prompt = (
        "Mejora y completa este borrador de escrito con los datos del expediente. Mantén la estructura y "
        "los huecos entre corchetes que no puedas rellenar con los datos.\n\n"
        f"<datos>\n{json.dumps(facts, ensure_ascii=False, default=str, indent=2)}\n</datos>\n\n"
        f"<borrador>\n{template}\n</borrador>"
    )
    return _call(system=SYSTEM_DRAFT, prompt=prompt, effort="medium", max_tokens=8000)


def answer_with_sources(question: str, passages: list[dict[str, Any]]) -> str | None:
    if not available() or not passages:
        return None
    context = "\n\n".join(
        f'<fuente id="{index + 1}" titulo="{item["title"]}">\n{item["snippet"]}\n</fuente>'
        for index, item in enumerate(passages)
    )
    return _call(
        system=(
            "Respondes preguntas sobre la documentación de una empresa usando solo las fuentes dadas. "
            "Cita las fuentes como [1], [2]. Si las fuentes no contienen la respuesta, dilo."
        ),
        prompt=f"{context}\n\nPregunta: {question}",
        effort="low",
        max_tokens=2000,
    )
