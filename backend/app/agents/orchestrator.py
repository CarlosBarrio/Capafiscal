"""
Orquestador: ha ocurrido algo → se abre (o no) un expediente → el
expediente decide qué agentes necesita.

    FUENTE → Vigilante → Expedientes → ¿qué ruta? → agentes → Director → HUMANO

1. Entrada (siempre): Vigilante y Expedientes entienden el evento.
2. Decisión inicial: ``choose_route`` elige la ruta según el tipo de caso.
3. Encadenado por resultados: tras cada paso, ``CHAIN_RULES`` miran las
   señales (por ejemplo, el Detector dice ``anomaly: true, severity: high``)
   y añaden los agentes que hagan falta antes del Director.

Cada recorrido queda registrado (AgentRun + AgentStep, con la ruta y por
qué se añadió cada agente) y el expediente recibe la línea de tiempo. El
humano recibe un único aviso con todo trabajado y un «Revisar y aprobar».
"""
from __future__ import annotations

from app import clock
from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.base import Agent
from app.agents.base import AgentContext
from app.agents.base import Event
from app.agents.base import jsonable
from app.agents.base import run_step
from app.agents.detector import DetectorAnomalias
from app.agents.director import Director
from app.agents.expedientes import ClasificadorExpedientes
from app.agents.fiscal import AgenteFiscal
from app.agents.gestor import GestorIncidencias
from app.agents.memoria import AgenteMemoria
from app.agents.perseguidor import Perseguidor
from app.agents.vigilante import Vigilante
from app.invoice_service import add_audit_event
from app.models import AgentRun
from app.models import Case
from app.models import CaseEvent
from app.models import FiscalNotification
from app.models import Invoice

AGENTS: dict[str, Agent] = {
    agent.code: agent
    for agent in (
        Vigilante(),
        ClasificadorExpedientes(),
        AgenteFiscal(),
        AgenteMemoria(),
        DetectorAnomalias(),
        GestorIncidencias(),
        Perseguidor(),
        Director(),
    )
}

INTAKE = ("vigilante", "expedientes")

# Rutas por tipo de caso: los agentes que se ejecutan después de la entrada.
ROUTES: dict[str, dict[str, Any]] = {
    "requerimiento": {
        "label": "Requerimiento o acto administrativo",
        "event": "notification",
        "steps": ("fiscal", "memoria", "gestor", "perseguidor", "director"),
    },
    "embargo": {
        "label": "Embargo",
        "event": "notification",
        "steps": ("memoria", "gestor", "director"),
    },
    "documento_normal": {
        "label": "Documento normal",
        "event": "invoice",
        "steps": ("detector", "director"),
    },
    "factura_sospechosa": {
        "label": "Factura sospechosa",
        "event": "invoice",
        "steps": ("memoria", "detector", "fiscal", "gestor", "director"),
        # Fiscal y Gestor solo trabajan si el Detector confirma la sospecha.
        "only_if_anomaly": ("fiscal", "gestor"),
    },
    "plazo": {
        "label": "Plazo de presentación",
        "event": "deadline",
        "steps": ("fiscal", "memoria", "detector", "gestor", "director"),
    },
}

# Sin expediente, estos agentes no tienen sobre qué trabajar.
NEEDS_CASE = {code for code, agent in AGENTS.items() if agent.needs_case}
NEEDS_CASE_LEGACY = {"fiscal", "memoria", "gestor", "perseguidor", "director"}  # notificaciones


def choose_route(ctx: AgentContext) -> tuple[str, str]:
    """La decisión inicial: qué agentes hacen falta para este caso."""
    event = ctx.event
    if event.kind == "notification":
        if ctx.facts.get("procedure") == "EMBARGO":
            return "embargo", "Es un embargo: hay que saber qué se le debe al embargado y contestar."
        return "requerimiento", f"Es {(ctx.facts.get('type_label') or 'un acto administrativo').lower()}: hay que ver a qué afecta, qué piden y preparar la respuesta."
    if event.kind == "invoice":
        hints = ctx.facts.get("triage") or []
        if hints:
            return "factura_sospechosa", "Señales al llegar: " + "; ".join(hints) + "."
        return "documento_normal", "Factura sin señales previas: basta con pasarla por el Detector."
    if event.kind == "deadline":
        return "plazo", "Se acerca un plazo: hay que preparar el modelo y revisar que no falte nada."
    raise ValueError(f"Evento sin ruta: {event.kind}")


# ---------------------------------------------------------------------
# Encadenado por resultados
# ---------------------------------------------------------------------


