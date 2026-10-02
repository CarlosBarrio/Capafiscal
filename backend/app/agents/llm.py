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


# Precio por millón de tokens (entrada, salida) en USD, para medir el coste real.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES_PER_MTOK.get(model)
    if price is None:
        return None
    return round(input_tokens / 1_000_000 * price[0] + output_tokens / 1_000_000 * price[1], 6)


# Motivo legible del fallback. Los reintentos (2, con espera) ya los hace el cliente antes de llegar aquí.
STATUS_REASONS = {401: "clave no válida (401)", 403: "sin permiso (403)", 429: "límite de peticiones (429)",
                  529: "servicio saturado (529)"}


def _complete(
    *,
    system: str,
    content: str | list[dict[str, Any]],
    effort: str,
    max_tokens: int,
    output_format: dict[str, Any] | None = None,
    model: str | None = None,
) -> tuple[str | None, dict[str, Any]]:
    """Una llamada a Claude. Devuelve (texto o None, metadatos: modelo, tokens, coste, motivo del fallo)."""
    import time

    import anthropic

    model = model or settings.agent_model
    output_config: dict[str, Any] = {"effort": effort}
    if output_format:
        output_config["format"] = output_format
    meta: dict[str, Any] = {"model": model, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "fallback": None}
    started = time.perf_counter()

    try:
        response = _client().beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            betas=[FALLBACK_BETA],
            fallbacks="default",
            system=system,
            output_config=output_config,
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIStatusError as error:
        reason = STATUS_REASONS.get(error.status_code, f"error {error.status_code}")
        logger.warning("La IA devolvió un error (%s); se usan reglas.", reason)
        meta.update(fallback=reason, ms=int((time.perf_counter() - started) * 1000))
        return None, meta
    except anthropic.APITimeoutError:
        logger.warning("La IA no respondió a tiempo; se usan reglas.")
        meta.update(fallback="tiempo agotado", ms=int((time.perf_counter() - started) * 1000))
        return None, meta
    except anthropic.APIConnectionError:
        logger.warning("Sin conexión con la IA; se usan reglas.")
        meta.update(fallback="sin conexión", ms=int((time.perf_counter() - started) * 1000))
        return None, meta
    except anthropic.AnthropicError as error:  # cualquier otro fallo del cliente: nunca rompe el proceso
        logger.warning("Fallo del cliente de IA (%s); se usan reglas.", type(error).__name__)
        meta.update(fallback=f"fallo del cliente ({type(error).__name__})", ms=int((time.perf_counter() - started) * 1000))
        return None, meta

    meta["ms"] = int((time.perf_counter() - started) * 1000)
    usage = getattr(response, "usage", None)
    if usage is not None:
        meta["input_tokens"] = int(getattr(usage, "input_tokens", 0) or 0)
        meta["output_tokens"] = int(getattr(usage, "output_tokens", 0) or 0)
    served_by = getattr(response, "model", None) or model
    meta["served_by"] = served_by
    meta["cost_usd"] = estimate_cost(served_by, meta["input_tokens"], meta["output_tokens"])

    if response.stop_reason in {"refusal", "max_tokens"}:
        logger.warning("La IA no completó la respuesta (%s); se usan reglas.", response.stop_reason)
        meta["fallback"] = response.stop_reason
        return None, meta

    return next((block.text for block in response.content if block.type == "text"), None), meta


def _call(*, system: str, prompt: str, effort: str, max_tokens: int, output_format: dict[str, Any] | None = None) -> str | None:
    text, _meta = _complete(system=system, content=prompt, effort=effort, max_tokens=max_tokens, output_format=output_format)
    return text


INVOICE_FIELDS = (
    "supplier_name", "supplier_tax_id", "customer_name", "customer_tax_id", "invoice_number",
    "invoice_date", "due_date", "subtotal", "tax_total", "withholding_total", "total", "tax_rate", "concept", "category",
)


def expense_categories() -> list[str]:
    from app.extractor import DEFAULT_CATEGORY
    from app.extractor import EXPENSE_CATEGORIES

    return [name for name, _account, _keywords, _suppliers in EXPENSE_CATEGORIES] + [DEFAULT_CATEGORY]

INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "supplier_name": {"type": "string", "description": "Razón social o nombre de quien EMITE la factura."},
        "supplier_tax_id": {"type": "string", "description": "NIF/CIF del emisor, sin guiones ni espacios."},
        "customer_name": {"type": "string", "description": "Nombre de quien RECIBE la factura."},
        "customer_tax_id": {"type": "string", "description": "NIF/CIF del destinatario, sin guiones ni espacios."},
        "invoice_number": {"type": "string", "description": "Número de factura tal como aparece (serie incluida si va unida)."},
        "invoice_date": {"type": "string", "description": "Fecha de expedición en formato AAAA-MM-DD."},
        "due_date": {"type": "string", "description": "Fecha de vencimiento AAAA-MM-DD, o cadena vacía."},
        "subtotal": {"type": "string", "description": "Base imponible total, con punto decimal (1234.56)."},
        "tax_total": {"type": "string", "description": "Cuota total de IVA, con punto decimal."},
        "withholding_total": {"type": "string", "description": "Retención de IRPF (positiva), o cadena vacía."},
        "total": {"type": "string", "description": "Total de la factura, con punto decimal."},
        "tax_rate": {"type": "string", "description": "Tipo de IVA principal (21, 10, 4, 0), o cadena vacía."},
        "concept": {"type": "string", "description": "Concepto breve (máx. 12 palabras)."},
        "category": {"type": "string", "enum": expense_categories(), "description": "Categoría contable del gasto."},
    },
    "required": list(INVOICE_FIELDS),
    "additionalProperties": False,
}

SYSTEM_INVOICE = (
    "Lees facturas españolas y extraes sus datos sin inventar nada. Si un dato no aparece en el documento, "
    "devuelve cadena vacía. El emisor es quien vende o presta el servicio (suele figurar en la cabecera, el pie "
    "o el registro mercantil); el destinatario es el cliente. Los importes van con punto decimal y sin símbolo."
)


def extract_invoice(*, pdf_bytes: bytes | None = None, text: str | None = None, company: str | None = None, model: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Claude como capacidad de interpretación: lee la factura (PDF o texto) y devuelve campos.

    Las reglas validan después cada valor; aquí no se decide nada.
    """
    import base64

    meta: dict[str, Any] = {"model": model or settings.agent_model, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
    if not available():
        return None, {**meta, "fallback": "sin ANTHROPIC_API_KEY"}
    hint = f"La empresa que usa el programa es {company}. " if company else ""
    instruction = {"type": "text", "text": hint + "Extrae los datos de esta factura."}
    if pdf_bytes:
        content: list[dict[str, Any]] = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
            instruction,
        ]
    elif text and text.strip():
        content = [{"type": "text", "text": f"<factura>\n{text}\n</factura>"}, instruction]
    else:
        return None, {**meta, "fallback": "documento vacío"}

    raw, meta = _complete(
        system=SYSTEM_INVOICE, content=content, effort="low", max_tokens=4000,
        output_format={"type": "json_schema", "schema": INVOICE_SCHEMA}, model=model,
    )
    if not raw:
        return None, meta
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, {**meta, "fallback": "JSON inválido"}
    if not isinstance(data, dict):
        return None, {**meta, "fallback": "JSON inválido"}
    return data, meta


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
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


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
