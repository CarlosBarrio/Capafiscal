"""Contrato de la API: una ruta, un endpoint; OpenAPI dice la verdad; una lectura no escribe."""
from __future__ import annotations

import ast
import pathlib
from collections import defaultdict

APP_DIR = pathlib.Path(__file__).resolve().parents[2] / "app"
METHODS = {"get", "post", "put", "patch", "delete"}

# GET que todavía escriben, con su motivo (ver el informe de la fase 0.5). Pospuestos: requieren una decisión
# de API/UX. Si se corrige uno, hay que quitarlo de aquí; si aparece uno nuevo, este test falla.
GET_WRITES_POSTPONED = {
    "dashboard_today": "sincroniza tareas de revisión derivadas (synchronize_all_review_tasks)",
    "list_risks": "sincroniza tareas de revisión derivadas",
    "review_task_inbox": "sincroniza tareas de revisión derivadas",
    "export_ledger": "auditoría de acceso a una exportación",
    "payroll_sepa": "auditoría de acceso a la remesa SEPA",
}


def endpoints():
    """(método, ruta completa) → [archivo:función], leyendo los decoradores (prefijos de APIRouter incluidos)."""
    found = defaultdict(list)
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        prefixes = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and getattr(node.value.func, "id", None) == "APIRouter":
                prefix = next((item.value.value for item in node.value.keywords if item.arg == "prefix"), "")
                prefixes[node.targets[0].id] = prefix
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr in METHODS
                            and decorator.args and isinstance(decorator.args[0], ast.Constant)):
                        owner = getattr(decorator.func.value, "id", "")
                        full = ("" if owner == "app" else prefixes.get(owner, "")) + decorator.args[0].value
                        found[(decorator.func.attr.upper(), full)].append((path.name, node))
    return found


def test_no_method_and_path_is_declared_twice():
    duplicated = {key: [f"{name}:{node.name}" for name, node in items] for key, items in endpoints().items() if len(items) > 1}
    assert not duplicated, duplicated


def test_get_api_agents_is_the_agents_overview_and_openapi_agrees(client):
    response = client.get("/api/agents")
    assert response.status_code == 200
    data = response.json()
    assert {"routes", "engine", "ai_enabled", "runs_this_month", "agents"} <= set(data)
    assert {agent["code"] for agent in data["agents"]} >= {"vigilante", "fiscal", "memoria", "detector", "gestor", "perseguidor", "director"}

    from app.main import app

    operation = app.openapi()["paths"]["/api/agents"]["get"]
    assert operation["operationId"] == "agents_api_agents_get"  # agent_routes.agents, el que responde
    (name, node), = endpoints()[("GET", "/api/agents")]
    assert (name, node.name) == ("agent_routes.py", "agents")


def test_get_endpoints_do_not_write_except_the_documented_ones():
    writers = {}
    for (method, _path), items in endpoints().items():
        if method != "GET":
            continue
        for name, node in items:
            source = ast.unparse(node)
            if ".commit(" in source or "add_audit_event(" in source:
                writers[node.name] = name
    assert set(writers) == set(GET_WRITES_POSTPONED), writers


def test_automations_overview_does_not_commit(client):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    commits = []

    def count(session):
        commits.append(session)

    event.listen(Session, "before_commit", count)
    try:
        assert client.get("/api/automations").status_code == 200
    finally:
        event.remove(Session, "before_commit", count)
    assert commits == []