def rule_anomaly(ctx: AgentContext, agent_code: str) -> tuple[list[str], str] | None:
    if agent_code != "detector" or not ctx.signals.get("anomaly"):
        return None
    severity = {"high": "alta", "medium": "media", "low": "baja"}.get(ctx.signals.get("severity") or "", "")
    return ["memoria", "fiscal", "gestor"], f"El Detector encontró una anomalía (gravedad {severity}): hace falta contexto, impacto fiscal y qué hacer."


def rule_missing_documents(ctx: AgentContext, agent_code: str) -> tuple[list[str], str] | None:
    if agent_code != "gestor" or not ctx.signals.get("missing_documents"):
        return None
    return ["perseguidor"], f"Faltan {ctx.signals['missing_documents']} documento(s) que no puede preparar el sistema: hay que pedirlos."


CHAIN_RULES: tuple[Callable[[AgentContext, str], tuple[list[str], str] | None], ...] = (rule_anomaly, rule_missing_documents)


# ---------------------------------------------------------------------
# Recorrido
# ---------------------------------------------------------------------


def find_case(database: Session, event: Event) -> Case | None:
    if event.kind == "notification":
        return database.scalar(select(Case).where(Case.notification_id == event.ref_id))
    if event.kind == "invoice":
        return database.scalar(select(Case).where(Case.fingerprint == f"factura:{event.ref_id}"))
    if event.kind == "deadline":
        payload = event.payload
        return database.scalar(select(Case).where(Case.fingerprint == f"plazo:{payload['model']}:{payload['year']}-{payload['quarter']}"))
    return None


def backfill_case_events(ctx: AgentContext, run: AgentRun, *, until: int) -> None:
    """Si el expediente se abre a mitad del recorrido, recibe lo ya hecho."""
    from app.agents.registry import AGENTS_BY_CODE
    from app.models import AgentStep

    ctx.database.flush()
    steps = ctx.database.scalars(
        select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.position <= until).order_by(AgentStep.position)
    ).all()
    for step in steps:
        if step.status == "SKIPPED":
            continue
        name = AGENTS_BY_CODE.get(step.agent, {}).get("name", step.agent)
        ctx.database.add(
            CaseEvent(
                case_id=ctx.case.id,
                kind="agent",
                actor=step.agent,
                title=f"{name} · {step.summary}"[:255],
                data={"run_id": run.id, "status": step.status, "engine": step.engine, "duration_ms": step.duration_ms},
            )
        )


