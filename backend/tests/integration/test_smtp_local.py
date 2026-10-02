"""Correo en desarrollo: solo a un buzón local (Mailpit, MailHog…), nunca a un servidor real.

El buzón de pruebas es un servidor SMTP mínimo en un hilo: el envío pasa por smtplib de verdad."""
from __future__ import annotations

import socket
import socketserver
import threading
from email import message_from_bytes

import pytest


class SinkHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"220 buzon-local ESMTP\r\n")
        while line := self.rfile.readline():
            command = line.decode().strip().upper()
            if command.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-buzon-local\r\n250 SIZE 10485760\r\n")  # sin STARTTLS, como Mailpit por defecto
            elif command == "DATA":
                self.wfile.write(b"354 adelante\r\n")
                data = b""
                while (chunk := self.rfile.readline()) not in (b".\r\n", b""):
                    data += chunk
                self.server.received.append(message_from_bytes(data))
                self.wfile.write(b"250 guardado\r\n")
            elif command == "QUIT":
                self.wfile.write(b"221 adios\r\n")
                return
            else:
                self.wfile.write(b"250 vale\r\n")


@pytest.fixture
def sink():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), SinkHandler)
    server.received = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def message(to: str = "destino@ejemplo.test"):
    from email.message import EmailMessage

    email = EmailMessage()
    email["From"], email["To"], email["Subject"] = "capafiscal@ejemplo.test", to, "Prueba local"
    email.set_content("SIMULACIÓN — NO OFICIAL")
    return email


def test_local_sink_receives_the_mail_without_tls(sink, monkeypatch):
    from app.config import settings
    from app.outbox_service import smtp_deliver

    monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
    monkeypatch.setattr(settings, "smtp_port", sink.server_address[1])
    monkeypatch.setattr(settings, "smtp_user", "")
    smtp_deliver(message())
    assert len(sink.received) == 1 and sink.received[0]["Subject"] == "Prueba local"


def test_development_never_sends_to_a_real_server(monkeypatch):
    import smtplib

    from app.config import settings
    from app.outbox_service import SmtpBlocked
    from app.outbox_service import smtp_deliver

    def no_network(*args, **kwargs):
        raise AssertionError("no debe abrir ninguna conexión")

    monkeypatch.setattr(smtplib, "SMTP", no_network)
    monkeypatch.setattr(smtplib, "SMTP_SSL", no_network)
    monkeypatch.setattr(settings, "smtp_host", "smtp.office365.com")
    monkeypatch.setattr(settings, "app_environment", "development")
    monkeypatch.setattr(settings, "smtp_allow_external", False)
    with pytest.raises(SmtpBlocked, match="no se envía correo real"):
        smtp_deliver(message())


def test_smtp_down_is_a_clear_error_and_the_message_stays_failed(client, monkeypatch):
    """Servidor de correo caído: el mensaje queda FAILED con el motivo, y la API responde sin 500."""
    from app.config import settings
    from tests.integration.test_operations import create_customer
    from tests.integration.test_operations import issue
    from tests.integration.test_operations import setup_company

    with socket.socket() as probe:  # un puerto local en el que no escucha nadie
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setattr(settings, "smtp_host", "localhost")
    monkeypatch.setattr(settings, "smtp_port", closed_port)
    monkeypatch.setattr(settings, "smtp_from", "facturas@ejemplo.test")

    setup_company(client)
    customer = create_customer(client)
    invoice = issue(client, customer["id"], [{"description": "Servicio", "unit_price": 100}])
    outbox = client.post(f"/api/sales/invoices/{invoice['id']}/send").json()
    response = client.post(f"/api/outbox/{outbox['id']}/send")
    assert 400 <= response.status_code < 500, response.text
    assert "servidor de correo" in response.json()["detail"].lower()
    stored = next(item for item in client.get("/api/outbox").json()["messages"] if item["id"] == outbox["id"])
    assert stored["status"] == "FAILED" and stored["error"]


