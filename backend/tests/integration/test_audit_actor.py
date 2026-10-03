"""Integridad de la auditoría: quién firma cada evento.

La identidad sale de la sesión autenticada (request.state.user). La cabecera X-Actor la puede poner
cualquiera: se guarda aparte como «declared_actor» y nunca suplanta al usuario. Sin persona detrás
(planificador, conectores) el actor lo dice el propio sistema («automatizacion», «conector-correo»…).
Datos sintéticos.
"""
from __future__ import annotations

from tests.integration.test_multiempresa import as_
from tests.integration.test_multiempresa import multi  # noqa: F401 (fixture)
from tests.integration.test_multiempresa import upload


def audit_events(client_id=None, action=None):
    from app.database import SessionLocal
    from app.models import AuditEvent
    from app.tenancy import tenant_session

    session = tenant_session(client_id) if client_id is not None else SessionLocal()
    with session as database:
        query = database.query(AuditEvent)
        if action:
            query = query.filter(AuditEvent.action == action)
        return [(item.actor, dict(item.event_data or {})) for item in query.order_by(AuditEvent.id).all()]


def test_authenticated_user_cannot_sign_as_someone_else(client, multi):
    """El gestor de A manda X-Actor con el correo del administrador: el evento lo firma el gestor."""
    headers = {**as_(multi["gestor_a"], multi["a"]), "X-Actor": "admin@gestoria.test"}
    assert upload(client, headers, multi["pdf"]).status_code == 201
    (actor, data), = audit_events(multi["a"], "document.uploaded")
    assert actor == "gestor@gestoria.test"
    assert data["authenticated_user"]["email"] == "gestor@gestoria.test"
    assert data["declared_actor"] == "admin@gestoria.test"  # se guarda, pero como lo que es: una declaración


def test_engines_inside_a_request_still_record_who_triggered_them(client, multi):
    """Eventos firmados por un motor («extractor») dentro de una petición: queda quién la hizo."""
    upload(client, as_(multi["gestor_a"], multi["a"]), multi["pdf"])
    engine_events = [data for actor, data in audit_events(multi["a"]) if actor == "extractor"]
    assert engine_events and all(item["authenticated_user"]["email"] == "gestor@gestoria.test" for item in engine_events)


def test_without_login_the_header_is_never_an_identity(client):
    """Una sola empresa sin inicio de sesión: actor «usuario-local»; X-Actor solo como declarado."""
    client.put("/api/company", json={"name": "Empresa Simulada S.L.", "tax_id": "B00000017"}, headers={"X-Actor": "director-general"})
    events = audit_events(action="company.updated") or [item for item in audit_events() if item[1].get("declared_actor") == "director-general"]
    assert events
    for actor, data in events:
        assert actor == "usuario-local" and data["declared_actor"] == "director-general" and "authenticated_user" not in data


def test_scheduled_automations_sign_as_automation(client):
    from app.automation_service import local_now
    from app.automation_service import run_due
    from app.database import SessionLocal

    with SessionLocal() as database:
        run_due(database, now=local_now().replace(hour=23, minute=0))
        database.commit()
    events = [item for item in audit_events() if item[0] == "automatizacion"]
    assert events and all("authenticated_user" not in data and "declared_actor" not in data for _actor, data in events)


def test_public_portal_upload_has_no_identity_even_with_headers(client):
    """Endpoint público (portal con enlace): firma «portal»; ni sesión ni X-Actor se convierten en identidad."""
    from tests.agents.test_agents import REQUERIMIENTO
    from tests.agents.test_agents import open_case
    from tests.agents.test_agents import setup_company
    from tests.agents.test_agents import upload_text

    setup_company(client)
    upload_text(client, "requerimiento_303.txt", REQUERIMIENTO)
    open_case(client)
    message = client.get("/api/outbox", params={"kind": "REQUEST"}).json()["messages"][0]
    token = next(line.split("/portal/")[1].strip() for line in message["body"].splitlines() if "/portal/" in line)
    client.post(f"/api/portal/{token}", headers={"X-Actor": "gestor@gestoria.test"},
                files={"uploaded_file": ("justificante.txt", b"Justificante de pago simulado", "text/plain")})
    (actor, data), = audit_events(action="case.document_received")
    assert actor == "portal" and "authenticated_user" not in data and data["declared_actor"] == "gestor@gestoria.test"


def test_internal_connector_work_keeps_its_own_actor(client, monkeypatch):
    """Llamadas internas (buzón desde el planificador): actor del conector y sin usuario."""
    from tests.integration.test_mail_isolation import mailbox
    from tests.integration.test_mail_isolation import run_inbox_for  # noqa: F401
    from app.automation_service import run_automation
    from app.database import SessionLocal

    mailbox()
    with SessionLocal() as database:
        run_automation(database, "EMAIL_INBOX", trigger="SCHEDULE", actor="automatizacion")
        database.commit()
    received = audit_events(action="document.received_by_email")
    assert received and all(actor == "conector-correo" and "authenticated_user" not in data for actor, data in received)


def test_no_route_turns_the_header_into_the_actor():
    """Búsqueda estática: ningún endpoint lee la cabecera X-Actor directamente; todos pasan por resolve_actor."""
    import ast
    import pathlib

    app_dir = pathlib.Path(__file__).resolve().parents[2] / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if path.name in {"deps.py", "main.py"}:
            continue  # resolve_actor y el middleware (que lo guarda como declarado)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.lower() == "x-actor":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, offenders
