"""
Automatizaciones del agente: el trabajo que se hace solo, cada día, sin que
nadie tenga que acordarse.

Cada automatización tiene su horario, se puede activar o desactivar y
ejecutar a mano, y deja constancia de cada ejecución (qué hizo y cuántos
elementos tocó), que alimenta «Tu agente este mes».

El planificador es un hilo ligero dentro del propio servidor: comprueba cada
minuto qué toca. En producción se sustituiría por Celery beat o similar sin
cambiar las tareas.
"""
from __future__ import annotations

from app import clock
import logging
import threading
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Callable

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.invoice_service import add_audit_event
from app.models import AutomationRun
from app.models import AutomationSetting
from app.models import CompanyProfile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Automation:
    code: str
    name: str
    description: str
    schedule: str
    hour: int
    minute: int
    # "daily", "monthly" (desde el día `day`), "quarterly" (mes siguiente al trimestre)
    cadence: str
    handler: Callable[[Session, datetime], tuple[int, str]]
    minutes_per_item: int
    area: str
    tab: str
    day: int = 1
    default_enabled: bool = True


# ---------------------------------------------------------------------
# Tareas
# ---------------------------------------------------------------------


def job_recurring(database: Session, now: datetime) -> tuple[int, str]:
    from app.sales_service import generate_due_recurring
    from app.outbox_service import prepare_invoice_email
    from app.models import RecurringInvoice

    created = generate_due_recurring(database, today=now.date(), actor="agent")
    issued = [item for item in created if item.status == "ISSUED"]
    prepared = 0
    for invoice in issued:
        template = database.get(RecurringInvoice, invoice.recurring_id) if invoice.recurring_id else None
        if template and template.auto_send:
            prepare_invoice_email(database, invoice, created_by="agent")
            prepared += 1

    if not created:
        return 0, "No tocaba ninguna factura recurrente."
    parts = [f"{len(created)} factura(s) generada(s)"]
    if issued:
        parts.append(f"{len(issued)} emitida(s): " + ", ".join(item.code for item in issued))
    if len(created) > len(issued):
        parts.append(f"{len(created) - len(issued)} en borrador para revisar")
    if prepared:
        parts.append(f"{prepared} envío(s) preparado(s)")
    return len(created), " · ".join(parts) + "."


def job_dunning(database: Session, now: datetime) -> tuple[int, str]:
    from app.dunning_service import prepare_reminders

    created = prepare_reminders(database, today=now.date(), created_by="agent")
    if not created:
        return 0, "Ninguna factura vencida necesita reclamación nueva."
    without_email = sum(1 for item in created if not item.to_email)
    summary = f"{len(created)} reclamación(es) preparada(s) en la bandeja de salida"
    if without_email:
        summary += f" ({without_email} sin email del cliente)"
    return len(created), summary + "."


def job_bank(database: Session, now: datetime) -> tuple[int, str]:
    from app.bank_service import suggest_matches
    from app.reconciliation import reconcile

    suggest_matches(database)
    report = reconcile(database, actor="conciliacion-automatica")
    pending = report["counts"].get("POSIBLE", 0)
    exceptions = sum(report["counts"].get(state, 0) for state in ("IMPORTE_DISTINTO", "DUPLICADO", "SIN_FACTURA", "FACTURA_SIN_PAGO"))
    if not (report["auto_matched"] or pending or exceptions):
        return 0, "Sin movimientos nuevos que conciliar."
    return report["auto_matched"] + pending, (
        f"{report['auto_matched']} conciliado(s) automáticamente, {pending} propuesta(s) para confirmar y {exceptions} excepción(es) para el Detector."
    )


