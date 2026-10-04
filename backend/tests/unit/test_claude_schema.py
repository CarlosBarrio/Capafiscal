"""Esquema de salida de Claude para leer documentos: estricto, validable y con el tipo de documento de las reglas.

No llama a la API: un cliente simulado devuelve la respuesta y se comprueba qué se envía y qué se entrega.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

UNSUPPORTED = {"minimum", "maximum", "multipleOf", "minLength", "maxLength", "pattern", "minItems", "maxItems"}
NULLABLE_TEXT = ("supplier_name", "supplier_tax_id", "customer_name", "customer_tax_id", "invoice_number", "invoice_date", "due_date", "concept", "category")
NUMBERS = ("subtotal", "tax_total", "withholding_total", "total", "tax_rate")


def valid_answer(**changes):
    answer = {"is_invoice": True, "document_type": "factura", "supplier_name": "TALLERES EJEMPLO S.L.", "supplier_tax_id": "B00001016",
              "customer_name": None, "customer_tax_id": None, "invoice_number": "F-12", "invoice_date": "2026-09-01", "due_date": None,
              "subtotal": 100, "tax_total": 21.0, "withholding_total": None, "total": 121.5, "tax_rate": 21, "concept": None, "category": None}
    return {**answer, **changes}


def walk(schema):
    yield schema
    for value in schema.values():
        if isinstance(value, dict):
            yield from walk(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from walk(item)


def test_schema_is_strict_and_uses_only_supported_keywords():
    from app.agents.llm import INVOICE_FIELDS
    from app.agents.llm import INVOICE_SCHEMA

    assert INVOICE_SCHEMA["additionalProperties"] is False
    assert set(INVOICE_SCHEMA["required"]) == set(INVOICE_SCHEMA["properties"]) == set(INVOICE_FIELDS)
    for node in walk(INVOICE_SCHEMA):
        assert not UNSUPPORTED & set(node), node
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False


def test_types_document_type_and_is_invoice():
    from app.agents.llm import INVOICE_SCHEMA
    from evaluation.core import DOCUMENT_TYPES

    properties = INVOICE_SCHEMA["properties"]
    assert properties["is_invoice"]["type"] == "boolean"
    assert properties["document_type"]["type"] == "string"
    # Exactamente las categorías de las reglas, sin ninguna nueva
    assert set(properties["document_type"]["enum"]) == set(DOCUMENT_TYPES) == {"factura", "presupuesto", "albaran", "proforma", "pedido", "nomina", "otro"}
    for name in NULLABLE_TEXT:
        assert [option["type"] for option in properties[name]["anyOf"]] == ["string", "null"], name
    for name in NUMBERS:
        assert [option["type"] for option in properties[name]["anyOf"]] == ["number", "null"], name


@pytest.mark.parametrize(("change", "fragment"), [
    ({"is_invoice": "sí"}, "is_invoice"),
    ({"document_type": "nota de entrega"}, "document_type"),
    ({"total": "121,50"}, "total"),
    ({"subtotal": True}, "subtotal"),
    ({"extra": 1}, "campo no previsto"),
])
def test_validator_rejects_wrong_types_values_and_extra_fields(change, fragment):
    from app.agents.llm import schema_errors

    assert schema_errors(valid_answer()) == []
    errors = schema_errors(valid_answer(**change))
    assert errors and any(fragment in error for error in errors), errors


def test_validator_reports_missing_required_fields():
    from app.agents.llm import schema_errors

    answer = valid_answer()
    del answer["document_type"]
    assert schema_errors(answer) == ["document_type: falta"]


def fake_client(answer, sent):
    def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer))], usage=None, model="simulado")

    return lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))


def test_extract_invoice_sends_the_schema_and_returns_the_shape_the_rules_expect(monkeypatch):
    from app.agents import llm
    from app.config import settings

    sent: dict = {}
    monkeypatch.setattr(settings, "anthropic_api_key", "clave-de-prueba")
    monkeypatch.setattr(llm, "_client", fake_client(valid_answer(), sent))
    data, meta = llm.extract_invoice(text="FACTURA F-12\nTotal 121,50")
    assert sent["output_config"]["format"] == {"type": "json_schema", "schema": llm.INVOICE_SCHEMA}
    assert "schema_errors" not in meta
    assert data["is_invoice"] is True and data["document_type"] == "factura"
    # Importes como texto con punto decimal y vacíos como "": lo que ya consumen las reglas (interpretation.merge)
    assert (data["subtotal"], data["tax_total"], data["total"], data["tax_rate"]) == ("100", "21", "121.5", "21")
    assert data["customer_tax_id"] == "" and data["withholding_total"] == ""


def test_an_answer_outside_the_schema_is_flagged_and_not_trusted(monkeypatch):
    from app.agents import llm
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "clave-de-prueba")
    monkeypatch.setattr(llm, "_client", fake_client(valid_answer(is_invoice="sí", document_type="nota"), {}))
    data, meta = llm.extract_invoice(text="FACTURA F-12")
    assert meta["schema_errors"]
    assert data["is_invoice"] is None and data["document_type"] is None  # no se adivina


def test_claude_engine_keeps_false_as_an_answer(monkeypatch, tmp_path):
    """«No es factura» es una respuesta: no puede convertirse en «sin dato»."""
    from app.agents import llm
    from evaluation.core import Case
    from evaluation.core import engine_claude

    path = tmp_path / "doc.txt"
    path.write_text("PRESUPUESTO Nº 7", encoding="utf-8")
    answer = llm.normalize_invoice(valid_answer(is_invoice=False, document_type="presupuesto"))
    monkeypatch.setattr(llm, "extract_invoice", lambda **kwargs: (answer, {"model": "simulado"}))
    values, _meta = engine_claude(Case("x", path, {}), {"tax_id": "B00000000"}, None)
    assert values["is_invoice"] is False and values["document_type"] == "presupuesto"
