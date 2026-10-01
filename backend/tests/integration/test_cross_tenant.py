"""Aislamiento entre clientes sobre TODA la API, no sobre una lista hecha a mano.

El cliente B se llena de datos marcados con «SECRETO-B». Después, como el cliente A
(sin datos propios) se llama a cada ruta de la API que exista hoy (sale del esquema
OpenAPI, así que una ruta nueva entra sola en la prueba):

    - ninguna respuesta contiene el marcador de B;
    - ninguna ruta con un identificador de B responde 2xx (ni lectura ni escritura);
    - las tablas de B quedan byte a byte iguales después de todo el barrido.

Además se comprueba la estructura: toda tabla de datos lleva tenant_id.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal

from sqlalchemy import text

from evaluation.documentos import render_invoice
from tests.integration.test_multiempresa import as_
from tests.integration.test_multiempresa import multi  # noqa: F401 (fixture)
from tests.integration.test_multiempresa import upload

MARK = "SECRETO-B"
GLOBAL_TABLES = {"organizations", "users", "clients", "user_clients", "api_sessions",  # comunes a la gestoría
                 "intel_sources", "external_items"}  # información pública (BOE…): lo de cada cliente está en intel_matches
PUBLIC_PREFIXES = ("/api/portal/", "/api/auth/")  # acceso por token de un solo uso o de sesión, no por cliente
# Acciones sin identificador que recorren «todo»: si el aislamiento fallase, tocarían a B.
SWEEP_ACTIONS = ("/api/bank/reconcile", "/api/bank/confirm-suggestions", "/api/agents/anomalies/scan", "/api/agents/pulse/run",
                 "/api/documents/reprocess-pending", "/api/agents/deadlines/watch", "/api/agents/process-pending")
TABLE_FOR_PARAM = {
    "document_id": "documents", "invoice_id": "invoices", "case_id": "cases", "task_id": "review_tasks", "employee_id": "employees",
    "transaction_id": "bank_transactions", "notification_id": "fiscal_notifications", "customer_id": "customers", "project_id": "projects",
    "absence_id": "absences", "assignment_id": "project_assignments", "attachment_id": "case_attachments", "entry_id": "time_entries",
    "event_id": "ingested_events", "message_id": "outbox_messages", "payslip_id": "payslips", "rule_id": "learning_rules",
    "run_id": "agent_runs", "template_id": "recurring_invoices", "match_id": "intel_matches",
}


def tenant_tables():
    from app.database import Base
    from app.models import TenantMixin

    return {mapper.class_.__tablename__ for mapper in Base.registry.mappers if issubclass(mapper.class_, TenantMixin)}


def snapshot(tenant: int) -> dict[str, str]:
    """Huella de todas las filas de un cliente, leída sin pasar por el filtro del ORM."""
    from app.database import engine

    result = {}
    with engine.connect() as connection:
        for table in sorted(tenant_tables()):
            rows = connection.execute(text(f'SELECT * FROM "{table}" WHERE tenant_id = :t ORDER BY id'), {"t": tenant}).mappings().all()
            result[table] = hashlib.sha256(json.dumps([dict(row) for row in rows], default=str, sort_keys=True).encode()).hexdigest()
    return result


def ids_of(tenant: int) -> dict[str, list[int]]:
    from app.database import engine

    result = {}
    with engine.connect() as connection:
        for table in tenant_tables():
            result[table] = list(connection.execute(text(f'SELECT id FROM "{table}" WHERE tenant_id = :t ORDER BY id'), {"t": tenant}).scalars())
    return result


def seed_b(client, headers, tmp_path):
    """Un poco de todo en el cliente B, con el marcador en los textos."""
    pdf = tmp_path / "secreto.pdf"
    render_invoice(pdf, supplier={"name": f"{MARK} Suministros S.L.", "tax_id": "B00400044", "address": "C/ Tres 3"},
                   customer={"name": "Cliente B S.L.", "tax_id": "B00200022", "address": "C/ Cuatro 4"}, number="SB-1", issued=date(2026, 9, 1),
                   lines=[(f"Servicio {MARK}", Decimal(1), Decimal("250.00"))])
    upload(client, headers, pdf).raise_for_status()
    client.put("/api/company", json={"name": f"{MARK} Empresa S.L.", "tax_id": "B00200022", "legal_form": "SOCIEDAD"}, headers=headers).raise_for_status()
    client.post("/api/events", json={"kind": "notification", "source": "api", "external_id": "nb-1", "notification": {
        "issuer": "AEAT", "notification_type": "REQUERIMIENTO", "title": f"Requerimiento {MARK}", "summary": f"Aporte el libro registro {MARK}.",
        "deadline": "2026-10-20"}}, headers=headers).raise_for_status()
    csv = f"Fecha;Concepto;Importe\n10/09/2026;TRANSFERENCIA {MARK} SB-1;-302,50\n12/09/2026;COBRO {MARK};500,00\n".encode()
    client.post("/api/bank/import", files={"uploaded_file": ("b.csv", csv, "text/csv")}, headers=headers).raise_for_status()
    client.post("/api/team/employees", json={"first_name": MARK, "last_name": "Persona", "tax_id": "00000001R", "hire_date": "2026-01-01"}, headers=headers).raise_for_status()
    client.post("/api/sales/customers", json={"name": f"{MARK} Cliente S.L.", "tax_id": "B00600063"}, headers=headers).raise_for_status()
    client.post("/api/team/projects", json={"name": f"Proyecto {MARK}"}, headers=headers).raise_for_status()
    client.post("/api/agents/anomalies/scan", json={}, headers=headers)
    # Inteligencia: el perfil y las novedades relevantes de B son de B (el BOE es público y común).
    client.put("/api/intelligence/profile", json={"common": {"sector": MARK}, "juridico": {"areas": ["laboral", "mercantil"]}}, headers=headers).raise_for_status()
    client.post("/api/intelligence/refresh", json={"day": "2026-09-29", "back_days": 1}, headers=headers).raise_for_status()


def concrete_urls(path: str, b_ids: dict[str, list[int]]) -> list[str]:
    params = re.findall(r"\{(\w+)\}", path)
    values: dict[str, list[str]] = {}
    for name in params:
        if name in TABLE_FOR_PARAM and b_ids.get(TABLE_FOR_PARAM[name]):
            values[name] = [str(item) for item in b_ids[TABLE_FOR_PARAM[name]]][:5]
        elif name.endswith("_id"):
            values[name] = [str(item) for item in sorted({i for ids in b_ids.values() for i in ids})][:5] or ["1"]
        else:
            values[name] = {"party": ["supplier"], "key": ["B00400044"], "model": ["303"], "decision": ["aprobar"], "index": ["0"],
                            "code": ["EXP-2026-0001"], "token": ["x"], "period": ["2026-09"]}.get(name, ["1"])
    urls = [path]
    for name in params:
        urls = [url.replace("{" + name + "}", value) for url in urls for value in values[name]]
    return urls


def test_tables_with_data_all_carry_tenant_id():
    from app.database import Base

    tables = {mapper.class_.__tablename__ for mapper in Base.registry.mappers}
    assert tables - tenant_tables() <= GLOBAL_TABLES, f"Tablas sin tenant_id: {sorted(tables - tenant_tables() - GLOBAL_TABLES)}"


def test_client_a_cannot_read_or_change_anything_of_client_b(client, multi, tmp_path, monkeypatch):  # noqa: F811
    from pathlib import Path

    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "intel_boe_folder", Path(__file__).resolve().parents[1] / "fixtures" / "boe")

    admin, a, b = multi["admin"], multi["a"], multi["b"]
    seed_b(client, as_(admin, b), tmp_path)
    b_ids = ids_of(b)
    assert sum(len(ids) for ids in b_ids.values()) > 20, "B debería tener datos de muchos tipos"
    before = snapshot(b)

    headers = {**as_(admin, a), "X-CapaFiscal": "1"}
    leaks, writes, calls = [], [], 0
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/") or path.startswith(PUBLIC_PREFIXES) or path.startswith("/api/admin/"):
            continue
        for method in operations:
            has_ids = "{" in path
            if method != "get" and not has_ids and path not in SWEEP_ACTIONS:
                continue  # crear cosas en A no prueba nada sobre B
            for url in concrete_urls(path, b_ids):
                kwargs = {"json": {}} if method in {"post", "put", "patch"} else {}
                response = client.request(method.upper(), url, headers=headers, **kwargs)
                calls += 1
                body = response.content.decode("utf-8", errors="ignore")
                if MARK in body:
                    leaks.append(f"{method.upper()} {url} → {response.status_code} contiene datos de B")
                id_params = [name for name in re.findall(r"\{(\w+)\}", path) if name.endswith("_id")]
                if id_params and 200 <= response.status_code < 300:
                    writes.append(f"{method.upper()} {url} → {response.status_code} con un identificador de B")
    assert calls > 150
    assert not leaks, "\n".join(leaks)
    assert not writes, "\n".join(writes)
    after = snapshot(b)
    changed = [table for table in before if before[table] != after[table]]
    assert not changed, f"El barrido desde A cambió tablas de B: {changed}"

    # Y B sigue viendo lo suyo.
    assert MARK in client.get("/api/documents", headers=as_(admin, b)).text
