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