def job_payroll(database: Session, now: datetime) -> tuple[int, str]:
    from app.models import PayrollRun
    from app.payroll_service import active_employees
    from app.payroll_service import create_run

    year, month = now.year, now.month
    if database.scalar(select(PayrollRun).where(PayrollRun.year == year, PayrollRun.month == month)):
        return 0, "La nómina del mes ya está preparada."
    if not active_employees(database, year, month):
        return 0, "No hay personas en plantilla este mes."
    run = create_run(database, year=year, month=month)
    return len(run.payslips), f"Borrador de nómina de {month:02d}/{year} con {len(run.payslips)} recibo(s): revisa variables y apruébalo."


def job_timesheet(database: Session, now: datetime) -> tuple[int, str]:
    from app.timesheet_service import timesheet_alerts

    alerts = timesheet_alerts(database, now=now.replace(tzinfo=None))
    if not alerts:
        return 0, "Registro de jornada al día."
    labels = {
        "forgotten": "fichaje(s) sin salida",
        "long_day": "jornada(s) de más de 9 h",
        "short_rest": "descanso(s) entre jornadas inferiores a 12 h",
        "missing": "persona(s) con días laborables sin registro",
    }
    kinds: dict[str, int] = {}
    for alert in alerts:
        kinds[alert["kind"]] = kinds.get(alert["kind"], 0) + 1
    return len(alerts), "Detectado: " + ", ".join(f"{count} {labels[kind]}" for kind, count in kinds.items()) + "."


def job_advisor(database: Session, now: datetime) -> tuple[int, str]:
    from app.advisor_service import prepare_advisor_email
    from app.models import OutboxMessage

    company = database.scalar(select(CompanyProfile).limit(1))
    if not company or not company.advisor_email:
        return 0, "Sin email de gestoría en «Mi empresa»: no se prepara el envío."
    quarter = (now.month - 1) // 3  # trimestre recién cerrado
    year = now.year if quarter else now.year - 1
    quarter = quarter or 4
    key = year * 10 + quarter
    if database.scalar(
        select(OutboxMessage.id).where(OutboxMessage.entity_type == "advisor_pack", OutboxMessage.entity_id == key, OutboxMessage.status != "DISCARDED")
    ):
        return 0, f"El cierre del {quarter}T {year} ya está preparado."
    prepare_advisor_email(database, year=year, quarter=quarter, created_by="agent")
    return 1, f"Cierre del {quarter}T {year} preparado para la gestoría en la bandeja de salida."


def job_digest(database: Session, now: datetime) -> tuple[int, str]:
    from app.models import OutboxMessage
    from app.outbox_service import create_message

    company = database.scalar(select(CompanyProfile).limit(1))
    today = now.date()
    if database.scalar(
        select(OutboxMessage.id).where(OutboxMessage.kind == "DIGEST", OutboxMessage.entity_type == "digest", OutboxMessage.entity_id == today.toordinal())
    ):
        return 0, "El resumen de hoy ya está preparado."

    digest = build_digest(database, today)
    create_message(
        database,
        kind="DIGEST",
        subject=f"Tu resumen de hoy · {digest['headline']}",
        body=digest["text"],
        to_email=company.email if company else None,
        to_name=company.name if company else None,
        entity_type="digest",
        entity_id=today.toordinal(),
    )
    return 1, f"Resumen preparado: {digest['headline']}."


def job_agents(database: Session, now: datetime) -> tuple[int, str]:
    from app.agents.orchestrator import process_pending

    cases = process_pending(database, trigger="schedule", today=now.date())
    if not cases:
        return 0, "Sin notificaciones nuevas que trabajar."
    return len(cases), f"{len(cases)} notificación(es) trabajadas de punta a punta: " + ", ".join(case.code for case in cases) + "."


