"""Catálogo del equipo de agentes y su actividad."""
from __future__ import annotations

from app import clock
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import llm
from app.models import AgentRun
from app.models import AgentStep

AGENTS: list[dict[str, Any]] = [
    {"code": "vigilante", "name": "Vigilante", "icon": "eye", "need": "No quiero revisar 120 sitios cada mañana",
     "role": "Vigila buzones, sede electrónica y correo, detecta lo nuevo y lo descarga."},
    {"code": "expedientes", "name": "Expedientes", "icon": "archive", "need": "¿A quién le afecta esto?",
     "role": "Identifica a quién afecta cada documento (empresa, empleado, cliente o proveedor), qué trámite es y abre el expediente."},
    {"code": "fiscal", "name": "Fiscal", "icon": "receipt", "need": "No quiero llegar al día 20 con sorpresas",
     "role": "Calcula los modelos en continuo y comprueba si una notificación afecta a algún periodo y dónde está la diferencia."},
    {"code": "memoria", "name": "Memoria", "icon": "brain", "need": "¿Qué pasó la última vez?",
     "role": "Recuerda todo lo que ha pasado y encuentra antecedentes con evidencia documental."},
    {"code": "gestor", "name": "Gestor de incidencias", "icon": "briefcase", "need": "Dime qué tengo que hacer",
     "role": "Trabaja el expediente: qué piden, qué documentos prepara el sistema, qué falta, qué hacer y el borrador de respuesta."},
    {"code": "perseguidor", "name": "Perseguidor", "icon": "send", "need": "No quiero perseguir a nadie",
     "role": "Pide la documentación que falta con un enlace de subida, recuerda y deja de insistir cuando llega y es correcta."},
    {"code": "director", "name": "Director de cartera", "icon": "chart", "need": "¿Qué reviso primero?",
     "role": "Prioriza todos los expedientes y te dice cada mañana qué revisar primero."},
    {"code": "detector", "name": "Detector de anomalías", "icon": "alert", "need": "Avísame si algo no cuadra",
     "role": "Motor reutilizable: importes atípicos, duplicados, IVA atípico, pagos sin factura, facturas sin pago, proveedores nuevos, cambios de comportamiento, facturas que faltan y patrones interrumpidos."},
]
AGENTS_BY_CODE = {item["code"]: item for item in AGENTS}


def agents_overview(database: Session) -> dict[str, Any]:
    month_start = clock.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    rows = database.execute(
        select(AgentStep.agent, func.count(), func.avg(AgentStep.duration_ms), func.max(AgentStep.created_at))
        .where(AgentStep.created_at >= month_start)
        .group_by(AgentStep.agent)
    ).all()
    stats = {agent: {"steps": count, "avg_ms": int(avg or 0), "last": last.isoformat() if last else None} for agent, count, avg, last in rows}
    last_summaries = {}
    for agent in AGENTS_BY_CODE:
        step = database.scalar(select(AgentStep).where(AgentStep.agent == agent, AgentStep.status != "SKIPPED").order_by(AgentStep.id.desc()).limit(1))
        if step:
            last_summaries[agent] = step.summary
    runs = database.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.started_at >= month_start)) or 0

    from app.agents.orchestrator import AGENTS as INSTANCES
    from app.agents.orchestrator import routes_overview

    return {
        "routes": routes_overview(),
        "engine": llm.engine_label(),
        "ai_enabled": llm.available(),
        "runs_this_month": runs,
        "agents": [
            {
                **agent,
                **stats.get(agent["code"], {"steps": 0, "avg_ms": 0, "last": None}),
                "last_summary": last_summaries.get(agent["code"]),
                "contract": INSTANCES[agent["code"]].contract() if agent["code"] in INSTANCES else None,
            }
            for agent in AGENTS
        ],
    }