def process_event(database: Session, event: Event, *, trigger: str = "system", today: date | None = None, holder: dict[str, Any] | None = None) -> Case | None:
    now = clock.now()
    ctx = AgentContext(database=database, today=today or clock.today(), now=now, trigger=trigger, event=event)
    if event.kind == "notification":
        notification = database.get(FiscalNotification, event.ref_id)
        if notification is None:
            return None
        ctx.facts["notification"] = notification
    elif event.kind == "invoice":
        invoice = database.get(Invoice, event.ref_id)
        if invoice is None:
            return None
        ctx.facts["invoice_obj"] = invoice
    ctx.case = find_case(database, event)

    run = AgentRun(pipeline=event.kind, trigger=trigger, case_id=ctx.case.id if ctx.case else None, status="RUNNING")
    database.add(run)
    database.flush()
    if holder is not None:
        holder["run"] = run

    position = 0
    errors = 0
    executed: list[str] = []
    chained: list[dict[str, Any]] = []
    skipped: list[str] = []
    escalated: dict[str, Any] | None = None

    def execute(code: str) -> None:
        nonlocal position, errors
        agent = AGENTS[code]
        needs_case = code in NEEDS_CASE or (event.kind == "notification" and code in NEEDS_CASE_LEGACY)
        if needs_case and ctx.case is None:
            skipped.append(code)
            return
        had_case = ctx.case is not None
        position += 1
        result = run_step(ctx, run, agent, position)
        executed.append(code)
        if result.status == "ERROR":
            errors += 1
        if not had_case and ctx.case is not None:
            run.case_id = ctx.case.id
            backfill_case_events(ctx, run, until=position - 1)

    for code in INTAKE:
        execute(code)

    route, reason = choose_route(ctx)
    ctx.route = route
    plan = list(ROUTES[route]["steps"])
    conditional = set(ROUTES[route].get("only_if_anomaly", ()))

    while plan:
        code = plan.pop(0)
        if code in conditional and not ctx.signals.get("anomaly"):
            skipped.append(code)
            continue
        execute(code)

        for rule in CHAIN_RULES:
            outcome = rule(ctx, code)
            if not outcome:
                continue
            wanted, why = outcome
            if event.kind in {"invoice", "deadline"} and ctx.case is None and code == "detector":
                open_event_case(ctx, run, until=position)
            if rule is rule_anomaly and route == "documento_normal":
                # Parecía normal, pero el Detector dice lo contrario: cambia de ruta.
                escalated = {"from": route, "to": "factura_sospechosa", "reason": why}
                route = ctx.route = "factura_sospechosa"
            added = []
            for extra in wanted:
                if extra in executed or extra in plan:
                    continue  # ya hecho o ya previsto en la ruta
                director_at = plan.index("director") if "director" in plan else len(plan)
                plan.insert(director_at, extra)
                added.append(extra)
            if added:
                chained.append({"after": code, "added": added, "reason": why})

    case = ctx.case
    run.status = "OK" if not errors else "PARTIAL"
    run.finished_at = clock.now()
    route_info = {
        "code": route,
        "label": ROUTES[route]["label"],
        "reason": reason,
        "event": event.kind,
        "source": event.source,
        "agents": executed,
        "chained": chained,
        "escalated": escalated,
        "skipped": [code for code in skipped if code not in executed],
    }

    if case is None:
        run.summary = f"{ROUTES[route]['label']}: sin expediente ({ctx.facts.get('decision') or 'nada que revisar'})"
        database.add(run)
        database.flush()
        return None

    run.summary = case.headline
    facts = {key: value for key, value in ctx.facts.items() if key not in {"notification", "invoice_obj", "llm_extraction", "anomalies"}}
    facts["route"] = route_info
    facts["findings"] = [item.to_dict() for item in ctx.findings]
    if event.kind == "invoice":
        invoice = ctx.facts["invoice_obj"]
        facts["supplier_key"] = invoice.supplier_tax_id or (invoice.supplier_name or "").strip().upper() or None
        facts["finding_types"] = sorted({item.tipo for item in ctx.findings if item.agente == "detector"})
    previous = case.facts or {}
    for key in ("human_decision", "origin", "severity", "severity_score"):
        if key in previous and key not in facts:
            facts[key] = previous[key]
    case.facts = jsonable(facts)
    if not case.antecedents and ctx.facts.get("antecedents"):
        case.antecedents = jsonable(ctx.facts["antecedents"])

    elapsed = int((run.finished_at - run.started_at.replace(tzinfo=run.started_at.tzinfo or timezone.utc)).total_seconds() * 1000)
    database.add(
        CaseEvent(
            case_id=case.id,
            kind="system",
            actor="orquestador",
            title=f"Ruta «{ROUTES[route]['label']}»: {len(executed)} agentes en {elapsed} ms"
            + (f", {sum(len(item['added']) for item in chained)} añadidos por sus resultados" if chained else "")
            + (f" ({errors} con incidencias)" if errors else ""),
            detail=reason,
            data={"run_id": run.id, "route": route, "chained": chained},
        )
    )
    notification = ctx.facts.get("notification")
    if notification is not None and notification.status == "PENDING":
        notification.status = "IN_PROGRESS"
    add_audit_event(
        database,
        action=f"agents.{event.kind}_processed",
        entity_type="case",
        entity_id=case.id,
        actor="agent",
        event_data={"run_id": run.id, "code": case.code, "route": route, "priority": case.priority, "status": case.status},
    )
    database.flush()
    return case


def open_event_case(ctx: AgentContext, run: AgentRun, *, until: int) -> None:
    """El Detector ha confirmado la sospecha: Expedientes abre el expediente."""
    from app.agents.expedientes import open_case_for_event

    open_case_for_event(ctx, announce=False)
    run.case_id = ctx.case.id
    backfill_case_events(ctx, run, until=until)
    ctx.database.add(
        CaseEvent(case_id=ctx.case.id, kind="agent", actor="expedientes", title=f"Expedientes · Abierto {ctx.case.code}: {ctx.case.title}"[:255], data={"run_id": run.id, "reason": "anomalía confirmada"})
    )


# ---------------------------------------------------------------------
# Entradas
# ---------------------------------------------------------------------


def process_notification(database: Session, notification_id: int, *, trigger: str = "system", today: date | None = None, holder: dict[str, Any] | None = None) -> Case | None:
    notification = database.get(FiscalNotification, notification_id)
    if notification is None:
        return None
    from app.models import Document

    document = database.get(Document, notification.document_id) if notification.document_id else None
    source = (document.source if document else None) or "manual"
    return process_event(database, Event("notification", source=source or trigger, ref_id=notification_id), trigger=trigger, today=today, holder=holder)