def job_anomalies(database: Session, now: datetime) -> tuple[int, str]:
    from app.agents.detector import run_anomaly_scan
    from app.agents.pulse import escalate_analytic_findings

    # Lo grave sobre una factura lo investiga el orquestador entero; el resto, aviso.
    escalated = escalate_analytic_findings(database, now.date())
    result = run_anomaly_scan(database, trigger="schedule", today=now.date())
    investigated = f" {len(escalated)} investigada(s) a fondo por el orquestador." if escalated else ""
    if not result["created"] and not result["closed"] and not escalated:
        return 0, f"Todo cuadra ({result['found']} aviso(s) ya conocidos)." if result["found"] else "Todo cuadra: sin anomalías."
    return result["created"] + len(escalated), f"{result['created']} anomalía(s) nuevas y {result['closed']} cerrada(s) solas." + investigated


def job_deadlines(database: Session, now: datetime) -> tuple[int, str]:
    from app.agents.orchestrator import DEADLINE_LEAD_DAYS
    from app.agents.director import refresh_priorities
    from app.agents.orchestrator import watch_deadlines

    refresh_priorities(database, now.date())
    cases = watch_deadlines(database, trigger="schedule", today=now.date())
    if not cases:
        return 0, f"Ningún modelo vence en los próximos {DEADLINE_LEAD_DAYS} días sin expediente."
    return len(cases), f"{len(cases)} plazo(s) con expediente preparado: " + ", ".join(f"{case.code} ({case.title})" for case in cases) + "."


def job_email(database: Session, now: datetime) -> tuple[int, str]:
    from app.connectors.email.client import imap_configured
    from app.connectors.email.client import mailbox_folder
    from app.connectors.email.client import poll

    if not imap_configured() and not mailbox_folder().exists():
        return 0, "Sin buzón configurado (IMAP o carpeta data/buzon)."
    result = poll(database)
    if not result["messages"]:
        return 0, "Sin correos nuevos."
    return result["documents"], f"{result['messages']} correo(s), {result['documents']} documento(s)" + (f"; expedientes: {', '.join(result['cases'])}" if result["cases"] else "") + "."


def job_follow_up(database: Session, now: datetime) -> tuple[int, str]:
    from app.agents.perseguidor import follow_up

    result = follow_up(database, today=now.date())
    if not result["reminders"]:
        return 0, "Nada que recordar: no hay documentación pedida sin respuesta."
    return result["reminders"], f"{result['reminders']} recordatorio(s) preparados" + (f"; {result['escalated']} expediente(s) escalados" if result["escalated"] else "") + "."


def job_bank_sync(database: Session, now: datetime) -> tuple[int, str]:
    from app.bank_connect import provider
    from app.bank_sync import sync_all

    if provider() is None:
        return 0, "Sin banco conectado: se usa el extracto importado a mano."
    result = sync_all(database, today=now.date())
    if not result["connections"]:
        return 0, "Ningún banco conectado todavía."
    errors = f" Avisos: {'; '.join(result['errors'])}" if result["errors"] else ""
    return result["imported"], f"{result['imported']} movimiento(s) nuevos del banco, {result['auto_matched']} conciliado(s) solos.{errors}"


def job_intelligence(database: Session, now: datetime) -> tuple[int, str]:
    from app.intelligence.service import run

    result = run(database, today=now.date())
    fetched, matched = result["fetched"], result["matched"]
    if fetched["errors"]:
        return 0, f"BOE no disponible: {fetched['errors'][0]}. Se reintentará en la próxima ejecución."
    return matched["visible"], f"BOE: {fetched['new']} disposición(es) nuevas; {matched['visible']} relevante(s) para tu perfil ({matched['analysed']} resumida(s) con IA)."


