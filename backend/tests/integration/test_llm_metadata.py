"""Coste y trazabilidad de TODAS las llamadas a la IA (cliente de Anthropic simulado: no se llama a la API).

Cada llamada devuelve los mismos metadatos: operación, identificador, modelo pedido y servido, tokens de
entrada/salida/total, coste, duración y resultado. Lo que la API no devuelve queda en None con el motivo
(cost_note): un coste desconocido no es 0.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from tests.integration.test_failure_modes import anthropic_errors
from tests.integration.test_failure_modes import failing_client

INVOICE_ANSWER = {"is_invoice": True, "document_type": "factura", "supplier_name": None, "supplier_tax_id": None, "customer_name": None,
                  "customer_tax_id": None, "invoice_number": None, "invoice_date": None, "due_date": None, "subtotal": None, "tax_total": None,
                  "withholding_total": None, "total": None, "tax_rate": None, "concept": None, "category": None}
NOTIFICATION_ANSWER = {"summary": "Pide los libros.", "requested_documents": [], "tax_references": [], "response_days": "", "recommended_actions": []}
BOE_ANSWER = {"que_cambia": [{"texto": "Cambia algo", "cita": "texto oficial de la disposición de prueba con varias palabras"}],
              "a_quien_afecta": [], "entrada_en_vigor": [], "que_revisar": []}


def fake_client(text: str, *, usage=(1200, 300), model=None, stop="end_turn", calls=None):
    """Cliente simulado. Por defecto responde con el modelo configurado (AGENT_MODEL), sin escribir ningún identificador."""
    from app.config import settings

    model = model or settings.agent_model

    def create(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return SimpleNamespace(stop_reason=stop, model=model, content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]) if usage else None)

    return lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))


@pytest.fixture()
def ai(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "clave-de-prueba")
    return monkeypatch


def operations():
    from app.agents import llm
    from app.intelligence.summarizer import summarize

    return {
        "factura": (json.dumps(INVOICE_ANSWER), lambda: llm.extract_invoice(text="FACTURA 1\nTotal 121,00")),
        "notificacion": (json.dumps(NOTIFICATION_ANSWER), lambda: llm.extract_notification("Requerimiento de la AEAT")),
        "escrito": ("EXPONE ... SOLICITA ...", lambda: llm.draft_letter({"empresa": "Simulada"}, "Borrador")),
        "respuesta": ("Según [1]…", lambda: llm.answer_with_sources("¿Qué pasó?", [{"title": "Doc", "snippet": "texto"}])),
        "resumen_boe": (json.dumps(BOE_ANSWER), lambda: summarize("Título", "Artículo 1. Texto oficial de la disposición de prueba con varias palabras.", ["laboral"])),
    }


def meta_of(operation, value):
    return value["meta"] if operation == "resumen_boe" else value[1]


@pytest.mark.parametrize("operation", ["factura", "notificacion", "escrito", "respuesta", "resumen_boe"])
def test_every_call_reports_model_tokens_cost_duration_and_outcome(ai, operation):
    from app.agents import llm
    from app.config import settings

    text, call = operations()[operation]
    ai.setattr(llm, "_client", fake_client(text))
    meta = meta_of(operation, call())
    assert meta["operation"] == operation and len(meta["operation_id"]) == 32
    configured = settings.agent_model
    assert meta["model"] == configured and meta["served_by"] == configured
    assert (meta["input_tokens"], meta["output_tokens"], meta["total_tokens"]) == (1200, 300, 1500)
    assert meta["cost_usd"] == llm.estimate_cost(configured, 1200, 300) and meta["cost_note"] is None
    assert isinstance(meta["ms"], int) and meta["outcome"] == "ok" and meta["fallback"] is None


def test_operation_ids_are_unique(ai):
    from app.agents import llm

    ai.setattr(llm, "_client", fake_client("EXPONE"))
    ids = {llm.draft_letter({}, "Borrador")[1]["operation_id"] for _ in range(5)}
    assert len(ids) == 5


def test_missing_usage_and_unknown_price_are_null_not_zero(ai):
    from app.agents import llm

    ai.setattr(llm, "_client", fake_client("EXPONE", usage=None))
    meta = llm.draft_letter({}, "Borrador")[1]
    assert meta["input_tokens"] is None and meta["total_tokens"] is None and meta["cost_usd"] is None
    assert "no devolvió el uso" in meta["cost_note"]

    ai.setattr(llm, "_client", fake_client("EXPONE", model="modelo-sin-precio"))
    meta = llm.draft_letter({}, "Borrador")[1]
    assert meta["total_tokens"] == 1500 and meta["cost_usd"] is None and "sin precio" in meta["cost_note"]


@pytest.mark.parametrize("index", range(6))
def test_api_errors_are_errors_with_unknown_usage(ai, index):
    from app.agents import llm

    error, reason = anthropic_errors()[index]
    ai.setattr(llm, "_client", failing_client(error))
    for operation, (_text, call) in operations().items():
        meta = meta_of(operation, call())
        assert meta["outcome"] == "error" and meta["fallback"] == reason, operation
        assert meta["total_tokens"] is None and meta["cost_usd"] is None and meta["cost_note"], operation


def test_refusal_is_a_fallback_and_skipped_calls_cost_nothing(ai):
    from app.agents import llm
    from app.config import settings

    ai.setattr(llm, "_client", fake_client("", stop="refusal"))
    text, meta = llm.draft_letter({}, "Borrador")
    assert text is None and meta["outcome"] == "fallback" and meta["fallback"] == "refusal"

    ai.setattr(settings, "anthropic_api_key", "")
    text, meta = llm.extract_notification("Requerimiento")
    assert text is None and meta["outcome"] == "skipped" and meta["cost_usd"] == 0.0 and meta["total_tokens"] == 0


def test_each_call_leaves_one_log_line_without_content(ai, caplog):
    from app.agents import llm

    ai.setattr(llm, "_client", fake_client("EXPONE"))
    with caplog.at_level(logging.INFO, logger="capafiscal.llm"):
        llm.answer_with_sources("PREGUNTA-SECRETA", [{"title": "Doc", "snippet": "CONTENIDO-SECRETO"}])
    lines = [record for record in caplog.records if record.name == "capafiscal.llm"]
    assert len(lines) == 1 and lines[0].fields["operation"] == "respuesta" and lines[0].fields["total_tokens"] == 1500
    assert "SECRETO" not in lines[0].getMessage() and "SECRETO" not in json.dumps(lines[0].fields)


def test_agent_steps_and_document_audit_keep_the_call_metadata(client, ai):
    """El Fiscal y el Gestor guardan los metadatos en AgentStep.output; la lectura de facturas, en la auditoría."""
    from app.agents import llm
    from app.config import settings
    from app.database import SessionLocal
    from app.models import AgentStep
    from tests.agents.test_agents import REQUERIMIENTO
    from tests.agents.test_agents import open_case
    from tests.agents.test_agents import setup_company
    from tests.agents.test_agents import upload_text

    def create(**kwargs):
        text = json.dumps(NOTIFICATION_ANSWER) if kwargs["output_config"].get("format") else "ESCRITO\nEXPONE\nSOLICITA"
        return SimpleNamespace(stop_reason="end_turn", model=settings.agent_model, content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=500, output_tokens=50))

    ai.setattr(llm, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))))
    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    open_case(client)
    with SessionLocal() as database:
        steps = {step.agent: step.output for step in database.query(AgentStep).all()}
    assert steps["fiscal"]["llm"][0]["operation"] == "notificacion" and steps["fiscal"]["llm"][0]["total_tokens"] == 550
    assert steps["gestor"]["llm"][0]["operation"] == "escrito" and steps["gestor"]["llm"][0]["cost_usd"] is not None


def test_memory_answer_returns_the_call_metadata(client, ai):
    from app.agents import llm

    ai.setattr(llm, "_client", fake_client("Según [1]…"))
    ai.setattr("app.agents.memory.search", lambda database, question, limit=5: [{"title": "Doc", "snippet": "texto", "date": None}])
    answer = client.post("/api/memory/ask", json={"question": "¿Qué pasó?"}).json()
    assert answer["llm"]["operation"] == "respuesta" and answer["llm"]["total_tokens"] == 1500


def test_invoice_interpretation_audit_keeps_tokens_cost_and_operation_id(client, ai, sample_pdfs):
    import app.interpretation as interpretation
    from app.agents import llm
    from app.database import SessionLocal
    from app.models import AuditEvent
    from app.models import Document
    from app.invoice_service import process_document
    from tests.integration.test_llm_transactions import make_document

    ai.setattr(llm, "_client", fake_client(json.dumps(INVOICE_ANSWER)))
    ai.setattr(interpretation, "needs_help", lambda *args, **kwargs: ["motivo de prueba"])
    ai.setattr("app.routing.allowed_reasons", lambda reasons: True)
    document_id, path = make_document(client, sample_pdfs)
    with SessionLocal() as database:
        process_document(database, document=database.get(Document, document_id), file_path=path, actor="prueba")
    with SessionLocal() as database:
        event = database.query(AuditEvent).filter(AuditEvent.action == "document.interpretation", AuditEvent.entity_id == str(document_id)).one()
    data = event.event_data
    assert data["total_tokens"] == 1500 and data["cost_usd"] is not None and len(data["operation_id"]) == 32 and data["outcome"] == "ok"
