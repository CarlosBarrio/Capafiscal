"""Producción: registros sin datos personales, salud para el monitor y comprobación de la configuración."""
from __future__ import annotations

import logging

import pytest

from tests.integration.test_multiempresa import multi  # noqa: F401 (fixture)


def test_health_is_public_says_what_fails_and_nothing_else(client, multi):  # noqa: F811
    response = client.get("/api/health")  # sin sesión: el monitor externo no inicia sesión
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] and body["checks"]["database"]["ok"] and body["checks"]["schema"]["ok"] and "disk" in body["checks"]
    assert set(body) == {"success", "application", "checks"}  # nada de clientes, facturas ni configuración


def test_health_answers_503_when_the_database_is_down(client, monkeypatch, tmp_path):
    from sqlalchemy import create_engine

    import app.database

    monkeypatch.setattr(app.database, "engine", create_engine(f"sqlite:///{tmp_path}/no/existe/base.db"))
    response = client.get("/api/health")
    assert response.status_code == 503 and response.json()["checks"]["database"] == {"ok": False, "detail": "OperationalError"}


def test_every_api_request_is_logged_without_its_content(client, caplog):
    with caplog.at_level(logging.INFO, logger="capafiscal.request"):
        response = client.get("/api/search", params={"q": "B00999999 Proveedor Secreto"}, headers={"X-Request-ID": "prueba-123"})
    assert response.headers["x-request-id"] == "prueba-123"
    lines = [record for record in caplog.records if record.name == "capafiscal.request"]
    assert lines and lines[-1].fields["path"] == "/api/search" and lines[-1].fields["status"] == 200 and lines[-1].fields["request_id"] == "prueba-123"
    assert "Secreto" not in caplog.text and "B00999999" not in caplog.text


def test_json_log_lines_carry_the_request_fields():
    import json

    from app.observability import JsonFormatter

    record = logging.LogRecord("capafiscal.request", logging.INFO, __file__, 1, "GET /api/work → 200", None, None)
    record.fields = {"path": "/api/work", "status": 200, "ms": 3.2}
    line = json.loads(JsonFormatter().format(record))
    assert line["level"] == "INFO" and line["path"] == "/api/work" and line["status"] == 200


def test_production_refuses_to_start_with_an_unsafe_configuration(monkeypatch):
    from app.config import settings
    from app.production import CRITICAL
    from app.production import check_on_startup
    from app.production import problems

    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setattr(settings, "auth_required", False)
    monkeypatch.setattr(settings, "public_base_url", "http://capafiscal.ejemplo")
    critical = [text for level, text in problems() if level == CRITICAL]
    assert any("AUTH_REQUIRED" in text for text in critical) and any("https" in text for text in critical)
    with pytest.raises(RuntimeError, match="no arranca"):
        check_on_startup()

    monkeypatch.setattr(settings, "auth_required", True)
    monkeypatch.setattr(settings, "public_base_url", "https://capafiscal.ejemplo")
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "seed_demo_data", False)
    monkeypatch.setattr(settings, "enable_demo_connectors", False)
    monkeypatch.setattr(settings, "reset_data_on_startup", False)
    assert not [text for level, text in problems() if level == CRITICAL]
    check_on_startup()  # solo avisos: arranca


def test_development_never_blocks_startup(monkeypatch):
    from app.config import settings
    from app.production import check_on_startup

    monkeypatch.setattr(settings, "app_environment", "development")
    monkeypatch.setattr(settings, "auth_required", False)
    check_on_startup()