AUTOMATIONS: tuple[Automation, ...] = (
    Automation("AGENT_PIPELINE", "Orquestador de expedientes", "Cada notificación nueva recorre los agentes de punta a punta: la detecta, la asigna, mira el impacto fiscal, busca antecedentes, reúne la documentación y prepara la respuesta.", "Cada 5 minutos", 0, 5, "interval", job_agents, 45, "Agente", "expedientes"),
    Automation("EMAIL_INBOX", "Buzón de correo", "Lee los correos nuevos (IMAP o la carpeta data/buzon), guarda sus facturas y notificaciones y las pasa al orquestador como cualquier otra entrada.", "Cada 5 minutos", 0, 5, "interval", job_email, 5, "Agente", "expedientes"),
    Automation("ANOMALY_SCAN", "Detector de anomalías", "Cruza facturas, banco e histórico y abre un expediente cuando algo no cuadra: importes atípicos, duplicados, IVA inusual, facturas que faltan o pagos sin factura.", "Cada día · 06:45", 6, 45, "daily", job_anomalies, 15, "Agente", "expedientes"),
    Automation("DEADLINE_WATCH", "Vigilante de plazos", "15 días antes de cada 303, 130, 111 o 115 abre su expediente: borrador del modelo, facturas sin revisar, anomalías del periodo y pasos hasta presentarlo.", "Cada día · 07:10", 7, 10, "daily", job_deadlines, 10, "Agente", "expedientes"),
    Automation("FOLLOW_UP", "Perseguidor de documentación", "Recuerda a quien debe aportar documentación, con cortesía creciente, hasta que la sube; después la verifica.", "Cada día · 09:30", 9, 30, "daily", job_follow_up, 10, "Agente", "expedientes"),
    Automation("BANK_SYNC", "Banco conectado", "Trae los movimientos nuevos de los bancos conectados (PSD2), sin duplicar los que ya entraron por extracto, y los concilia.", "Cada 6 horas", 0, 360, "interval", job_bank_sync, 2, "Finanzas", "negocio"),
    Automation("BANK_MATCH", "Conciliación bancaria", "Cruza los movimientos importados con facturas pendientes y propone el cobro o pago.", "Cada día · 06:30", 6, 30, "daily", job_bank, 3, "Finanzas", "negocio"),
    Automation("RECURRING_INVOICES", "Facturas recurrentes", "Genera (y emite si lo indicas) las cuotas, igualas y alquileres que se facturan cada periodo.", "Cada día · 07:00", 7, 0, "daily", job_recurring, 10, "Ventas", "ventas"),
    Automation("DAILY_DIGEST", "Resumen diario", "Prepara un correo con lo urgente del día: plazos, cobros, mensajes y fichajes.", "Cada día · 07:30", 7, 30, "daily", job_digest, 5, "Agente", "panel"),
    Automation("INTEL_BOE", "Radar jurídico (BOE)", "Descarga el sumario del BOE, filtra lo que toca tus áreas, lo explica con la fuente oficial y lo deja en Inteligencia.", "Cada día · 08:00", 8, 0, "daily", job_intelligence, 10, "Agente", "inteligencia"),
    Automation("DUNNING", "Reclamación de impagos", "Revisa las facturas vencidas y redacta el recordatorio, el segundo aviso o el requerimiento formal con intereses.", "Cada día · 08:00", 8, 0, "daily", job_dunning, 15, "Ventas", "ventas"),
    Automation("TIMESHEET_WATCH", "Vigilancia del registro de jornada", "Detecta fichajes olvidados, jornadas de más de 9 h, descansos cortos y días sin registro.", "Cada día · 10:00", 10, 0, "daily", job_timesheet, 4, "Personas", "jornada"),
    Automation("PAYROLL_DRAFT", "Borrador de nóminas", "El día 25 prepara la nómina del mes de toda la plantilla para que solo tengas que revisar y aprobar.", "Cada mes · día 25", 7, 0, "monthly", job_payroll, 20, "Personas", "nominas", day=25),
    Automation("ADVISOR_PACK", "Cierre para la gestoría", "El día 5 tras cada trimestre prepara el paquete (libros, modelos, facturas, nóminas, banco) para tu asesor.", "Trimestral · día 5", 9, 0, "quarterly", job_advisor, 120, "Finanzas", "informes", day=5),
)
BY_CODE = {item.code: item for item in AUTOMATIONS}


# ---------------------------------------------------------------------
# Resumen diario
# ---------------------------------------------------------------------


