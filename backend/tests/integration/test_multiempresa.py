"""Multiempresa: aislamiento total entre clientes y permisos por rol."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from evaluation.documentos import render_invoice

PASSWORD = "contraseña-segura-1"


@pytest.fixture()
def multi(client, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_required", True)
    setup = client.post("/api/auth/setup", json={"organization": "Gestoría Ejemplo", "admin_email": "admin@gestoria.test", "admin_password": PASSWORD,
                                                 "client_name": "Cliente A S.L.", "client_tax_id": "B00100016"}).json()
    admin = {"Authorization": f"Bearer {setup['token']}"}
    a = setup["client_id"]
    b = client.post("/api/admin/clients", json={"name": "Cliente B S.L.", "tax_id": "B00200022"}, headers=admin).json()["id"]

    def user(email, role, clients):
        client.post("/api/admin/users", json={"email": email, "password": PASSWORD, "role": role, "client_ids": clients}, headers=admin).raise_for_status()
        token = client.post("/api/auth/login", json={"email": email, "password": PASSWORD}).json()["token"]
        return {"Authorization": f"Bearer {token}"}

    pdf = tmp_path / "factura.pdf"
    render_invoice(pdf, supplier={"name": "Proveedor Común S.L.", "tax_id": "B00300038", "address": "C/ Uno 1"},
                   customer={"name": "Cliente", "tax_id": "B00100016", "address": "C/ Dos 2"}, number="PC-1", issued=date(2026, 9, 1),
                   lines=[("Servicio", Decimal(1), Decimal("100.00"))])
    context = {
        "admin": admin, "a": a, "b": b, "pdf": pdf,
        "gestor_a": user("gestor@gestoria.test", "GESTOR", [a]),
        "revisor_b": user("revisor@gestoria.test", "REVISOR", [b]),
        "cliente_a": user("cliente@a.test", "CLIENTE", [a]),
        "lectura_a": user("lectura@gestoria.test", "LECTURA", [a]),
    }
    client.cookies.clear()  # cada prueba decide con qué credenciales llama
    return context


def as_(headers, client_id=None):
    return {**headers, **({"X-Client-Id": str(client_id)} if client_id else {})}


def upload(client, headers, path):
    with path.open("rb") as handle:
        return client.post("/api/upload", files={"uploaded_file": (path.name, handle, "application/pdf")}, headers=headers)


def test_each_client_only_sees_its_own_data(client, multi):
    admin, a, b = multi["admin"], multi["a"], multi["b"]
    # El mismo PDF en dos clientes: no choca y queda separado.
    doc_a = upload(client, as_(admin, a), multi["pdf"]).json()["document"]["id"]
    doc_b = upload(client, as_(admin, b), multi["pdf"]).json()["document"]["id"]
    assert doc_a != doc_b
    assert [item["id"] for item in client.get("/api/documents", headers=as_(admin, a)).json()] == [doc_a]
    assert [item["id"] for item in client.get("/api/documents", headers=as_(admin, b)).json()] == [doc_b]
    assert client.get(f"/api/documents/{doc_b}", headers=as_(admin, a)).status_code == 404

    client.post("/api/events", json={"kind": "notification", "source": "api", "external_id": "n-1", "notification": {
        "issuer": "AEAT", "notification_type": "REQUERIMIENTO", "title": "Requerimiento", "summary": "Aporte el libro registro."}}, headers=as_(admin, a)).raise_for_status()
    # El mismo identificador externo en otro cliente es otro evento distinto.
    client.post("/api/events", json={"kind": "notification", "source": "api", "external_id": "n-1", "notification": {
        "issuer": "AEAT", "notification_type": "REQUERIMIENTO", "title": "Requerimiento", "summary": "Aporte el libro registro."}}, headers=as_(admin, b)).raise_for_status()
    cases_a = client.get("/api/cases", headers=as_(admin, a)).json()
    cases_b = client.get("/api/cases", headers=as_(admin, b)).json()
    assert len(cases_a) == 1 and len(cases_b) == 1 and cases_a[0]["id"] != cases_b[0]["id"]
    assert client.get(f"/api/cases/{cases_b[0]['id']}", headers=as_(admin, a)).status_code == 404


def test_roles(client, multi):
    a, b = multi["a"], multi["b"]
    assert client.get("/api/documents").status_code == 401  # sin sesión
    assert client.get("/api/documents", headers=as_(multi["gestor_a"], b)).status_code == 403  # cliente no asignado
    assert client.get("/api/documents", headers=multi["gestor_a"]).status_code == 200  # un solo cliente: se elige solo
    assert client.get("/api/documents", headers=multi["admin"]).status_code == 400  # admin con dos: hay que elegir
    assert client.post("/api/admin/clients", json={"name": "Otro"}, headers=multi["gestor_a"]).status_code == 403

    assert client.put("/api/company", json={"name": "X"}, headers=multi["revisor_b"]).status_code == 403  # revisor: no configura
    assert upload(client, multi["cliente_a"], multi["pdf"]).status_code == 201  # cliente: aporta documentos
    case_id = 1
    assert client.post(f"/api/cases/{case_id}/approve", headers=multi["cliente_a"]).status_code == 403  # …pero no decide
    assert client.post(f"/api/cases/{case_id}/approve", headers=multi["revisor_b"]).status_code != 403  # el revisor sí puede decidir
    assert upload(client, multi["lectura_a"], multi["pdf"]).status_code == 403  # lectura: solo ve
    assert client.get("/api/today", headers=multi["lectura_a"]).status_code == 200
    me = client.get("/api/auth/me", headers=multi["gestor_a"]).json()
    assert me["role"] == "GESTOR" and [item["id"] for item in me["clients"]] == [a]


def test_isolation_fails_closed_without_a_client(client, multi):
    from app.database import SessionLocal
    from app.models import Document
    from app.models import Invoice
    from app.tenancy import TenantError
    from app.tenancy import tenant_session

    upload(client, as_(multi["admin"], multi["a"]), multi["pdf"])
    with SessionLocal() as database:  # sin cliente elegido, en multiempresa: no se ve nada
        assert database.query(Document).count() == 0
    with tenant_session(multi["a"]) as database:
        assert database.query(Document).count() == 1
        intruder = Invoice(document_id=1, tenant_id=multi["b"])
        database.add(intruder)
        with pytest.raises(TenantError):
            database.flush()


def test_public_portal_works_for_the_right_client(client, multi):
    from app.models import Case
    from app.models import DocumentRequest
    from app.tenancy import tenant_session

    with tenant_session(multi["b"]) as database:
        case = Case(code="EXP-1", kind="NOTIFICATION", title="Requerimiento", status="WAITING_DOCS", facts={}, required_documents=[], proposed_actions=[], antecedents=[])
        database.add(case)
        database.flush()
        database.add(DocumentRequest(case_id=case.id, item_code="FACTURAS", label="Facturas del trimestre", token="t" * 32, status="PENDING"))
        database.commit()
    response = client.get(f"/api/portal/{'t' * 32}")
    assert response.status_code == 200 and response.json()["label"] == "Facturas del trimestre"


def test_setup_adopts_single_company_data(client, monkeypatch):
    from app.config import settings

    client.put("/api/company", json={"name": "Empresa Única S.L.", "tax_id": "B00100016", "legal_form": "SOCIEDAD"})
    monkeypatch.setattr(settings, "auth_required", True)
    setup = client.post("/api/auth/setup", json={"organization": "Gestoría", "admin_email": "a@g.test", "admin_password": PASSWORD,
                                                 "client_name": "Empresa Única S.L."}).json()
    assert setup["adopted_rows"] >= 1
    company = client.get("/api/company", headers={"Authorization": f"Bearer {setup['token']}"}).json()
    assert company["name"] == "Empresa Única S.L."
    assert client.post("/api/auth/setup", json={"organization": "Otra", "admin_email": "b@g.test", "admin_password": PASSWORD,
                                                "client_name": "Otra"}).status_code == 409


def test_scheduled_jobs_run_once_per_client(client, multi):
    from app.automation_service import run_due_all_clients
    from app.models import AutomationRun
    from app.tenancy import tenant_session

    done = run_due_all_clients()
    assert set(done) == {multi["a"], multi["b"]} and done[multi["a"]] == done[multi["b"]] > 0
    with tenant_session(multi["a"]) as database:
        assert database.query(AutomationRun).count() == done[multi["a"]]


def test_browser_session_cookie_requires_csrf_header_for_writes(client, multi):
    login = client.post("/api/auth/login", json={"email": "gestor@gestoria.test", "password": PASSWORD})
    assert "cf_session" in login.cookies or "cf_session" in client.cookies
    bare = {"Authorization": ""}
    assert client.get("/api/documents", headers=bare).status_code == 200  # lectura con la cookie
    assert upload(client, bare, multi["pdf"]).status_code == 403  # escritura con cookie sin cabecera: CSRF
    assert upload(client, {**bare, "X-CapaFiscal": "1"}, multi["pdf"]).status_code == 201
    assert client.get("/api/auth/status").json() == {"auth_required": True}