def test_production_check_warns_about_a_test_mailbox(monkeypatch):
    from app.config import settings
    from app.production import problems

    monkeypatch.setattr(settings, "smtp_host", "mailpit")
    assert any("buzón de pruebas" in text for _, text in problems(settings))


class RecordingSMTP:
    """Servidor SMTP simulado que anota lo que se le pide (conexión, STARTTLS, login, envío); no abre red."""

    calls: list[tuple] = []

    def __init__(self, host, port, timeout=None):
        RecordingSMTP.calls.append(("connect", host, port))

    def ehlo(self):
        pass

    def has_extn(self, name):
        return name == "starttls"

    def starttls(self, context=None):
        RecordingSMTP.calls.append(("starttls",))

    def login(self, user, password):
        RecordingSMTP.calls.append(("login", user))

    def send_message(self, message):
        RecordingSMTP.calls.append(("send", message["To"]))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.mark.parametrize("allow_external", [False, True])
def test_production_sends_to_an_external_server_with_tls_whatever_the_flag(monkeypatch, allow_external):
    """En producción el correo real sale siempre (con STARTTLS); SMTP_ALLOW_EXTERNAL solo afecta fuera de producción."""
    import smtplib

    from app.config import settings
    from app.outbox_service import smtp_deliver

    RecordingSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)
    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setattr(settings, "smtp_host", "smtp.office365.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_use_ssl", False)
    monkeypatch.setattr(settings, "smtp_user", "avisos@ejemplo.test")
    monkeypatch.setattr(settings, "smtp_allow_external", allow_external)
    smtp_deliver(message())
    assert RecordingSMTP.calls == [("connect", "smtp.office365.com", 587), ("starttls",), ("login", "avisos@ejemplo.test"), ("send", "destino@ejemplo.test")]


def test_development_sends_to_an_external_server_only_when_explicitly_allowed(monkeypatch):
    import smtplib

    from app.config import settings
    from app.outbox_service import smtp_deliver

    RecordingSMTP.calls = []
    monkeypatch.setattr(smtplib, "SMTP", RecordingSMTP)
    monkeypatch.setattr(settings, "app_environment", "development")
    monkeypatch.setattr(settings, "smtp_host", "smtp.office365.com")
    monkeypatch.setattr(settings, "smtp_use_ssl", False)
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "smtp_allow_external", True)
    smtp_deliver(message())
    assert ("starttls",) in RecordingSMTP.calls and RecordingSMTP.calls[-1] == ("send", "destino@ejemplo.test")


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False), ("no", False), (None, False)])
def test_smtp_allow_external_is_read_from_the_environment(monkeypatch, tmp_path, raw, expected):
    """El valor por defecto es False; solo un «sí» explícito en el entorno lo activa."""
    from app.config import Settings

    monkeypatch.chdir(tmp_path)
    if raw is None:
        monkeypatch.delenv("SMTP_ALLOW_EXTERNAL", raising=False)
    else:
        monkeypatch.setenv("SMTP_ALLOW_EXTERNAL", raw)
    assert Settings(_env_file=None).smtp_allow_external is expected


def test_localhost_is_treated_as_a_test_mailbox_in_every_environment(monkeypatch):
    """Comportamiento actual, documentado: «localhost», «127.0.0.1», «::1», «mailpit» y «mailhog» se tratan como
    buzón de pruebas en cualquier entorno. Por eso en desarrollo se permite y STARTTLS solo se usa si el servidor
    lo ofrece. Si en ese equipo hay un relé real (p. ej. postfix en localhost), el correo SÍ saldría: es una
    decisión pendiente, no una garantía."""
    from app.config import settings
    from app.outbox_service import local_smtp

    for host in ("localhost", "LOCALHOST ", "127.0.0.1", "::1", "mailpit", "mailhog"):
        monkeypatch.setattr(settings, "smtp_host", host)
        assert local_smtp(), host
    for host in ("smtp.office365.com", "10.0.0.5", "mail.ejemplo.test"):
        monkeypatch.setattr(settings, "smtp_host", host)
        assert not local_smtp(), host