def build_digest(database: Session, today: date) -> dict[str, Any]:
    from app.agenda_service import build_agenda
    from app.dunning_service import collections_overview
    from app.outbox_service import outbox_counts
    from app.sales_service import format_eur
    from app.timesheet_service import timesheet_alerts

    agenda = build_agenda(database, horizon_days=7, today=today)
    urgent = [item for item in agenda["items"] if item["level"] in {"overdue", "critical", "high"}]
    collections = collections_overview(database, today)
    outbox = outbox_counts(database)
    alerts = timesheet_alerts(database)

    try:
        from app.bank_service import build_cashflow_forecast

        cash = build_cashflow_forecast(database, horizon_days=30, today=today)
    except Exception:  # la previsión es opcional en el resumen
        cash = None

    lines = [f"Buenos días. Esto es lo que tu administrativo digital ha revisado hoy ({today:%d/%m/%Y}).", ""]

    if urgent:
        lines.append("LO URGENTE")
        for item in urgent[:8]:
            lines.append(f"  · {item['title']} — {item['detail']}")
        lines.append("")

    summary = collections["summary"]
    if summary["count"]:
        lines.append("COBROS")
        lines.append(f"  · {summary['count']} factura(s) vencida(s) por {format_eur(summary['amount'])}.")
        if summary["pending_actions"]:
            lines.append(f"  · {summary['pending_actions']} reclamación(es) listas para enviar en la bandeja de salida.")
        lines.append("")

    if outbox["draft"]:
        lines.append(f"BANDEJA DE SALIDA: {outbox['draft']} mensaje(s) esperando tu visto bueno.")
        lines.append("")

    if alerts:
        lines.append("REGISTRO DE JORNADA")
        for alert in alerts[:5]:
            lines.append(f"  · {alert['employee_name']}: {alert['title'].lower()}")
        lines.append("")

    if cash and cash.get("lowest_point"):
        lowest = cash["lowest_point"]
        balance = lowest.get("balance") if isinstance(lowest, dict) else None
        if balance is not None:
            lines.append(f"TESORERÍA: el saldo previsto más bajo en 30 días es {format_eur(balance)} ({lowest.get('date', '')}).")
            lines.append("")

    if len(lines) <= 2:
        lines.append("Todo en orden: no hay nada que requiera tu atención hoy.")

    headline = (
        f"{len(urgent)} asunto(s) urgente(s)" if urgent
        else f"{summary['count']} cobro(s) vencido(s)" if summary["count"]
        else "todo en orden"
    )
    return {"headline": headline, "text": "\n".join(lines), "urgent": len(urgent)}


# ---------------------------------------------------------------------
# Ejecución
# ---------------------------------------------------------------------


def local_now() -> datetime:
    from app.sales_service import madrid_now

    return madrid_now()


def read_setting(database: Session, code: str) -> AutomationSetting:
    """Para mostrar o decidir: la guardada o la de por defecto, SIN guardarla (una lectura no escribe)."""
    from sqlalchemy import select

    return database.scalar(select(AutomationSetting).where(AutomationSetting.code == code)) or AutomationSetting(
        code=code, enabled=BY_CODE[code].default_enabled
    )


def get_setting(database: Session, code: str) -> AutomationSetting:
    """Para cambiarla o anotar una ejecución: la crea si aún no existe."""
    from sqlalchemy import select

    setting = database.scalar(select(AutomationSetting).where(AutomationSetting.code == code))
    if setting is None:
        setting = AutomationSetting(code=code, enabled=BY_CODE[code].default_enabled)
        database.add(setting)
        database.flush()
    return setting


