"""Seguridad básica: freno a quien prueba contraseñas, cookies seguras con HTTPS y cabeceras."""
from __future__ import annotations

from tests.integration.test_multiempresa import PASSWORD
from tests.integration.test_multiempresa import multi  # noqa: F401 (fixture)


def test_login_is_throttled_after_repeated_failures(client, multi):  # noqa: F811
    bad = {"email": "gestor@gestoria.test", "password": "no-es-esta"}
    codes = [client.post("/api/auth/login", json=bad).status_code for _ in range(6)]
    assert codes == [401] * 5 + [429]
    # Ni siquiera la contraseña buena entra mientras dura el bloqueo; otro usuario sí.
    assert client.post("/api/auth/login", json={**bad, "password": PASSWORD}).status_code == 429
    assert client.post("/api/auth/login", json={"email": "lectura@gestoria.test", "password": PASSWORD}).status_code == 200


def test_security_headers_on_every_response(client):
    for url in ("/api/work", "/", "/api/no-existe"):
        headers = client.get(url).headers
        assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
    assert client.get("/api/work").headers["cache-control"] == "no-store"


def test_cookies_are_secure_behind_https(client, multi, monkeypatch):  # noqa: F811
    from app.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://capafiscal.ejemplo")
    response = client.post("/api/auth/login", json={"email": "lectura@gestoria.test", "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
