"""Cuando algo falla fuera de CapaFiscal: el proceso sigue, el motivo queda a la vista y nunca se inventa nada.

PDF dañado, IA caída / lenta / limitada / con clave mala / con respuesta rara, DEHú inaccesible y agregador
bancario sin red. (Base de datos caída: test_observability; SMTP caído: test_smtp_local.)
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

REQUEST = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def failing_client(error: Exception):
    class Messages:
        def create(self, **kwargs):
            raise error

    return lambda: SimpleNamespace(beta=SimpleNamespace(messages=Messages()))


def status_error(cls, code: int):
    return cls(f"error {code}", response=httpx.Response(code, request=REQUEST), body=None)


def anthropic_errors():
    import anthropic

    return [
        (anthropic.APITimeoutError(request=REQUEST), "tiempo agotado"),
        (status_error(anthropic.RateLimitError, 429), "límite de peticiones (429)"),
        (status_error(anthropic.AuthenticationError, 401), "clave no válida (401)"),
        (status_error(anthropic.InternalServerError, 500), "error 500"),
        (anthropic.APIConnectionError(request=REQUEST), "sin conexión"),
        (anthropic.AnthropicError("respuesta inesperada"), "fallo del cliente (AnthropicError)"),
    ]


@pytest.mark.parametrize("index", range(6))
def test_ai_failures_fall_back_to_rules_with_a_readable_reason(monkeypatch, index):
    from app.agents import llm
    from app.config import settings

    error, reason = anthropic_errors()[index]
    monkeypatch.setattr(settings, "anthropic_api_key", "clave-de-prueba")
    monkeypatch.setattr(llm, "_client", failing_client(error))
    data, meta = llm.extract_invoice(text="FACTURA 1\nTotal 121,00")
    assert data is None and meta["fallback"] == reason


@pytest.mark.parametrize("raw", ["{no es json", "[1, 2, 3]", "\"texto\""])
def test_ai_answer_that_is_not_a_json_object_is_rejected(monkeypatch, raw):
    from app.agents import llm
    from app.config import settings

    response = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=raw)], usage=None, model="simulado")
    monkeypatch.setattr(settings, "anthropic_api_key", "clave-de-prueba")
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: response))))
    data, meta = llm.extract_invoice(text="FACTURA 1\nTotal 121,00")
    assert data is None and meta["fallback"] == "JSON inválido"


def test_missing_api_key_is_not_an_error(monkeypatch):
    from app.agents import llm
    from app.config import settings

    monkeypatch.setattr(settings, "anthropic_api_key", "")
    data, meta = llm.extract_invoice(text="FACTURA 1")
    assert data is None and meta["fallback"] == "sin ANTHROPIC_API_KEY"


@pytest.mark.parametrize("content", [b"%PDF-1.4\nbasura sin estructura\n", b"%PDF-1.7\n1 0 obj << /Type /Catalog"])
def test_damaged_pdf_fails_with_its_reason_and_is_logged_without_its_name(client, caplog, content):
    """Un PDF dañado no es un escaneado: «OCR necesario» mandaría a la persona por el camino equivocado."""
    with caplog.at_level(logging.INFO, logger="capafiscal.documents"):
        response = client.post("/api/upload", files={"uploaded_file": ("FACTURA-CLIENTE-PRIVADO.pdf", content, "application/pdf")})
    assert response.status_code == 201, response.text
    document = response.json().get("document") or response.json()
    assert document["status"] == "FAILED" and document["extraction_status"] == "FAILED"
    assert "dañado" in document["failure_reason"]

    record = next(item for item in caplog.records if item.name == "capafiscal.documents")
    assert record.fields["outcome"] == "error" and record.fields["stage"] == "lectura" and record.fields["error"] == "UnreadableDocument"
    assert "PRIVADO" not in record.getMessage() and "PRIVADO" not in str(record.fields)


def test_every_processed_document_leaves_one_log_line(client, caplog):
    with caplog.at_level(logging.INFO, logger="capafiscal.documents"):
        client.post("/api/upload", files={"uploaded_file": ("f.txt", "FACTURA Nº 7\nTotal 121,00 €".encode(), "text/plain")})
    records = [item for item in caplog.records if item.name == "capafiscal.documents"]
    assert len(records) == 1
    fields = records[0].fields
    assert fields["engine"] == "reglas" and fields["claude_calls"] == 0 and fields["ms"] >= 0 and fields["outcome"] != "error"


def test_unreachable_dehu_folder_is_an_error_not_an_empty_inbox(client, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "dehu_inbox_dir", str(tmp_path / "unidad-de-red-sin-montar"))
    response = client.post("/api/connectors/dehu/poll")
    assert response.status_code == 503
    assert "No se puede leer la carpeta de la DEHú" in response.json()["detail"]


def test_bank_aggregator_without_network_is_a_clear_error(monkeypatch):
    from app.bank_connect import BankProviderError
    from app.bank_connect import GoCardlessProvider

    def offline(*args, **kwargs):
        raise httpx.ConnectError("sin red")

    monkeypatch.setattr(httpx, "request", offline)
    provider = GoCardlessProvider("https://agregador.ejemplo.test/api/v2", "id-de-prueba", "clave-de-prueba")
    with pytest.raises(BankProviderError, match="No se pudo conectar con el agregador bancario") as caught:
        provider.institutions("ES")
    assert "clave-de-prueba" not in str(caught.value)