def is_due(automation: Automation, setting: AutomationSetting, now: datetime) -> bool:
    if not setting.enabled:
        return False
    if automation.cadence == "interval":
        last = setting.last_run_at
        if last is None:
            return True
        last = last if last.tzinfo else last.replace(tzinfo=timezone.utc)
        return (now - last).total_seconds() >= automation.minute * 60
    if (now.hour, now.minute) < (automation.hour, automation.minute):
        return False

    last = setting.last_run_at
    last_local = last.astimezone(now.tzinfo).date() if last and last.tzinfo else (last.date() if last else None)
    today = now.date()

    if automation.cadence == "daily":
        return last_local != today
    if automation.cadence == "monthly":
        return today.day >= automation.day and (last_local is None or (last_local.year, last_local.month) != (today.year, today.month) or last_local.day < automation.day)
    if automation.cadence == "quarterly":
        return today.month in {1, 4, 7, 10} and today.day >= automation.day and (
            last_local is None or (last_local.year, last_local.month) != (today.year, today.month) or last_local.day < automation.day
        )
    return False


def run_automation(database: Session, code: str, *, trigger: str = "MANUAL", now: datetime | None = None, actor: str = "agent") -> AutomationRun:
    automation = BY_CODE.get(code)
    if automation is None:
        raise ValueError("Automatización desconocida.")

    now = now or local_now()
    setting = get_setting(database, code)
    run = AutomationRun(code=code, trigger=trigger, status="RUNNING", started_at=clock.now())
    database.add(run)
    database.flush()

    try:
        with database.begin_nested():
            items, summary = automation.handler(database, now)
        run.status = "OK" if items else "NOTHING"
        run.items = items
        run.summary = summary
    except Exception as error:  # una tarea que falla no tumba las demás
        logger.exception("Fallo en la automatización %s", code)
        run.status = "ERROR"
        run.items = 0
        run.summary = f"Error: {error}"

    run.finished_at = clock.now()
    setting.last_run_at = run.finished_at
    setting.last_status = run.status
    setting.last_summary = run.summary
    add_audit_event(
        database,
        action=f"automation.{code.lower()}",
        entity_type="automation",
        entity_id=code,
        actor=actor,
        event_data={"status": run.status, "items": run.items, "summary": run.summary, "trigger": trigger},
    )
    return run


def run_due(database: Session, now: datetime | None = None) -> list[AutomationRun]:
    now = now or local_now()
    runs = []
    for automation in AUTOMATIONS:
        setting = read_setting(database, automation.code)
        if is_due(automation, setting, now):
            runs.append(run_automation(database, automation.code, trigger="SCHEDULE", now=now))
    return runs


