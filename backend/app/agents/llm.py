"""
Capa de IA opcional (Claude).

Los agentes funcionan siempre con reglas y plantillas deterministas. Si hay
ANTHROPIC_API_KEY, algunos pasos se apoyan en Claude para leer mejor la
notificación y redactar la respuesta; el paso guarda qué motor participó.
Nunca se envía nada a la IA sin clave configurada, y ante cualquier error o
negativa se vuelve al método determinista.

Toda llamada devuelve los metadatos de call_meta (operación, id, modelo, tokens,
coste, duración y resultado) y deja una línea en el registro «capafiscal.llm».
Fase 1 (no hecho aún): el transporte (_client/_complete) pasará a un
ModelProvider en app/ai/providers/; este módulo quedará como capa de tareas
(prompts y esquemas) y call_meta seguirá siendo el contrato de metadatos.
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


LLM_LOG = logging.getLogger("capafiscal.llm")


def call_meta(operation: str, model: str | None, *, outcome: str, fallback: str | None = None) -> dict[str, Any]:
    """Metadatos comunes de una llamada a la IA (o de una que no llegó a hacerse).

        operation      para qué: factura, notificacion, escrito, respuesta, resumen_boe
        operation_id   identificador único de la llamada (para cruzarla con auditoría y registros)
        model          el modelo pedido; served_by, el que respondió
        input_tokens / output_tokens / total_tokens   los que devuelve la API; None si no los devuelve
        cost_usd       calculado con PRICES_PER_MTOK; None si no hay tokens o no hay precio para el modelo
                       (cost_note dice por qué). No se inventa: un coste desconocido no es 0.
        ms             duración de la llamada
        outcome        ok · fallback (respuesta no utilizable: negativa o truncada) · error (la API falló)
                       · skipped (no se llamó: sin clave o sin contenido; no cuesta nada)
    """
    import uuid

    skipped = outcome == "skipped"
    return {"operation": operation, "operation_id": uuid.uuid4().hex, "model": model or settings.agent_model, "served_by": None,
            "input_tokens": 0 if skipped else None, "output_tokens": 0 if skipped else None, "total_tokens": 0 if skipped else None,
            "cost_usd": 0.0 if skipped else None, "cost_note": None if skipped else "la API no devolvió el uso de tokens",
            "ms": 0 if skipped else None, "outcome": outcome, "fallback": fallback}


def log_call(meta: dict[str, Any]) -> None:
    """Una línea por llamada, sin contenido (ni prompt ni respuesta): para medir coste y fallos."""
    fields = {key: meta.get(key) for key in ("operation", "operation_id", "model", "served_by", "input_tokens", "output_tokens",
                                              "total_tokens", "cost_usd", "ms", "outcome", "fallback")}
    level = logging.WARNING if meta.get("outcome") == "error" else logging.INFO
    LLM_LOG.log(level, "IA · %s · %s · %s ms · %s tokens · %s $", fields["operation"], fields["outcome"], fields["ms"],
                fields["total_tokens"], fields["cost_usd"], extra={"fields": fields})


def _complete(
    *,
    system: str,
    content: str | list[dict[str, Any]],
    effort: str,
    max_tokens: int,
    output_format: dict[str, Any] | None = None,
    model: str | None = None,
    operation: str = "otra",
) -> tuple[str | None, dict[str, Any]]:
    """Una llamada a Claude. Devuelve (texto o None, metadatos de call_meta)."""
    import time

    import anthropic

    model = model or settings.agent_model
    output_config: dict[str, Any] = {"effort": effort}
    if output_format:
        output_config["format"] = output_format
    meta = call_meta(operation, model, outcome="error")
    started = time.perf_counter()

    def failed(reason: str) -> tuple[None, dict[str, Any]]:
        # Sin respuesta no hay uso de tokens que leer: tokens y coste quedan en None (desconocidos).
        meta.update(fallback=reason, outcome="error", ms=int((time.perf_counter() - started) * 1000),
                    cost_note="la llamada falló antes de recibir respuesta: la API no informa del uso")
        log_call(meta)
        return None, meta

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
        return failed(reason)
    except anthropic.APITimeoutError:
        logger.warning("La IA no respondió a tiempo; se usan reglas.")
        return failed("tiempo agotado")
    except anthropic.APIConnectionError:
        logger.warning("Sin conexión con la IA; se usan reglas.")
        return failed("sin conexión")
    except anthropic.AnthropicError as error:  # cualquier otro fallo del cliente: nunca rompe el proceso
        logger.warning("Fallo del cliente de IA (%s); se usan reglas.", type(error).__name__)
        return failed(f"fallo del cliente ({type(error).__name__})")

    meta["ms"] = int((time.perf_counter() - started) * 1000)
    usage = getattr(response, "usage", None)
    if usage is not None:
        input_tokens, output_tokens = getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)
        meta["input_tokens"] = int(input_tokens) if input_tokens is not None else None
        meta["output_tokens"] = int(output_tokens) if output_tokens is not None else None
        if meta["input_tokens"] is not None and meta["output_tokens"] is not None:
            meta["total_tokens"] = meta["input_tokens"] + meta["output_tokens"]
    served_by = getattr(response, "model", None) or model
    meta["served_by"] = served_by
    if meta["total_tokens"] is None:
        meta["cost_usd"], meta["cost_note"] = None, "la API no devolvió el uso de tokens"
    else:
        meta["cost_usd"] = estimate_cost(served_by, meta["input_tokens"], meta["output_tokens"])
        meta["cost_note"] = None if meta["cost_usd"] is not None else f"sin precio para el modelo {served_by} en PRICES_PER_MTOK"

    if response.stop_reason in {"refusal", "max_tokens"}:
        logger.warning("La IA no completó la respuesta (%s); se usan reglas.", response.stop_reason)
        meta.update(fallback=response.stop_reason, outcome="fallback")
        log_call(meta)
        return None, meta

    meta.update(outcome="ok", fallback=None)
    log_call(meta)
    return next((block.text for block in response.content if block.type == "text"), None), meta


def _call(*, system: str, prompt: str, effort: str, max_tokens: int, output_format: dict[str, Any] | None = None,
          operation: str = "otra") -> tuple[str | None, dict[str, Any]]:
    """Llamada de texto: (texto o None, metadatos). Los metadatos ya no se pierden."""
    return _complete(system=system, content=prompt, effort=effort, max_tokens=max_tokens, output_format=output_format, operation=operation)


INVOICE_FIELDS = (
    "is_invoice", "document_type",
    "supplier_name", "supplier_tax_id", "customer_name", "customer_tax_id", "invoice_number",
    "invoice_date", "due_date", "subtotal", "tax_total", "withholding_total", "total", "tax_rate", "concept", "category",
)
# Las mismas categorías que las reglas (evaluation.core.DOCUMENT_TYPES; extractor.non_invoice_title).
DOCUMENT_TYPES = ("factura", "presupuesto", "albaran", "proforma", "pedido", "otro")
INVOICE_AMOUNTS = ("subtotal", "tax_total", "withholding_total", "total", "tax_rate")


def expense_categories() -> list[str]:
    from app.extractor import DEFAULT_CATEGORY
    from app.extractor import EXPENSE_CATEGORIES

    return [name for name, _account, _keywords, _suppliers in EXPENSE_CATEGORIES] + [DEFAULT_CATEGORY]


def _nullable(kind: str, description: str, **extra: Any) -> dict[str, Any]:
    return {"anyOf": [{"type": kind, **extra}, {"type": "null"}], "description": description}


INVOICE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_invoice": {"type": "boolean", "description": "true solo si el documento es una factura (también simplificada, rectificativa o albarán-factura)."},
        "document_type": {"type": "string", "enum": list(DOCUMENT_TYPES), "description": "Tipo de documento según su título: una proforma, un presupuesto, un albarán o un pedido no son factura."},
        "supplier_name": _nullable("string", "Razón social o nombre de quien EMITE el documento."),
        "supplier_tax_id": _nullable("string", "NIF/CIF del emisor, sin guiones ni espacios."),
        "customer_name": _nullable("string", "Nombre de quien RECIBE el documento."),
        "customer_tax_id": _nullable("string", "NIF/CIF del destinatario, sin guiones ni espacios."),
        "invoice_number": _nullable("string", "Número del documento tal como aparece (serie incluida si va unida)."),
        "invoice_date": _nullable("string", "Fecha de expedición.", format="date"),
        "due_date": _nullable("string", "Fecha de vencimiento.", format="date"),
        "subtotal": _nullable("number", "Base imponible total."),
        "tax_total": _nullable("number", "Cuota total de IVA."),
        "withholding_total": _nullable("number", "Retención de IRPF, en positivo."),
        "total": _nullable("number", "Total del documento."),
        "tax_rate": _nullable("number", "Tipo de IVA principal (21, 10, 4, 0)."),
        "concept": _nullable("string", "Concepto breve (máx. 12 palabras)."),
        "category": _nullable("string", "Categoría contable del gasto.", enum=expense_categories()),
    },
    "required": list(INVOICE_FIELDS),
    "additionalProperties": False,
}

SYSTEM_INVOICE = (
    "Lees documentos comerciales españoles y extraes sus datos sin inventar nada. Si un dato no aparece en el "
    "documento, devuelve null. Primero decide el tipo por el título del documento, no por palabras sueltas: "
    "factura (también simplificada, rectificativa o albarán-factura), presupuesto, albaran, proforma (una «factura "
    "proforma» es proforma), pedido (también orden o nota de pedido) u otro. is_invoice es true solo si es factura; "
    "si no lo es, extrae igualmente lo que aparezca. El emisor es quien vende o presta el servicio (suele figurar en "
    "la cabecera, el pie o el registro mercantil); el destinatario es el cliente. Los importes son números, sin símbolo."
)


def schema_errors(data: Any, schema: dict[str, Any] | None = None, path: str = "") -> list[str]:
    """Comprueba una respuesta contra el subconjunto de JSON Schema que usan las salidas estructuradas
    (type, anyOf, enum, required, additionalProperties: false). Lista vacía = válida."""
    schema = INVOICE_SCHEMA if schema is None else schema
    if "anyOf" in schema:
        return [] if any(not schema_errors(data, option, path) for option in schema["anyOf"]) else [f"{path or 'raíz'}: no encaja en ninguna opción"]
    kinds = {"object": dict, "string": str, "boolean": bool, "null": type(None), "array": list}
    kind = schema.get("type")
    if kind in ("number", "integer"):
        valid = isinstance(data, (int, float)) and not isinstance(data, bool) and (kind == "number" or isinstance(data, int))
    else:
        valid = kind is None or isinstance(data, kinds[kind])
    if not valid:
        return [f"{path or 'raíz'}: se esperaba {kind}, llegó {type(data).__name__}"]
    if "enum" in schema and data not in schema["enum"]:
        return [f"{path or 'raíz'}: {data!r} no es un valor permitido"]
    errors: list[str] = []
    if kind == "object":
        properties = schema.get("properties", {})
        errors += [f"{path}{name}: falta" for name in schema.get("required", []) if name not in data]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}{name}: campo no previsto" for name in data if name not in properties]
        for name, value in data.items():
            if name in properties:
                errors += schema_errors(value, properties[name], f"{path}{name}.")
    return [error.replace(".:", ":") for error in errors]


def normalize_invoice(data: dict[str, Any]) -> dict[str, Any]:
    """La salida de Claude con la forma que esperan las reglas: textos e importes como cadena ("" si falta).

    is_invoice queda booleano (o None si no llegó bien) y document_type, uno de DOCUMENT_TYPES (o None).
    """
    from decimal import Decimal

    out: dict[str, Any] = {}
    for name in INVOICE_FIELDS:
        value = data.get(name)
        if name == "is_invoice":
            out[name] = value if isinstance(value, bool) else None
        elif name == "document_type":
            out[name] = value if value in DOCUMENT_TYPES else None
        elif value is None:
            out[name] = ""
        elif name in INVOICE_AMOUNTS and isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = format(Decimal(str(value)).normalize(), "f")
        else:
            out[name] = str(value)
    return out


def extract_invoice(*, pdf_bytes: bytes | None = None, text: str | None = None, company: str | None = None, model: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Claude como capacidad de interpretación: lee la factura (PDF o texto) y devuelve campos.

    Las reglas validan después cada valor; aquí no se decide nada.
    """
    import base64

    if not available():
        return None, call_meta("factura", model, outcome="skipped", fallback="sin ANTHROPIC_API_KEY")
    hint = f"La empresa que usa el programa es {company}. " if company else ""
    instruction = {"type": "text", "text": hint + "Clasifica este documento y extrae sus datos."}
    if pdf_bytes:
        content: list[dict[str, Any]] = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()}},
            instruction,
        ]
    elif text and text.strip():
        content = [{"type": "text", "text": f"<factura>\n{text}\n</factura>"}, instruction]
    else:
        return None, call_meta("factura", model, outcome="skipped", fallback="documento vacío")

    raw, meta = _complete(
        system=SYSTEM_INVOICE, content=content, effort="low", max_tokens=4000,
        output_format={"type": "json_schema", "schema": INVOICE_SCHEMA}, model=model, operation="factura",
    )
    if not raw:
        return None, meta
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, {**meta, "fallback": "JSON inválido", "outcome": "fallback"}
    if not isinstance(data, dict):
        return None, {**meta, "fallback": "JSON inválido", "outcome": "fallback"}
    errors = schema_errors(data)
    if errors:  # con salidas estructuradas no debería pasar; si pasa, se anota y no se usa lo que no encaja
        meta = {**meta, "schema_errors": errors}
    return normalize_invoice(data), meta