def process_invoice(database: Session, invoice_id: int, *, trigger: str = "system", today: date | None = None, holder: dict[str, Any] | None = None, source: str | None = None) -> Case | None:
    invoice = database.get(Invoice, invoice_id)
    if invoice is None or invoice.direction == "ISSUED":
        return None
    return process_event(database, Event("invoice", source=source or trigger, ref_id=invoice_id), trigger=trigger, today=today, holder=holder)


def process_deadline(database: Session, *, model: str, year: int, quarter: int, due: date, trigger: str = "schedule", today: date | None = None, holder: dict[str, Any] | None = None) -> Case | None:
    event = Event("deadline", source="calendario", payload={"model": model, "year": year, "quarter": quarter, "due": due.isoformat()})
    return process_event(database, event, trigger=trigger, today=today, holder=holder)


def rerun_case(database: Session, case: Case) -> Case | None:
    facts = case.facts or {}
    if case.notification_id:
        return process_notification(database, case.notification_id, trigger="manual")
    if (case.fingerprint or "").startswith("factura:"):
        return process_invoice(database, int(case.fingerprint.split(":")[1]), trigger="manual")
    if (case.fingerprint or "").startswith("plazo:") and facts.get("period"):
        period = facts["period"]
        return process_deadline(database, model=period["model"], year=period["year"], quarter=period["quarter"], due=date.fromisoformat(period["due"]), trigger="manual")
    return None


DEADLINE_MODELS = {"303", "130", "111", "115"}
DEADLINE_LEAD_DAYS = 15


def watch_deadlines(database: Session, *, trigger: str = "schedule", today: date | None = None) -> list[Case]:
    """Tercer tipo de entrada: plazos que se acercan crean su expediente."""
    from app.tax_service import build_tax_calendar

    today = today or clock.today()
    entries = []
    for year in [today.year] + ([today.year + 1] if today.month == 12 else []):
        entries += build_tax_calendar(database, year=year, today=today)["entries"]
    cases = []
    for entry in entries:
        if entry["model"] not in DEADLINE_MODELS or not entry["period"] or entry["status"] not in {"UPCOMING", "DUE_SOON"}:
            continue
        if not 0 <= entry["days_left"] <= DEADLINE_LEAD_DAYS:
            continue
        existing = find_case(database, Event("deadline", payload={"model": entry["model"], "year": entry["period_year"], "quarter": entry["period"]}))
        if existing is not None:
            continue
        from app.agents.intake import ingest

        _event, _duplicate, case = ingest(
            database, source="calendario", external_id=f"{entry['model']}:{entry['period_year']}-{entry['period']}", kind="deadline",
            payload={"model": entry["model"], "year": entry["period_year"], "quarter": entry["period"], "due": entry["due_date"]},
            trigger=trigger, today=today,
        )
        if case:
            cases.append(case)
    return cases


def process_pending(database: Session, *, trigger: str = "schedule", today: date | None = None) -> list[Case]:
    """Notificaciones abiertas que aún no tienen expediente (backlog o importadas)."""
    handled = select(Case.notification_id).where(Case.notification_id.is_not(None))
    pending = database.scalars(
        select(FiscalNotification).where(
            FiscalNotification.status.in_(["PENDING", "IN_PROGRESS"]),
            FiscalNotification.id.notin_(handled),
        )
    ).all()
    from app.agents.intake import ingest

    cases = []
    for notification in pending:
        _event, _duplicate, case = ingest(
            database, source="pendientes", external_id=f"notification:{notification.id}", kind="notification",
            payload={"notification_id": notification.id}, force=True, trigger=trigger, today=today,
        )
        if case:
            cases.append(case)
    return cases


def routes_overview() -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "label": route["label"],
            "event": route["event"],
            "steps": [*INTAKE, *route["steps"]],
            "only_if_anomaly": list(route.get("only_if_anomaly", ())),
        }
        for code, route in ROUTES.items()
    ]


def serialize_run(run: AgentRun) -> dict[str, Any]:
    from app.agents.registry import AGENTS_BY_CODE

    return {
        "id": run.id,
        "pipeline": run.pipeline,
        "trigger": run.trigger,
        "case_id": run.case_id,
        "status": run.status,
        "summary": run.summary,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "steps": [
            {
                "position": step.position,
                "agent": step.agent,
                "agent_name": AGENTS_BY_CODE.get(step.agent, {}).get("name", step.agent),
                "icon": AGENTS_BY_CODE.get(step.agent, {}).get("icon", "sparkles"),
                "status": step.status,
                "summary": step.summary,
                "engine": step.engine,
                "duration_ms": step.duration_ms,
                "evidence": step.evidence,
                "output": step.output,
                "created_at": step.created_at.isoformat() if step.created_at else None,
            }
            for step in run.steps
        ],
    }