def run_due_all_clients(now: datetime | None = None) -> dict[int, int]:
    """Multiempresa: lo que toca se decide una vez y se ejecuta en la sesión aislada de cada cliente."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Client
    from app.tenancy import tenant_session

    now = now or local_now()
    with SessionLocal() as database:
        clients = list(database.scalars(select(Client.id).where(Client.active.is_(True))).all())
    done: dict[int, int] = {}
    for client_id in clients:
        with tenant_session(client_id) as database:
            try:
                # Cada cliente tiene sus automatizaciones activadas y su propio «última vez».
                due = [automation.code for automation in AUTOMATIONS if is_due(automation, read_setting(database, automation.code), now)]
                runs = [run_automation(database, code, trigger="SCHEDULE", now=now) for code in due]
                database.commit()
                done[client_id] = len(runs)
            except Exception:
                database.rollback()
                logger.exception("Error en las automatizaciones del cliente %s", client_id)
    return done


def serialize_run(run: AutomationRun) -> dict[str, Any]:
    automation = BY_CODE.get(run.code)
    return {
        "id": run.id,
        "code": run.code,
        "name": automation.name if automation else run.code,
        "trigger": run.trigger,
        "status": run.status,
        "items": run.items,
        "summary": run.summary,
        "started_at": run.started_at.isoformat() if run.started_at else None,
    }


def next_run_label(automation: Automation, setting: AutomationSetting, now: datetime) -> str:
    if not setting.enabled:
        return "Desactivada"
    if automation.cadence == "interval":
        return f"Cada {automation.minute // 60} horas" if automation.minute >= 120 and automation.minute % 60 == 0 else f"Cada {automation.minute} minutos"
    if is_due(automation, setting, now):
        return "En la próxima comprobación"
    at = f"{automation.hour:02d}:{automation.minute:02d}"
    if automation.cadence == "daily":
        return f"Mañana a las {at}" if (now.hour, now.minute) >= (automation.hour, automation.minute) else f"Hoy a las {at}"
    if automation.cadence == "monthly":
        target = date(now.year, now.month, automation.day)
        if now.date() >= target:
            target = (target.replace(day=1) + timedelta(days=32)).replace(day=automation.day)
        return f"{target:%d/%m} a las {at}"
    month = next(m for m in (1, 4, 7, 10, 13) if (m, automation.day) > (now.month, now.day) or m == 13)
    year = now.year + (1 if month == 13 else 0)
    return f"{automation.day:02d}/{(1 if month == 13 else month):02d}/{year} a las {at}"


def automations_overview(database: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or local_now()
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    stats = {
        code: (count, items)
        for code, count, items in database.execute(
            select(AutomationRun.code, func.count(), func.coalesce(func.sum(AutomationRun.items), 0))
            .where(AutomationRun.started_at >= month_start)
            .group_by(AutomationRun.code)
        ).all()
    }
    items = []
    minutes_saved = 0
    for automation in AUTOMATIONS:
        setting = read_setting(database, automation.code)
        runs, handled = stats.get(automation.code, (0, 0))
        minutes_saved += handled * automation.minutes_per_item
        items.append(
            {
                "code": automation.code,
                "name": automation.name,
                "description": automation.description,
                "schedule": automation.schedule,
                "area": automation.area,
                "tab": automation.tab,
                "enabled": setting.enabled,
                "last_run_at": setting.last_run_at.isoformat() if setting.last_run_at else None,
                "last_status": setting.last_status,
                "last_summary": setting.last_summary,
                "next_run": next_run_label(automation, setting, now),
                "month_runs": runs,
                "month_items": int(handled),
            }
        )

    recent = database.scalars(select(AutomationRun).order_by(AutomationRun.started_at.desc(), AutomationRun.id.desc()).limit(40)).all()
    company = database.scalar(select(CompanyProfile).limit(1))
    hourly = float(company.hourly_cost) if company and company.hourly_cost else 25.0
    return {
        "automations": items,
        "recent": [serialize_run(item) for item in recent],
        "month": {
            "items": sum(item["month_items"] for item in items),
            "runs": sum(item["month_runs"] for item in items),
            "hours_saved": round(minutes_saved / 60, 1),
            "cost_saved": round(minutes_saved / 60 * hourly, 2),
        },
        "scheduler": SCHEDULER.state(),
    }


def set_enabled(database: Session, code: str, enabled: bool) -> AutomationSetting:
    if code not in BY_CODE:
        raise ValueError("Automatización desconocida.")
    setting = get_setting(database, code)
    setting.enabled = enabled
    return setting


# ---------------------------------------------------------------------
# Planificador
# ---------------------------------------------------------------------


class Scheduler:
    def __init__(self, interval_seconds: int = 60) -> None:
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_tick: datetime | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="capafiscal-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def state(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
        }

    def tick(self) -> None:
        from app.config import settings
        from app.database import SessionLocal

        if settings.auth_required:
            run_due_all_clients()
            self.last_tick = clock.now()
            return
        database = SessionLocal()
        try:
            run_due(database)
            database.commit()
        except Exception:
            database.rollback()
            logger.exception("Error en el planificador de automatizaciones")
        finally:
            database.close()
        self.last_tick = clock.now()

    def _loop(self) -> None:
        # Pequeña espera para no competir con el arranque.
        if self._stop.wait(5):
            return
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)


SCHEDULER = Scheduler()