def extract_notification(text: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """(datos o None, metadatos de la llamada)."""
    if not available() or not text.strip():
        return None, call_meta("notificacion", None, outcome="skipped", fallback="sin ANTHROPIC_API_KEY" if not available() else "texto vacío")
    raw, meta = _call(
        system=SYSTEM_EXTRACT,
        prompt=f"Texto de la notificación:\n<notificacion>\n{text}\n</notificacion>",
        effort="low",
        max_tokens=4000,
        output_format={"type": "json_schema", "schema": NOTIFICATION_SCHEMA},
        operation="notificacion",
    )
    if not raw:
        return None, meta
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, {**meta, "fallback": "JSON inválido", "outcome": "fallback"}
    if not isinstance(data, dict):
        return None, {**meta, "fallback": "JSON inválido", "outcome": "fallback"}
    return data, meta


def draft_letter(facts: dict[str, Any], template: str) -> tuple[str | None, dict[str, Any]]:
    """(escrito o None, metadatos de la llamada)."""
    if not available():
        return None, call_meta("escrito", None, outcome="skipped", fallback="sin ANTHROPIC_API_KEY")
    prompt = (
        "Mejora y completa este borrador de escrito con los datos del expediente. Mantén la estructura y "
        "los huecos entre corchetes que no puedas rellenar con los datos.\n\n"
        f"<datos>\n{json.dumps(facts, ensure_ascii=False, default=str, indent=2)}\n</datos>\n\n"
        f"<borrador>\n{template}\n</borrador>"
    )
    return _call(system=SYSTEM_DRAFT, prompt=prompt, effort="medium", max_tokens=8000, operation="escrito")


def answer_with_sources(question: str, passages: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    """(respuesta o None, metadatos de la llamada)."""
    if not available() or not passages:
        return None, call_meta("respuesta", None, outcome="skipped", fallback="sin ANTHROPIC_API_KEY" if not available() else "sin fuentes")
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
        operation="respuesta",
    )
