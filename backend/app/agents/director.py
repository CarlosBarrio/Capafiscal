"""
Director: prioriza. Decide qué necesita a una persona, con qué urgencia, y
cada mañana resume «las cosas que deberías revisar hoy».
"""
from __future__ import annotations

from app import clock
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


class Assessed:
    """El expediente con la prioridad de HOY calculada en memoria, sin tocar la fila.

    Las pantallas (GET) lo usan para no escribir: en SQLite dos lecturas que escriben a la vez
    acaban en «database is locked». La prioridad se guarda al trabajar el expediente y cada
    mañana con el Vigilante de plazos (refresh_priorities).
    """

    def __init__(self, database: Session, case: Case, today: date):
        self._case = case
        self.priority = max(case.priority if case.status == "WAITING_DOCS" else 0, score_case(case, today))
        self.level = level_for(self.priority)
        self.status = decide_status(database, case)
        self.headline = f"{case.subject_name or 'Tu empresa'} — {LEVEL_WORDS[self.level]}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._case, name)


def assessed_open_cases(database: Session, today: date) -> list[Any]:
    return [Assessed(database, case, today) for case in database.scalars(select(Case).where(Case.status.in_(OPEN_STATUSES))).all()]


def refresh_priorities(database: Session, today: date) -> int:
    """Guarda la prioridad del día (los plazos se acercan aunque nadie toque el expediente)."""
    cases = database.scalars(select(Case).where(Case.status.in_(OPEN_STATUSES))).all()
    for case in cases:
        prioritize(database, case, today)
    return len(cases)


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

    today = today or clock.today()
    cases = assessed_open_cases(database, today)

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


# ---------------------------------------------------------------------
# Centro operativo: la razón para abrir CapaFiscal por la mañana
# ---------------------------------------------------------------------

AUTO_RESOLVED_PREFIX = "Resuelta automáticamente"

# Minutos que le llevaría a una persona hacer a mano cada trabajo. Son una
# ESTIMACIÓN explícita (se muestran en pantalla) para ajustar con el piloto.
MINUTES_SAVED = {
    "document": (4, "leer, comprobar y registrar un documento"),
    "case_notification": (45, "preparar un expediente de notificación (análisis, documentación y escrito)"),
    "case_anomaly": (15, "investigar una factura o movimiento sospechoso"),
    "case_deadline": (30, "preparar un modelo (borrador, revisión de facturas y pasos)"),
    "request": (10, "pedir un documento y perseguirlo hasta recibirlo"),
    "duplicate": (2, "detectar y descartar un duplicado"),
    "auto_closed": (5, "revisar y cerrar un aviso que ya no aplica"),
}


def attention_reason(case: Case, today: date) -> str:
    """Una línea con el porqué: plazo, dato del hallazgo o bloqueo."""
    facts = case.facts or {}
    processing = facts.get("processing")
    if processing:
        return f"el agente {processing.get('agent_name') or processing.get('failed_agent')} no pudo terminar"
    def until(value: date) -> str:
        days = (value - today).days
        return "vencido" if days < 0 else "hoy" if days == 0 else "mañana" if days == 1 else f"en {days} días"

    if case.deadline:
        text = f"vence {until(case.deadline)}" if case.deadline >= today else "plazo vencido"
        if case.internal_deadline and case.internal_deadline < case.deadline and case.internal_deadline >= today:
            text += f" (objetivo interno: {until(case.internal_deadline)})"
        return text
    for finding in facts.get("findings") or []:
        data = finding.get("datos") or {}
        if finding.get("tipo") == "IMPORTE_ATIPICO" and data.get("ratio"):
            return f"{str(round(data['ratio'], 1)).replace('.', ',')} veces por encima de lo habitual"
        if finding.get("agente") == "detector":
            return finding.get("resultado") or ""
    if facts.get("severity_score"):
        ratio = facts.get("ratio") or (facts.get("median") and case.amount and float(case.amount) / float(facts["median"]))
        if ratio:
            return f"{str(round(float(ratio), 1)).replace('.', ',')} veces por encima de lo habitual"
    return (case.summary or "")[:120]


