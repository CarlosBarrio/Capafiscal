"""
Evaluación de notificaciones DEHú de punta a punta.

    python -m evaluation dehu                         # dataset dehu_sintetico
    python -m evaluation dehu --dataset <carpeta>     # otra carpeta con el mismo formato (<id>.json + <id>.pdf + labels.json)

Recorre el mismo camino que producción: la carpeta se configura como DEHU_INBOX_DIR y se llama a
POST /api/connectors/dehu/poll (FolderTransport → adaptador → entrada común → expediente). Después compara, por
notificación: organismo, tipo, expediente, fecha de puesta a disposición, fecha de notificación, fecha límite,
documentación pedida, clase de acción y si se abrió el expediente.

Una base de datos temporal y sin clave de IA: mide las reglas. Nada sale del equipo.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CHECKS = {
    "organismo": "Organismo", "tipo": "Tipo", "expediente": "Nº de expediente", "puesta_disposicion": "Puesta a disposición",
    "notificada": "Fecha de notificación", "fecha_limite": "Fecha límite", "documentacion": "Documentación pedida",
    "accion": "Acción", "accion_no_es": "Acción (no informativa)", "expediente_creado": "Expediente abierto",
}


def observed(client, item: dict[str, Any]) -> dict[str, Any]:
    """Lo que CapaFiscal ha registrado para una notificación de la DEHú."""
    notifications = client.get("/api/notifications", params={"open_only": "false"}).json()
    notifications = notifications if isinstance(notifications, list) else notifications.get("notifications", [])
    match = next((row for row in notifications if item.get("document_id") and row.get("document_id") == item["document_id"]), None)
    match = match or next((row for row in notifications if row.get("reference") == item["identifier"]), None)
    case = client.get(f"/api/cases/{item['case_id']}").json() if item.get("case_id") else {}
    classification = (match or {}).get("classification") or {}
    return {
        "organismo": (match or {}).get("issuer") or case.get("organism"),
        "tipo": (match or {}).get("notification_type"),
        "expediente": (match or {}).get("reference") or case.get("reference"),
        "puesta_disposicion": (match or {}).get("available_at"),
        "notificada": (match or {}).get("notified_at"),
        "fecha_limite": (match or {}).get("deadline") or case.get("deadline"),
        "regla_plazo": (match or {}).get("deadline_rule"),
        "documentacion": sorted({doc.get("code") for doc in case.get("documents") or [] if doc.get("code")}),
        "accion": classification.get("action"),
        "expediente_creado": bool(item.get("case_id")),
        "case_code": item.get("case_code"),
    }


def compare(expected: dict[str, Any], got: dict[str, Any]) -> dict[str, bool]:
    checks = {}
    for name, value in expected.items():
        if name == "accion_no_es":
            checks[name] = got.get("accion") != value
        elif name == "documentacion":
            checks[name] = sorted(value) == got.get("documentacion")
        else:
            checks[name] = got.get(name) == value
    return checks


def run(folder: Path) -> dict[str, Any]:
    from evaluation.casos import prepare_environment
    from evaluation.casos import reset_database

    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    work = prepare_environment("reglas")
    os.environ["DEHU_INBOX_DIR"] = str(folder)
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app

    settings.dehu_inbox_dir = str(folder)
    rows = []
    try:
        with TestClient(app) as client:
            reset_database()
            company = labels.get("empresa", {})
            client.put("/api/company", json={"name": company.get("name", "Empresa"), "tax_id": company.get("tax_id"), "legal_form": "SOCIEDAD"})
            response = client.post("/api/connectors/dehu/poll")
            response.raise_for_status()
            polled = response.json()
            by_id = {item["identifier"]: item for item in polled["items"]}
            for case in labels["casos"]:
                item = by_id.get(case["id"])
                got = observed(client, item) if item else {}
                checks = compare(case["expected"], got) if item else {name: False for name in case["expected"]}
                rows.append({"id": case["id"], "tags": case.get("tags", []), "expected": case["expected"], "got": got,
                             "checks": checks, "perfect": bool(checks) and all(checks.values()), "processed": item is not None})
            again = client.post("/api/connectors/dehu/poll").json()  # idempotencia: la segunda pasada no duplica
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    per_check: dict[str, dict[str, int]] = {}
    for row in rows:
        for name, ok in row["checks"].items():
            entry = per_check.setdefault(name, {"ok": 0, "n": 0})
            entry["n"] += 1
            entry["ok"] += int(ok)
    return {"dataset": folder.name, "notifications": len(rows), "processed": sum(row["processed"] for row in rows),
            "perfect": sum(row["perfect"] for row in rows), "duplicates_on_second_poll": again.get("duplicates"), "new_on_second_poll": again.get("new"),
            "checks": per_check, "rows": rows}


def to_markdown(report: dict[str, Any]) -> str:
    lines = [f"# Evaluación DEHú · {report['dataset']}", "",
             f"- Notificaciones: {report['notifications']} · procesadas {report['processed']} · perfectas **{report['perfect']}/{report['notifications']}**",
             f"- Segunda pasada por la carpeta: {report['new_on_second_poll']} nuevas, {report['duplicates_on_second_poll']} duplicadas (idempotencia)", "",
             "| Comprobación | Aciertos |", "|---|---:|"]
    for name, entry in report["checks"].items():
        lines.append(f"| {CHECKS.get(name, name)} | {entry['ok']}/{entry['n']} |")
    lines += ["", "## Fallos", ""]
    failures = [row for row in report["rows"] if not row["perfect"]]
    for row in failures:
        wrong = [f"{CHECKS.get(name, name)}: esperado `{row['expected'][name]}`, obtenido `{row['got'].get('accion' if name == 'accion_no_es' else name)}`"
                 for name, ok in row["checks"].items() if not ok]
        lines.append(f"- **{row['id']}**: " + "; ".join(wrong) + (f" (regla: {row['got'].get('regla_plazo')})" if row["got"].get("regla_plazo") else ""))
    if not failures:
        lines.append("Ninguno.")
    return "\n".join(lines) + "\n"
