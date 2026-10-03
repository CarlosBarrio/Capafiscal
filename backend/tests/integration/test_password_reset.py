"""Recuperar el acceso: enlace firmado de un solo uso, que caduca, no revela cuentas y cierra las sesiones."""
from __future__ import annotations

from datetime import timedelta

from tests.integration.test_multiempresa import PASSWORD
from tests.integration.test_multiempresa import multi  # noqa: F401 (fixture)

NEW = "otra-clave-larga-2026"


def token_from(link: str) -> str:
    return link.split("?reset=", 1)[1]


def test_forgot_password_sends_a_link_and_never_reveals_accounts(client, multi, monkeypatch):  # noqa: F811
    from app import password_reset

    sent = []
    monkeypatch.setattr(password_reset, "send_link", lambda user, token: sent.append((user.email, token)) or True)
    known = client.post("/api/auth/forgot", json={"email": "Gestor@Gestoria.test"})
    unknown = client.post("/api/auth/forgot", json={"email": "nadie@ninguna.test"})
    assert known.status_code == unknown.status_code == 200 and known.json() == unknown.json()
    assert [email for email, _ in sent] == ["gestor@gestoria.test"]
    for _ in range(6):  # quien insiste deja de provocar correos (sin que la respuesta cambie)
        assert client.post("/api/auth/forgot", json={"email": "gestor@gestoria.test"}).status_code == 200
    assert len(sent) == 5


def test_reset_link_works_once_closes_sessions_and_unblocks_login(client, multi):  # noqa: F811
    session = multi["gestor_a"]
    assert client.get("/api/auth/me", headers=session).status_code == 200
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "gestor@gestoria.test", "password": "no-es-esta"})
    assert client.post("/api/auth/login", json={"email": "gestor@gestoria.test", "password": PASSWORD}).status_code == 429

    link = client.post(f"/api/admin/users/{gestor_id(client, multi)}/reset-link", headers=multi["admin"]).json()["link"]
    assert link.startswith("http") and "?reset=" in link
    assert client.post("/api/auth/reset", json={"token": token_from(link), "password": "corta"}).status_code == 422
    done = client.post("/api/auth/reset", json={"token": token_from(link), "password": NEW})
    assert done.status_code == 200
    assert client.get("/api/auth/me", headers=session).status_code == 401  # la sesión antigua ya no sirve
    assert client.post("/api/auth/reset", json={"token": token_from(link), "password": "y-otra-mas-larga"}).status_code == 400  # un solo uso
    assert client.post("/api/auth/login", json={"email": "gestor@gestoria.test", "password": PASSWORD}).status_code == 401
    assert client.post("/api/auth/login", json={"email": "gestor@gestoria.test", "password": NEW}).status_code == 200


def test_reset_link_expires_and_cannot_be_forged(client, multi):  # noqa: F811
    from app import clock

    link = client.post(f"/api/admin/users/{gestor_id(client, multi)}/reset-link", headers=multi["admin"]).json()["link"]
    token = token_from(link)
    user_id, expires, signature = token.split(".", 2)
    forged = f"{user_id}.{int(expires) + 86400}.{signature}"
    assert client.post("/api/auth/reset", json={"token": forged, "password": NEW}).status_code == 400
    with clock.frozen(clock.now() + timedelta(hours=1, minutes=1)):
        assert client.post("/api/auth/reset", json={"token": token, "password": NEW}).status_code == 400
    assert client.post("/api/auth/reset", json={"token": "1.2.3-no-vale", "password": NEW}).status_code == 400


def test_only_an_admin_can_create_reset_links(client, multi):  # noqa: F811
    target = gestor_id(client, multi)
    assert client.post(f"/api/admin/users/{target}/reset-link", headers=multi["gestor_a"]).status_code == 403
    assert client.post(f"/api/admin/users/{target}/reset-link").status_code == 401


def gestor_id(client, multi) -> int:  # noqa: F811
    users = client.get("/api/admin/users", headers=multi["admin"]).json()
    return next(user["id"] for user in users if user["email"] == "gestor@gestoria.test")


def test_the_email_carries_the_link_and_nothing_goes_to_the_logs(client, multi, monkeypatch, caplog):  # noqa: F811
    from app import outbox_service
    from app.config import settings

    delivered = []
    monkeypatch.setattr(settings, "smtp_host", "smtp.ejemplo.test")
    monkeypatch.setattr(settings, "smtp_from", "avisos@ejemplo.test")
    monkeypatch.setattr(outbox_service, "smtp_deliver", delivered.append)
    with caplog.at_level("INFO"):
        client.post("/api/auth/forgot", json={"email": "gestor@gestoria.test"})
        client.post("/api/auth/login", json={"email": "gestor@gestoria.test", "password": "no-es-esta"})
    assert len(delivered) == 1 and delivered[0]["To"] == "gestor@gestoria.test"
    body = delivered[0].get_content()
    token = token_from(body.split()[next(i for i, word in enumerate(body.split()) if "?reset=" in word)])
    assert client.post("/api/auth/reset", json={"token": token, "password": NEW}).status_code == 200
    assert token not in caplog.text and "no-es-esta" not in caplog.text and "Acceso fallido" in caplog.text
