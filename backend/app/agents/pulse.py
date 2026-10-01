"""
Ejecución continua: CapaFiscal trabaja aunque nadie suba nada.

Tres tipos de disparador, todos por la misma entrada única y el mismo
orquestador:

    EXTERNO     nueva factura, notificación o correo  → buzón, subidas, API
    TEMPORAL    se acerca un plazo, pasan 3 días sin  → vigilante de plazos,
                documentación                            perseguidor
    ANALÍTICO   anomalía, factura que falta, cambio    → detector (y, si es
                de comportamiento                        grave, investigación
                                                         completa del orquestador)

El «pulso» resume si la máquina está en marcha, cuándo trabajó por última vez
y qué hizo cada tipo de disparador.
"""
from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

TRIGGERS: dict[str, dict[str, Any]] = {
    "externo": {"label": "Eventos externos", "jobs": ("EMAIL_INBOX", "AGENT_PIPELINE"), "sources": ("upload", "email", "dehu", "api", "pendientes")},
    "temporal": {"label": "Eventos temporales", "jobs": ("DEADLINE_WATCH", "FOLLOW_UP"), "sources": ("calendario",)},
    "analitico": {"label": "Eventos analíticos", "jobs": ("ANOMALY_SCAN",), "sources": ("detector",)},
}
CYCLE_ORDER = ("EMAIL_INBOX", "AGENT_PIPELINE", "DEADLINE_WATCH", "FOLLOW_UP", "ANOMALY_SCAN")
HEALTHY_MINUTES = 15


def escalate_analytic_findings(database: Session, today: date) -> list[dict[str, Any]]:
    """Hallazgos graves del barrido sobre una factura → investigación completa del orquestador.

    El barrido abre avisos sueltos; si el riesgo es alto y hay una factura
    detrás, se trata como un evento más (fuente «detector») para que Memoria,
    Fiscal y Gestor lo trabajen y el Director lo priorice.
    """
    from app.agents.detector import scan
    from app.agents.intake import ingest
    from app.models import Case

    escalated = []
    for item in scan(database, today):
        invoice_id = item["facts"].get("invoice_id")
        if item["severity"] != "high" or not invoice_id:
            continue
        if database.scalar(select(Case.id).where(Case.fingerprint.in_([item["fingerprint"], f"factura:{invoice_id}"]))):
            continue  # ya avisado o ya investigado (o descartado por una persona)
        event, duplicate, case = ingest(
            database, source="detector", external_id=f"analitico:{item['fingerprint']}", kind="invoice",
            payload={"invoice_id": invoice_id, "finding": item["procedure"]}, trigger="schedule", today=today,
        )
        if not duplicate:
            escalated.append({"fingerprint": item["fingerprint"], "event_id": event.id, "case_code": case.code if case else None})
    return escalated


def run_cycle(database: Session, *, now: datetime | None = None, actor: str = "user") -> dict[str, Any]:
    """«Trabajar ahora»: un ciclo completo de los tres disparadores, en orden."""
    from app.automation_service import run_automation

    runs = []
    for code in CYCLE_ORDER:
        run = run_automation(database, code, trigger="MANUAL", now=now, actor=actor)
        runs.append({"code": code, "status": run.status, "items": run.items, "summary": run.summary})
    return {"runs": runs, "items": sum(item["items"] or 0 for item in runs)}


def pulse_status(database: Session, *, now: datetime | None = None) -> dict[str, Any]:
    """¿Está CapaFiscal trabajando? Último ciclo y qué hizo cada disparador hoy."""
    from app.config import settings
    from app.models import AutomationSetting
    from app.models import IngestedEvent

    now = now or datetime.now(timezone.utc)
    settings_by_code = {item.code: item for item in database.scalars(select(AutomationSetting)).all()}
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def aware(value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None

    last_runs = [aware(item.last_run_at) for code, item in settings_by_code.items() if code in CYCLE_ORDER and item.last_run_at]
    last = max(last_runs) if last_runs else None
    triggers = []
    for key, trigger in TRIGGERS.items():
        events = database.scalar(
            select(func.count()).select_from(IngestedEvent).where(IngestedEvent.source.in_(trigger["sources"]), IngestedEvent.created_at >= day_start)
        ) or 0
        jobs = []
        for code in trigger["jobs"]:
            setting = settings_by_code.get(code)
            jobs.append({"code": code, "last_run_at": aware(setting.last_run_at).isoformat() if setting and setting.last_run_at else None, "last_summary": setting.last_summary if setting else None, "enabled": setting.enabled if setting else True})
        triggers.append({"key": key, "label": trigger["label"], "events_today": events, "jobs": jobs})

    minutes = int((now - last).total_seconds() // 60) if last else None
    healthy = bool(settings.enable_scheduler and minutes is not None and minutes <= HEALTHY_MINUTES)
    if not settings.enable_scheduler:
        label = "Trabajo automático desactivado (ENABLE_SCHEDULER=false)"
    elif minutes is None:
        label = "Arrancando: aún no ha completado ningún ciclo"
    elif minutes < 1:
        label = "CapaFiscal está trabajando · último ciclo hace menos de 1 min"
    else:
        label = f"CapaFiscal está trabajando · último ciclo hace {minutes} min" if healthy else f"Sin actividad automática desde hace {minutes} min"
    return {"healthy": healthy, "label": label, "last_cycle_at": last.isoformat() if last else None, "minutes_since": minutes, "scheduler": settings.enable_scheduler, "triggers": triggers}

