"""
Agenda del agente: todo lo que vence pronto en un solo sitio (impuestos,
notificaciones, cobros, pagos y caducidades de cumplimiento).
"""
from __future__ import annotations

from app import clock
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from app.calendar_es import days_until


LEVEL_ORDER = {"overdue": 0, "critical": 1, "high": 2, "normal": 3}


def level_for(days_left: int) -> str:
    if days_left < 0:
        return "overdue"

    if days_left <= 3:
        return "critical"

    if days_left <= 7:
        return "high"

    return "normal"


def format_day(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def build_agenda(
    database: Session,
    *,
    horizon_days: int = 30,
    today: date | None = None,
) -> dict[str, Any]:
    from app.compliance_service import MANUAL_ITEMS
    from app.compliance_service import build_compliance_status
    from app.notification_service import list_notifications
    from app.reports_service import build_payments_overview
    from app.reports_service import format_eur
    from app.tax_service import build_tax_calendar

    current_day = today or clock.today()
    items: list[dict[str, Any]] = []

    # Impuestos
    for year in {current_day.year, current_day.year + 1}:
        for entry in build_tax_calendar(database, year=year, today=current_day)["entries"]:
            if entry["status"] in {"FILED", "NO_DATA"}:
                continue

            due = date.fromisoformat(entry["due_date"])
            remaining = days_until(due, current_day)

            if remaining > horizon_days:
                continue

            # Solo se arrastran vencidos recientes: lo antiguo es ruido.
            if remaining < -90:
                continue

            estimate = entry.get("estimate")
            amount_text = (
                f" Estimación: {format_eur(estimate)}."
                if estimate
                else ""
            )
            items.append(
                {
                    "code": "TAX_DEADLINE",
                    "kind": "tax",
                    "level": level_for(remaining),
                    "date": due.isoformat(),
                    "days_left": remaining,
                    "title": f"Modelo {entry['model']} · {entry['period_label']}",
                    "detail": f"{entry['name']}. Vence el {format_day(due)}.{amount_text}",
                    "action": (
                        "Revisa el borrador y márcalo como presentado."
                        if remaining >= 0
                        else "Plazo vencido: preséntalo cuanto antes para reducir recargos."
                    ),
                    "entity_type": "tax",
                    "entity_id": int(entry["model"]) * 100000 + entry["period_year"] * 10 + entry["period"],
                    "amount": estimate,
                    "tab": "impuestos",
                }
            )

    # Notificaciones
    for notification in list_notifications(database, only_open=True):
        if notification["deadline"] is None:
            continue

        due = date.fromisoformat(notification["deadline"])
        remaining = days_until(due, current_day)

        if remaining > horizon_days:
            continue

        level = level_for(remaining)

        if notification["notification_type"] in {"EMBARGO", "APREMIO"} and level == "normal":
            level = "high"

        items.append(
            {
                "code": "NOTIFICATION_DEADLINE",
                "kind": "notification",
                "level": level,
                "date": due.isoformat(),
                "days_left": remaining,
                "title": notification["title"],
                "detail": (
                    f"{notification['issuer_label']}"
                    + (f" · ref. {notification['reference']}" if notification["reference"] else "")
                    + f". Plazo orientativo: {format_day(due)}."
                ),
                "action": "Abre la notificación, prepara la respuesta o el pago y márcala como atendida.",
                "entity_type": "notification",
                "entity_id": notification["id"],
                "document_id": notification["document_id"],
                "amount": notification["amount"],
                "tab": "notificaciones",
            }
        )

    # Cobros y pagos
    for direction, label, action in (
        ("ISSUED", "Cobro", "Reclama el cobro al cliente."),
        ("RECEIVED", "Pago", "Programa el pago para evitar recargos o cortes de servicio."),
    ):
        overview = build_payments_overview(database, direction=direction, today=current_day)

        for payment in overview["items"]:
            if not payment["due_date"]:
                continue

            due = date.fromisoformat(payment["due_date"])
            remaining = days_until(due, current_day)

            if remaining > min(horizon_days, 14):
                continue

            items.append(
                {
                    "code": f"{direction}_PAYMENT_DUE",
                    "kind": "collection" if direction == "ISSUED" else "payment",
                    # Un cobro que aún no vence no es urgente; un pago sí puede serlo.
                    "level": (
                        level_for(remaining)
                        if remaining < 0 or direction == "RECEIVED"
                        else "normal"
                    ),
                    "date": due.isoformat(),
                    "days_left": remaining,
                    "title": (
                        f"{label} {'vencido' if remaining < 0 else 'próximo'} · "
                        f"{payment['supplier_name'] or 'Sin nombre'}"
                    ),
                    "detail": (
                        f"Factura {payment['invoice_number'] or 's/n'} por "
                        f"{format_eur(payment['total'])}, vence el {format_day(due)}."
                    ),
                    "action": action,
                    "entity_type": "invoice",
                    "entity_id": payment["invoice_id"],
                    "document_id": payment["document_id"],
                    "amount": payment["total"],
                    "tab": "negocio",
                }
            )

    # Cumplimiento
    for item in build_compliance_status(database, today=current_day)["items"]:
        if item["automatic"] or item["status"] not in {"WARNING", "EXPIRED"}:
            continue

        reference_date = item.get("expires_at") or item.get("obligation_date")
        due = date.fromisoformat(reference_date) if reference_date else current_day
        remaining = days_until(due, current_day)

        items.append(
            {
                "code": f"COMPLIANCE_{item['code']}",
                "kind": "compliance",
                "level": "overdue" if item["status"] == "EXPIRED" else ("critical" if remaining <= 7 else "high"),
                "date": due.isoformat(),
                "days_left": remaining,
                "title": item["title"],
                "detail": item["message"] or item["description"],
                "action": "Revisa la sección de cumplimiento.",
                "entity_type": "compliance",
                "entity_id": list(MANUAL_ITEMS).index(item["code"]) + 1,
                "amount": None,
                "tab": "cumplimiento",
            }
        )

    # Equipo: altas, bajas, fines de contrato.
    from app.team_service import build_team_overview

    # Se agrupan por persona para no inundar la agenda: una línea por
    # empleado con la fecha más urgente y el resumen de lo pendiente.
    grouped: dict[int, list[dict]] = {}
    for alert in build_team_overview(database, today=current_day)["alerts"]:
        grouped.setdefault(alert["employee_id"], []).append(alert)

    for employee_id, alerts in grouped.items():
        alerts.sort(key=lambda alert: alert["due_date"] or current_day.isoformat())
        first = alerts[0]
        due = date.fromisoformat(first["due_date"]) if first["due_date"] else current_day
        remaining = days_until(due, current_day)

        if remaining > horizon_days:
            continue

        overdue = any(alert["status"] == "overdue" for alert in alerts)

        if len(alerts) == 1:
            title = f"{first['title']} · {first['employee_name']}"
            detail = f"Fecha límite: {format_day(due)}."
        else:
            title = f"{len(alerts)} trámites de personal pendientes · {first['employee_name']}"
            detail = f"El más urgente: {first['title'][:1].lower() + first['title'][1:]} ({format_day(due)})."

        items.append(
            {
                "code": "TEAM_CHECK",
                "kind": "team",
                "level": "overdue" if overdue else level_for(remaining),
                "date": due.isoformat(),
                "days_left": remaining,
                "title": title,
                "detail": detail,
                "action": "Abre la ficha en Equipo y complétalo.",
                "entity_type": "employee",
                "entity_id": employee_id,
                "amount": None,
                "tab": "equipo",
            }
        )

    # Mensajes que el agente ha preparado y esperan el visto bueno.
    from app.outbox_service import outbox_counts

    pending_messages = outbox_counts(database)["draft"]
    if pending_messages:
        items.append(
            {
                "code": "OUTBOX",
                "kind": "outbox",
                "level": "high",
                "date": current_day.isoformat(),
                "days_left": 0,
                "title": "1 mensaje listo para enviar" if pending_messages == 1 else f"{pending_messages} mensajes listos para enviar",
                "detail": "Facturas, reclamaciones de cobro o recibos que el agente ha redactado.",
                "action": "Revísalos en la bandeja de salida y envíalos.",
                "entity_type": "outbox",
                "entity_id": 0,
                "amount": None,
                "tab": "salida",
            }
        )

    # Registro de jornada: olvidos e incidencias.
    from app.timesheet_service import timesheet_alerts

    alerts = timesheet_alerts(database)
    if alerts:
        people = sorted({alert["employee_name"] for alert in alerts})
        forgotten = sum(1 for alert in alerts if alert["kind"] == "forgotten")
        items.append(
            {
                "code": "TIMESHEET",
                "kind": "timesheet",
                "level": "high" if forgotten else "normal",
                "date": current_day.isoformat(),
                "days_left": 0,
                "title": f"{len(alerts)} incidencia(s) en el registro de jornada",
                "detail": ("Fichajes sin salida, jornadas largas o días sin registro · " + ", ".join(people[:3]) + ("…" if len(people) > 3 else "")),
                "action": "Corrígelas en Jornada (queda constancia del motivo).",
                "entity_type": "timesheet",
                "entity_id": 0,
                "amount": None,
                "tab": "jornada",
            }
        )

    # Nómina del mes sin preparar a partir del día 20.
    from sqlalchemy import select

    from app.models import Employee
    from app.models import PayrollRun

    has_staff = database.scalar(select(Employee.id).where(Employee.annual_salary.is_not(None)).limit(1))
    run_exists = database.scalar(
        select(PayrollRun.id).where(
            PayrollRun.year == current_day.year,
            PayrollRun.month == current_day.month,
        )
    )

    if has_staff and not run_exists and current_day.day >= 20:
        from app.calendar_es import last_day_of_month

        due = last_day_of_month(current_day.year, current_day.month)
        remaining = days_until(due, current_day)
        items.append(
            {
                "code": "PAYROLL_PENDING",
                "kind": "payroll",
                "level": level_for(remaining),
                "date": due.isoformat(),
                "days_left": remaining,
                "title": "Preparar las nóminas del mes",
                "detail": f"Las nóminas se pagan antes del {format_day(due)}.",
                "action": "Genera el borrador en Nóminas, revisa variables y apruébalo.",
                "entity_type": "payroll",
                "entity_id": current_day.year * 100 + current_day.month,
                "amount": None,
                "tab": "nominas",
            }
        )

    items.sort(key=lambda item: (LEVEL_ORDER.get(item["level"], 9), item["date"]))

    return {
        "today": current_day.isoformat(),
        "horizon_days": horizon_days,
        "counts": {
            level: sum(1 for item in items if item["level"] == level)
            for level in LEVEL_ORDER
        },
        "items": items,
    }