# Peso del tipo de asunto en el impacto: lo que tiene consecuencias legales o de dinero inmediato pesa más.
TYPE_WEIGHT = {
    "EMBARGO": 20, "EMBARGO_CREDITOS": 20, "EMBARGO_SALARIOS": 20, "EMBARGO_CUENTAS": 20, "APREMIO": 18, "SANCION": 16,
    "REQUERIMIENTO": 14, "COMPROBACION_LIMITADA": 14, "VERIFICACION_DATOS": 12, "PROPUESTA_LIQUIDACION": 14, "LIQUIDACION": 14,
}
TYPE_WHY = {
    "EMBARGO": "embargo: hay que retener desde ya", "APREMIO": "apremio: si no se paga, embargo", "SANCION": "sanción: plazo de alegaciones",
    "REQUERIMIENTO": "requerimiento: no atenderlo puede sancionarse", "PROPUESTA_LIQUIDACION": "propuesta de liquidación: último momento para alegar",
    "LIQUIDACION": "liquidación: pago en voluntaria",
}


def impact(case: Case, today: date) -> dict[str, Any]:
    """Cuánto importa un asunto hoy: urgencia + dinero en juego + tipo + bloqueo, con el porqué."""
    import math

    facts = case.facts or {}
    days_left = (case.deadline - today).days if case.deadline else None
    urgency = 10 if days_left is None else 50 if days_left <= 0 else 45 if days_left <= 2 else 35 if days_left <= 7 else 20 if days_left <= 15 else 5
    retention = (facts.get("embargo") or {}).get("retain_now")
    amount = float(retention) if retention else float(case.amount) if case.amount is not None else None
    money = min(30.0, 6 * math.log10(abs(amount) + 1)) if amount else 0.0
    procedure = case.procedure or ""
    family = next((name for name in TYPE_WEIGHT if procedure.startswith(name)), None)
    kind = TYPE_WEIGHT.get(family or "", 0) or {"high": 12, "medium": 6, "low": 2}.get(facts.get("severity") or "", 0) + (8 if case.kind == "DEADLINE" else 0)
    blocked = 10 if facts.get("processing") else 0
    score = round(urgency + money + kind + blocked)
    why = []
    if days_left is not None:
        why.append("plazo vencido" if days_left < 0 else "vence hoy" if days_left == 0 else f"vence en {days_left} {'día' if days_left == 1 else 'días'}")
    if amount:
        from app.agents.base import eur

        why.append(f"{eur(abs(amount))} en juego" if not retention else f"{eur(retention)} a retener")
    if family and TYPE_WHY.get(family.split("_")[0] if family.startswith("EMBARGO") else family):
        why.append(TYPE_WHY[family.split("_")[0] if family.startswith("EMBARGO") else family])
    elif case.kind == "ANOMALY" and facts.get("severity"):
        why.append(f"riesgo {({'high': 'alto', 'medium': 'medio', 'low': 'bajo'}).get(facts['severity'], facts['severity'])}")
    if blocked:
        why.append("un agente no pudo terminar")
    return {"score": score, "amount": amount, "days_left": days_left, "why": " · ".join(why) or (case.summary or "")[:120]}


