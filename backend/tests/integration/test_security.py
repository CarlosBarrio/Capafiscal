"""Seguridad básica: freno a quien prueba contraseñas, cookies seguras con HTTPS y cabeceras."""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"

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
        assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "SAMEORIGIN"
    assert client.get("/api/work").headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in client.get("/").headers  # sin HTTPS no se anuncia


def test_pages_only_run_scripts_from_static(client, monkeypatch):
    from app.config import settings

    policy = client.get("/").headers["content-security-policy"]
    assert "script-src 'self'" in policy and "unsafe-inline' https://fonts" in policy and "frame-ancestors 'self'" in policy
    assert "script-src 'self' 'unsafe" not in policy
    monkeypatch.setattr(settings, "public_base_url", "https://capafiscal.ejemplo")
    assert client.get("/").headers["strict-transport-security"].startswith("max-age=31536000")


def test_no_inline_scripts_or_handlers_in_the_frontend():
    """La CSP prohíbe el código en línea: un onclick= o un <script> sin src dejaría de funcionar."""
    offenders = []
    for path in [*STATIC.glob("*.js"), *STATIC.glob("*.html")]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\son(click|change|input|submit|keydown|load|error)=|<script>|javascript:", line):
                offenders.append(f"{path.name}:{number}")
    assert not offenders, offenders


def test_cookies_are_secure_behind_https(client, multi, monkeypatch):  # noqa: F811
    from app.config import settings

    monkeypatch.setattr(settings, "public_base_url", "https://capafiscal.ejemplo")
    response = client.post("/api/auth/login", json={"email": "lectura@gestoria.test", "password": PASSWORD})
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
