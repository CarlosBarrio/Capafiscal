"""
Director: prioriza. Decide qué necesita a una persona, con qué urgencia, y
cada mañana resume «las cosas que deberías revisar hoy».
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import StepResult
from app.agents.knowledge import PROCEDURES
from app.models import Case
from app.models import OutboxMessage

OPEN_STATUSES = ("OPEN", "WAITING_HUMAN", "WAITING_DOCS", "READY_TO_FILE")
LEVEL_WORDS = {"critical": "Urgente", "high": "Revisión necesaria", "normal": "Para revisar", "low": "Informativo"}
PARENT = {
    "EMBARGO_CREDITOS": "EMBARGO",
    "EMBARGO_SALARIOS": "EMBARGO",
    "EMBARGO_CUENTAS": "EMBARGO",
    "COMPROBACION_LIMITADA": "REQUERIMIENTO",
    "VERIFICACION_DATOS": "REQUERIMIENTO",
    "REQUERIMIENTO_TERCEROS": "REQUERIMIENTO",
}


def score_case(case: Case, today: date) -> int:
    procedure = PARENT.get(case.procedure or "", case.procedure or "OTRO")
    if case.kind == "ANOMALY":
        base = int((case.facts or {}).get("severity_score", 30))
    else:
        base = PROCEDURES.get(procedure, PROCEDURES.get(case.procedure or "", PROCEDURES["OTRO"]))["base_score"]

    deadline = case.internal_deadline or case.deadline
    if deadline:
        days = (deadline - today).days
        urgency = 45 if days <= 2 else 35 if days <= 5 else 28 if days <= 10 else 22 if days <= 15 else 12 if days <= 30 else 5
    elif case.kind == "NOTIFICATION" and procedure not in {"COMUNICACION", "OTRO"}:
        urgency = 25  # plazo desconocido: urgente hasta confirmarlo
    else:
        urgency = 0

    facts = case.facts or {}
    retained = sum(item.get("total", 0) for item in facts.get("embargo_pending") or [])
    amount = float(case.amount or facts.get("amount") or retained or 0)
    money = min(15, int(math.log10(amount) * 4)) if amount > 10 else 0
    missing = sum(1 for item in (case.required_documents or []) if item.get("status") in {"missing", "partial", "requested"})
    blocking = 8 if missing else 0
    return max(0, min(100, base + urgency + money + blocking))


def level_for(score: int) -> str:
    return "critical" if score >= 80 else "high" if score >= 60 else "normal" if score >= 35 else "low"


def decide_status(database: Session, case: Case) -> str:
    if case.status in {"READY_TO_FILE", "FILED", "RESOLVED", "DISMISSED"}:
        return case.status
    pending = [item for item in case.requests if item.status == "PENDING"]
    if pending:
        sent = database.scalar(
            select(OutboxMessage.id).where(
                OutboxMessage.entity_type == "case_request",
                OutboxMessage.entity_id == case.id,
                OutboxMessage.status == "SENT",
            )
        )
        if sent:
            return "WAITING_DOCS"
    return "WAITING_HUMAN"


def headline_for(case: Case) -> str:
    who = case.subject_name or "Tu empresa"
    return f"{who} — {LEVEL_WORDS[case.level]}"


def prioritize(database: Session, case: Case, today: date) -> None:
    case.priority = max(case.priority if case.status == "WAITING_DOCS" else 0, score_case(case, today))
    case.level = level_for(case.priority)
    case.status = decide_status(database, case)
    case.headline = headline_for(case)


class Director(Agent):
    code = "director"
    name = "Director de cartera"
    role = "Prioriza el trabajo y te dice cada mañana qué revisar primero."
    icon = "chart"
    handles = ("notification", "invoice", "deadline")
    needs_case = False
    consumes = ("expediente trabajado", "hallazgos con riesgo y confianza", "plazos e importes")
    produces = ("prioridad 0-100", "nivel", "¿merece revisión humana?", "estado", "titular")

    def run(self, ctx: AgentContext) -> StepResult:
        case = ctx.case
        if case is None:
            # Nada que supere el umbral: no se molesta a nadie.
            low = [item for item in ctx.findings if item.agente == "detector"]
            ctx.facts["decision"] = "sin nada anómalo" if not low else f"{len(low)} aviso(s) de riesgo bajo, sin expediente"
            return StepResult(
                summary=("No merece revisión: " + ctx.facts["decision"] + ". Queda archivada y en la memoria.") if ctx.event and ctx.event.kind == "invoice" else "Nada que revisar.",
                output={"review": False, "decision": ctx.facts["decision"]},
            )
        severity = ctx.signals.get("severity")
        if case.kind == "ANOMALY" and severity:
            from app.agents.detector import SEVERITY

            # Riesgo del Detector + si además hay IVA en juego (Fiscal).
            score = SEVERITY[severity] + (10 if ctx.signals.get("tax_risk") else 0)
            ctx.facts.update({"severity": severity, "severity_score": score})
            case.facts = {**(case.facts or {}), "severity": severity, "severity_score": score}
        prioritize(ctx.database, case, ctx.today)
        days = (case.deadline - ctx.today).days if case.deadline else None
        status_text = {
            "WAITING_HUMAN": "espera tu revisión",
            "WAITING_DOCS": "espera documentación",
            "READY_TO_FILE": "listo para presentar",
        }.get(case.status, case.status.lower())
        return StepResult(
            summary=f"Prioridad {case.priority}/100 ({LEVEL_WORDS[case.level].lower()}); {status_text}"
            + (f"; vence en {days} días" if days is not None and days >= 0 else "; plazo vencido" if days is not None else "")
            + ".",
            output={"priority": case.priority, "level": case.level, "status": case.status, "headline": case.headline, "review": case.status == "WAITING_HUMAN"},
        )


def daily_briefing(database: Session, today: date | None = None, limit: int = 8) -> dict[str, Any]:
    """Las cosas que alguien debería revisar hoy, de todas las fuentes, en orden."""
    from app.agenda_service import build_agenda

    today = today or date.today()
    cases = database.scalars(select(Case).where(Case.status.in_(OPEN_STATUSES))).all()
    for case in cases:
        prioritize(database, case, today)

    items: list[dict[str, Any]] = []
    for case in cases:
        if case.status == "WAITING_DOCS" and case.level not in {"critical", "high"}:
            continue
        items.append(
            {
                "source": "case",
                "case_id": case.id,
                "title": case.title,
                "subtitle": case.headline,
                "detail": (case.summary or "")[:220],
                "level": case.level,
                "score": case.priority,
                "deadline": case.deadline.isoformat() if case.deadline else None,
                "status": case.status,
                "tab": "expedientes",
            }
        )

    level_score = {"overdue": 85, "critical": 75, "high": 55, "normal": 25}
    agenda = build_agenda(database, horizon_days=10, today=today)
    for entry in agenda["items"]:
        if entry["kind"] == "notification":
            continue  # ya está como expediente
        score = level_score.get(entry["level"], 20)
        items.append(
            {
                "source": entry["kind"],
                "case_id": None,
                "title": entry["title"],
                "subtitle": None,
                "detail": entry["detail"],
                "level": "critical" if score >= 80 else "high" if score >= 55 else "normal",
                "score": score,
                "deadline": entry["date"],
                "status": None,
                "tab": entry["tab"],
                "document_id": entry.get("document_id"),
            }
        )

    items.sort(key=lambda item: (-item["score"], item["deadline"] or "9999"))
    counts = {
        "open_cases": len(cases),
        "waiting_human": sum(1 for case in cases if case.status == "WAITING_HUMAN"),
        "waiting_docs": sum(1 for case in cases if case.status == "WAITING_DOCS"),
        "ready_to_file": sum(1 for case in cases if case.status == "READY_TO_FILE"),
        "critical": sum(1 for item in items if item["level"] == "critical"),
        "anomalies": sum(1 for case in cases if case.kind == "ANOMALY"),
    }
    top = items[:limit]
    urgent_top = sum(1 for item in top if item["level"] == "critical")
    if not top:
        headline = "Nada requiere tu atención hoy"
    elif len(top) == 1:
        headline = "Hoy deberías revisar 1 cosa" + (", urgente" if urgent_top else "")
    elif urgent_top == len(top):
        headline = f"Hoy deberías revisar {len(top)} cosas, todas urgentes"
    else:
        headline = f"Hoy deberías revisar {len(top)} cosas" + (f", {urgent_top} urgentes" if urgent_top > 1 else ", 1 urgente" if urgent_top else "")
    return {"date": today.isoformat(), "headline": headline, "counts": counts, "items": top, "total": len(items)}