def operational_board(database: Session, today: date | None = None, *, days: int = 7) -> dict[str, Any]:
    """Qué requiere atención, qué está pendiente y qué se ha resuelto solo."""
    from datetime import timedelta

    from sqlalchemy import func

    from app.models import DocumentRequest
    from app.models import IngestedEvent

    today = today or clock.today()
    since = clock.now() - timedelta(days=days)
    cases = assessed_open_cases(database, today)

    # 🔴 Requiere tu atención: lo urgente o importante que espera a una persona, y lo bloqueado.
    attention = []
    for case in sorted(cases, key=lambda item: -item.priority):
        blocked = bool((case.facts or {}).get("processing"))
        if case.status == "WAITING_HUMAN" and (case.level in {"critical", "high"} or blocked):
            attention.append({
                "case_id": case.id, "code": case.code, "title": case.title, "reason": attention_reason(case, today),
                "level": "critical" if blocked else case.level, "priority": case.priority, "kind": case.kind, "blocked": blocked,
            })
    failed_events = database.scalars(select(IngestedEvent).where(IngestedEvent.status == "FAILED").order_by(IngestedEvent.id.desc())).all()
    for event in failed_events[:5]:
        attention.append({
            "case_id": None, "event_id": event.id, "code": None, "title": f"No se pudo procesar una entrada ({event.source})",
            "reason": (event.error or "")[:120], "level": "critical", "priority": 100, "kind": "EVENT", "blocked": True,
        })

    # 🟠 Pendiente: avanza, pero no hoy necesariamente.
    requested = database.scalar(
        select(func.count()).select_from(DocumentRequest).join(Case, Case.id == DocumentRequest.case_id).where(DocumentRequest.status == "PENDING", Case.status.in_(OPEN_STATUSES))
    ) or 0
    blocked_cases = sum(1 for case in cases if (case.facts or {}).get("processing"))
    soon = today + timedelta(days=15)
    upcoming = sum(1 for case in cases if case.deadline and today <= case.deadline <= soon and case.level not in {"critical", "high"})
    to_review = sum(1 for case in cases if case.status == "WAITING_HUMAN" and case.level in {"normal", "low"} and not (case.facts or {}).get("processing"))
    ready = sum(1 for case in cases if case.status == "READY_TO_FILE")
    pending_items = [
        {"key": "requested", "count": requested, "label": "documento(s) solicitados sin recibir"},
        {"key": "blocked", "count": blocked_cases + len(failed_events), "label": "expediente(s) o entradas bloqueadas"},
        {"key": "upcoming", "count": upcoming, "label": "plazo(s) en los próximos 15 días"},
        {"key": "to_review", "count": to_review, "label": "expediente(s) para revisar sin prisa"},
        {"key": "ready", "count": ready, "label": "listo(s) para presentar"},
    ]

    # 🟢 Resuelto sin intervención: trabajo que no ha necesitado a nadie.
    no_case = database.scalar(
        select(func.count()).select_from(IngestedEvent).where(IngestedEvent.status == "COMPLETED", IngestedEvent.case_id.is_(None), IngestedEvent.updated_at >= since)
    ) or 0
    duplicates = database.scalar(select(func.coalesce(func.sum(IngestedEvent.duplicates), 0)).where(IngestedEvent.updated_at >= since)) or 0
    auto_closed = database.scalar(
        select(func.count()).select_from(Case).where(Case.status == "RESOLVED", Case.resolution.like(f"{AUTO_RESOLVED_PREFIX}%"), Case.resolved_at >= since)
    ) or 0
    received = database.scalar(
        select(func.count()).select_from(DocumentRequest).where(DocumentRequest.status == "RECEIVED", DocumentRequest.received_at >= since)
    ) or 0
    prepared = database.scalar(
        select(func.count()).select_from(IngestedEvent).where(IngestedEvent.status == "COMPLETED", IngestedEvent.case_id.is_not(None), IngestedEvent.updated_at >= since)
    ) or 0
    resolved_items = [
        {"key": "no_case", "count": no_case, "label": "documento(s) revisados por los agentes sin nada que objetar"},
        {"key": "prepared", "count": prepared, "label": "expediente(s) preparados de principio a fin"},
        {"key": "received", "count": received, "label": "documento(s) conseguidos por el Perseguidor"},
        {"key": "auto_closed", "count": auto_closed, "label": "aviso(s) cerrados solos al desaparecer el problema"},
        {"key": "duplicates", "count": int(duplicates), "label": "entrada(s) repetidas ignoradas"},
    ]

    # Trabajo realizado e intervención humana en el periodo (entradas registradas).
    from app.models import AuditEvent

    events = database.scalars(select(IngestedEvent).where(IngestedEvent.created_at >= since)).all()
    ai_documents = {
        str(value) for value in database.scalars(
            select(AuditEvent.entity_id).where(AuditEvent.action == "document.interpretation", AuditEvent.created_at >= since)
        ).all()
    }
    solo = with_ai = human = failed = 0
    for event in events:
        used_ai = str((event.payload or {}).get("document_id")) in ai_documents
        if event.status == "FAILED":
            failed += 1
        elif event.status == "NEEDS_HUMAN" or event.case_id is not None:
            human += 1
        elif used_ai:
            with_ai += 1
        else:
            solo += 1
    handled = len(events)
    new_cases = database.scalars(select(Case).where(Case.created_at >= since)).all()
    requests_created = database.scalar(select(func.count()).select_from(DocumentRequest).where(DocumentRequest.created_at >= since)) or 0
    documents = sum(1 for event in events if event.kind in {"document", "invoice"})
    work = {
        "events": handled,
        "documents": documents,
        "cases": len(new_cases),
        "anomalies": sum(1 for case in new_cases if case.kind == "ANOMALY"),
        "deadlines": sum(1 for case in new_cases if case.kind == "DEADLINE"),
        "requests": requests_created,
    }
    intervention = {
        "total": handled,
        "solo": solo,
        "with_ai": with_ai,
        "human": human,
        "failed": failed,
        "human_rate": round((human + failed) / handled, 3) if handled else None,
        "automatic_rate": round((solo + with_ai) / handled, 3) if handled else None,
    }
    units = {
        "document": documents,
        "case_notification": sum(1 for case in new_cases if case.kind == "NOTIFICATION"),
        "case_anomaly": work["anomalies"],
        "case_deadline": work["deadlines"],
        "request": requests_created,
        "duplicate": int(duplicates),
        "auto_closed": auto_closed,
    }
    minutes = sum(units[key] * MINUTES_SAVED[key][0] for key in units)
    time_saved = {
        "minutes": minutes,
        "hours": round(minutes / 60, 1),
        "assumptions": [{"key": key, "minutes": MINUTES_SAVED[key][0], "what": MINUTES_SAVED[key][1], "count": units[key]} for key in units if units[key]],
        "note": "Estimación con tiempos supuestos por tarea; se ajustará con datos del piloto.",
    }

    # Lo que más impacto tiene hoy, con el porqué (urgencia, dinero, tipo de asunto, bloqueos).
    by_id = {case.id: case for case in cases}
    for item in attention:
        if item.get("case_id") in by_id:
            item["impact"] = impact(by_id[item["case_id"]], today)
        else:
            item["impact"] = {"score": 100, "amount": None, "days_left": None, "why": "entrada que no se pudo procesar"}
    attention.sort(key=lambda item: -item["impact"]["score"])
    candidates = [
        {"case_id": case.id, "code": case.code, "title": case.title, "reason": attention_reason(case, today), "level": case.level,
         "priority": case.priority, "kind": case.kind, "blocked": bool((case.facts or {}).get("processing")), "impact": impact(case, today)}
        for case in cases if case.status in {"WAITING_HUMAN", "READY_TO_FILE"}
    ]
    ranked = sorted(attention + [item for item in candidates if not any(other.get("case_id") == item["case_id"] for other in attention)],
                    key=lambda item: -item["impact"]["score"])
    top = ranked[:3]

    return {
        "date": today.isoformat(),
        "period_days": days,
        "attention": {"count": len(attention), "items": attention},
        "pending": {"count": sum(item["count"] for item in pending_items), "items": [item for item in pending_items if item["count"]]},
        "resolved": {"count": sum(item["count"] for item in resolved_items), "items": [item for item in resolved_items if item["count"]]},
        "top": top,
        "fiscal": fiscal_snapshot(database, today),
        "bank": bank_snapshot(database, today),
        "work": work,
        "intervention": intervention,
        "time_saved": time_saved,
    }


def fiscal_snapshot(database: Session, today: date) -> list[dict[str, Any]]:
    """Cómo va el trimestre: una línea por modelo."""
    from app.fiscal_position import positions

    try:
        report = positions(database, today=today)
    except Exception:  # la foto fiscal no debe tumbar el panel
        return []
    return [{"model": item["model"], "period": item["period_label"], "status": item["status"], "headline": item["headline"], "summary": item["summary"],
             "due_date": item["due_date"], "days_left": item["days_left"], "information_available": item["information_available"]} for item in report["models"]]


def bank_snapshot(database: Session, today: date) -> dict[str, Any] | None:
    from app.models import BankTransaction
    from app.reconciliation import reconcile

    if database.scalar(select(BankTransaction.id).limit(1)) is None:
        return None
    report = reconcile(database, today=today, auto=False, persist=False)
    return {"counts": report["counts"], "levels": report["levels"], "reconciled_rate": report["reconciled_rate"], "total": report["total"]}

