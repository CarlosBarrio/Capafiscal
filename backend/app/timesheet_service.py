"""
Registro de jornada (art. 34.9 del Estatuto de los Trabajadores).

Toda empresa con personal debe registrar el inicio y el final de la jornada
de cada persona y conservarlo cuatro años a disposición de la plantilla, sus
representantes y la Inspección de Trabajo. Aquí:

- cada persona ficha entrada y salida (o se registra a mano con motivo);
- cualquier corrección queda trazada (quién, cuándo y por qué);
- el agente vigila olvidos, jornadas de más de 9 horas, descanso entre
  jornadas inferior a 12 horas y días laborables sin registro;
- el informe mensual sale en PDF para firmar y en Excel.
"""
from __future__ import annotations

import io
from calendar import monthrange
from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calendar_es import is_business_day
from app.invoice_service import add_audit_event
from app.models import Absence
from app.models import CompanyProfile
from app.models import Employee
from app.models import TimeEntry
from app.team_service import display_name
from app.team_service import employment_status
from app.team_service import initials
from app.team_service import avatar_color

MAX_ORDINARY_MINUTES = 9 * 60      # art. 34.3 ET (salvo convenio)
MIN_REST_MINUTES = 12 * 60         # descanso mínimo entre jornadas
FORGOTTEN_HOURS = 14               # fichaje abierto más tiempo = olvido
WEEK_HOURS = 40
WEEKDAYS = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
MONTHS_ES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


class TimesheetError(ValueError):
    pass


def local_now() -> datetime:
    from app.sales_service import madrid_now

    return madrid_now().replace(tzinfo=None)


def minutes_of(entry: TimeEntry, now: datetime | None = None) -> int:
    end = entry.clock_out or now
    if end is None:
        return 0
    return max(0, int((end - entry.clock_in).total_seconds() // 60))


def format_minutes(minutes: int) -> str:
    sign = "-" if minutes < 0 else ""
    minutes = abs(minutes)
    return f"{sign}{minutes // 60} h {minutes % 60:02d} min"


def active_employees(database: Session, day: date) -> list[Employee]:
    employees = database.scalars(select(Employee).order_by(Employee.first_name)).all()
    return [
        item for item in employees
        if employment_status(item, day) == "ACTIVE"
        and (not item.hire_date or item.hire_date <= day)
    ]


def open_entry(database: Session, employee_id: int) -> TimeEntry | None:
    return database.scalar(
        select(TimeEntry)
        .where(TimeEntry.employee_id == employee_id, TimeEntry.clock_out.is_(None))
        .order_by(TimeEntry.clock_in.desc())
        .limit(1)
    )


def serialize_entry(entry: TimeEntry, now: datetime | None = None) -> dict[str, Any]:
    minutes = minutes_of(entry, now if entry.clock_out is None else None)
    return {
        "id": entry.id,
        "employee_id": entry.employee_id,
        "employee_name": display_name(entry.employee) if entry.employee else None,
        "work_date": entry.work_date.isoformat(),
        "clock_in": entry.clock_in.isoformat(timespec="minutes"),
        "clock_out": entry.clock_out.isoformat(timespec="minutes") if entry.clock_out else None,
        "minutes": minutes,
        "duration": format_minutes(minutes),
        "open": entry.clock_out is None,
        "source": entry.source,
        "note": entry.note,
        "edit_reason": entry.edit_reason,
        "edited_by": entry.edited_by,
    }


# ---------------------------------------------------------------------
# Fichar y corregir
# ---------------------------------------------------------------------


def clock(database: Session, employee_id: int, *, actor: str = "user", now: datetime | None = None, note: str | None = None) -> dict[str, Any]:
    employee = database.get(Employee, employee_id)
    if employee is None:
        raise TimesheetError("Persona no encontrada.")

    now = (now or local_now()).replace(second=0, microsecond=0)
    current = open_entry(database, employee_id)
    if current is None:
        check_employed(employee, now.date())

    if current:
        if now <= current.clock_in:
            raise TimesheetError("La salida no puede ser anterior a la entrada.")
        current.clock_out = now
        action = "out"
        entry = current
    else:
        entry = TimeEntry(employee_id=employee_id, work_date=now.date(), clock_in=now, source="APP", note=note)
        database.add(entry)
        action = "in"

    database.flush()
    add_audit_event(
        database,
        action=f"timesheet.clock_{action}",
        entity_type="employee",
        entity_id=employee_id,
        actor=actor,
        event_data={"entry_id": entry.id, "time": now.isoformat(timespec="minutes")},
    )
    return {"action": action, "entry": serialize_entry(entry, now)}


def check_employed(employee: Employee, day: date) -> None:
    if employee.hire_date and day < employee.hire_date:
        raise TimesheetError(f"{display_name(employee)} no está de alta hasta el {employee.hire_date:%d/%m/%Y}.")
    if employee.termination_date and day > employee.termination_date:
        raise TimesheetError(f"{display_name(employee)} causó baja el {employee.termination_date:%d/%m/%Y}.")


def parse_time(day: date, value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        hour, minute = (int(part) for part in value.split(":")[:2])
        return datetime.combine(day, time(hour, minute))
    except (ValueError, TypeError) as error:
        raise TimesheetError("Hora no válida (usa HH:MM).") from error


def check_overlap(database: Session, employee_id: int, start: datetime, end: datetime | None, exclude_id: int | None = None) -> None:
    entries = database.scalars(
        select(TimeEntry).where(
            TimeEntry.employee_id == employee_id,
            TimeEntry.work_date.between(start.date() - timedelta(days=1), start.date() + timedelta(days=1)),
        )
    ).all()
    for item in entries:
        if item.id == exclude_id:
            continue
        other_end = item.clock_out or datetime.max
        mine_end = end or datetime.max
        if start < other_end and item.clock_in < mine_end:
            raise TimesheetError(
                f"Se solapa con otro registro ({item.clock_in:%H:%M}–{item.clock_out:%H:%M} del {item.work_date:%d/%m})."
                if item.clock_out
                else "Hay un fichaje abierto que se solapa. Ciérralo antes."
            )


def save_manual(
    database: Session,
    *,
    employee_id: int,
    work_date: date,
    start: str,
    end: str | None,
    reason: str | None,
    note: str | None = None,
    entry: TimeEntry | None = None,
    actor: str = "user",
) -> TimeEntry:
    if not reason or len(reason.strip()) < 3:
        raise TimesheetError("Indica el motivo del registro manual o de la corrección (queda en el historial).")
    employee = database.get(Employee, employee_id)
    if employee is None:
        raise TimesheetError("Persona no encontrada.")
    check_employed(employee, work_date)

    clock_in = parse_time(work_date, start)
    clock_out = parse_time(work_date, end)
    if clock_in is None:
        raise TimesheetError("Indica la hora de entrada.")
    if clock_out is not None and clock_out <= clock_in:
        clock_out += timedelta(days=1)  # turno que cruza la medianoche
    if clock_out and clock_out - clock_in > timedelta(hours=16):
        raise TimesheetError("Un tramo no puede superar 16 horas.")
    if work_date > local_now().date():
        raise TimesheetError("No se registran jornadas futuras.")

    check_overlap(database, employee_id, clock_in, clock_out, entry.id if entry else None)

    previous = serialize_entry(entry) if entry else None
    if entry is None:
        entry = TimeEntry(employee_id=employee_id, source="MANUAL")
        database.add(entry)

    entry.work_date = work_date
    entry.clock_in = clock_in
    entry.clock_out = clock_out
    entry.note = note or entry.note
    entry.edit_reason = reason.strip()[:255]
    entry.edited_by = actor
    database.flush()

    add_audit_event(
        database,
        action="timesheet.corrected" if previous else "timesheet.manual",
        entity_type="employee",
        entity_id=employee_id,
        actor=actor,
        event_data={"entry_id": entry.id, "before": previous, "after": serialize_entry(entry), "reason": entry.edit_reason},
    )
    return entry


def delete_entry(database: Session, entry: TimeEntry, reason: str | None, actor: str = "user") -> None:
    if not reason or len(reason.strip()) < 3:
        raise TimesheetError("Indica el motivo para borrar el registro.")
    add_audit_event(
        database,
        action="timesheet.deleted",
        entity_type="employee",
        entity_id=entry.employee_id,
        actor=actor,
        event_data={"entry": serialize_entry(entry), "reason": reason.strip()},
    )
    database.delete(entry)


# ---------------------------------------------------------------------
# Resúmenes
# ---------------------------------------------------------------------


def absences_between(database: Session, start: date, end: date) -> dict[int, set[date]]:
    result: dict[int, set[date]] = {}
    for absence in database.scalars(
        select(Absence).where(Absence.start_date <= end, Absence.end_date >= start)
    ).all():
        day = max(absence.start_date, start)
        while day <= min(absence.end_date, end):
            result.setdefault(absence.employee_id, set()).add(day)
            day += timedelta(days=1)
    return result


def expected_minutes_per_day(employee: Employee) -> int:
    return int(WEEK_HOURS * 60 / 5 * (employee.workday_percent or 100) / 100)


def works_on(employee: Employee, day: date, absent: set[date]) -> bool:
    if not is_business_day(day) or day in absent:
        return False
    if employee.hire_date and day < employee.hire_date:
        return False
    if employee.termination_date and day > employee.termination_date:
        return False
    return True


def entries_between(database: Session, start: date, end: date, employee_id: int | None = None) -> list[TimeEntry]:
    statement = (
        select(TimeEntry)
        .where(TimeEntry.work_date >= start, TimeEntry.work_date <= end)
        .order_by(TimeEntry.clock_in.asc())
    )
    if employee_id:
        statement = statement.where(TimeEntry.employee_id == employee_id)
    return list(database.scalars(statement).all())


def day_board(database: Session, now: datetime | None = None) -> dict[str, Any]:
    now = now or local_now()
    today = now.date()
    absent = absences_between(database, today, today)
    today_entries = entries_between(database, today, today)
    people = []

    for employee in active_employees(database, today):
        mine = [item for item in today_entries if item.employee_id == employee.id]
        current = open_entry(database, employee.id)
        worked = sum(minutes_of(item, now) for item in mine)
        if current and current.work_date != today:
            worked += minutes_of(current, now)
        state = (
            "working" if current
            else "absent" if today in absent.get(employee.id, set())
            else "done" if mine
            else "pending"
        )
        people.append(
            {
                "employee_id": employee.id,
                "name": display_name(employee),
                "initials": initials(employee),
                "color": avatar_color(employee),
                "job_title": employee.job_title,
                "state": state,
                "since": current.clock_in.isoformat(timespec="minutes") if current else None,
                "open_entry_id": current.id if current else None,
                "worked_minutes": worked,
                "worked": format_minutes(worked),
                "expected_minutes": expected_minutes_per_day(employee) if is_business_day(today) else 0,
                "segments": [serialize_entry(item, now) for item in mine],
            }
        )

    return {
        "now": now.isoformat(timespec="minutes"),
        "business_day": is_business_day(today),
        "people": people,
        "working": sum(1 for item in people if item["state"] == "working"),
    }


def month_summary(database: Session, year: int, month: int, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or local_now()
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    last_counted = min(end, now.date())
    absences = absences_between(database, start, end)
    entries = entries_between(database, start, end)
    rows = []

    employees = [
        item for item in database.scalars(select(Employee).order_by(Employee.first_name)).all()
        if (not item.hire_date or item.hire_date <= end) and (not item.termination_date or item.termination_date >= start)
    ]

    for employee in employees:
        mine = [item for item in entries if item.employee_id == employee.id]
        absent = absences.get(employee.id, set())
        worked = sum(minutes_of(item, now if item.clock_out is None else None) for item in mine)
        per_day: dict[date, int] = {}
        for item in mine:
            per_day[item.work_date] = per_day.get(item.work_date, 0) + minutes_of(item, now if item.clock_out is None else None)

        working_days = [
            start + timedelta(days=offset)
            for offset in range((end - start).days + 1)
            if works_on(employee, start + timedelta(days=offset), absent)
        ]
        expected = len([day for day in working_days if day <= last_counted]) * expected_minutes_per_day(employee)
        missing = [day for day in working_days if day < now.date() and day not in per_day]
        long_days = [day for day, minutes in per_day.items() if minutes > MAX_ORDINARY_MINUTES]

        rows.append(
            {
                "employee_id": employee.id,
                "name": display_name(employee),
                "initials": initials(employee),
                "color": avatar_color(employee),
                "workday_percent": employee.workday_percent or 100,
                "days_worked": len(per_day),
                "working_days": len(working_days),
                "worked_minutes": worked,
                "expected_minutes": expected,
                "balance_minutes": worked - expected,
                "worked": format_minutes(worked),
                "expected": format_minutes(expected),
                "balance": format_minutes(worked - expected),
                "missing_days": [day.isoformat() for day in missing],
                "long_days": sorted(day.isoformat() for day in long_days),
                "manual_entries": sum(1 for item in mine if item.source == "MANUAL" or item.edit_reason),
                "daily": {day.isoformat(): minutes for day, minutes in per_day.items()},
            }
        )

    return {
        "year": year,
        "month": month,
        "label": f"{MONTHS_ES[month - 1]} {year}",
        "days": (end - start).days + 1,
        "rows": rows,
        "totals": {
            "worked_minutes": sum(row["worked_minutes"] for row in rows),
            "balance_minutes": sum(row["balance_minutes"] for row in rows),
            "missing_days": sum(len(row["missing_days"]) for row in rows),
        },
    }


def timesheet_alerts(database: Session, *, now: datetime | None = None, lookback_days: int = 14) -> list[dict[str, Any]]:
    """Incidencias que el agente señala (y que llegan a la agenda de «Hoy»)."""
    now = now or local_now()
    today = now.date()
    start = today - timedelta(days=lookback_days)
    alerts: list[dict[str, Any]] = []

    # Hasta que la empresa empieza a fichar no hay nada que vigilar.
    if database.scalar(select(TimeEntry.id).limit(1)) is None:
        return alerts
    employees = {item.id: item for item in database.scalars(select(Employee)).all()}

    for entry in database.scalars(select(TimeEntry).where(TimeEntry.clock_out.is_(None))).all():
        if now - entry.clock_in > timedelta(hours=FORGOTTEN_HOURS):
            alerts.append(
                {
                    "kind": "forgotten",
                    "employee_id": entry.employee_id,
                    "employee_name": display_name(employees[entry.employee_id]),
                    "date": entry.work_date.isoformat(),
                    "title": "Fichaje sin salida",
                    "detail": f"Entrada el {entry.clock_in:%d/%m a las %H:%M} sin salida registrada.",
                    "entry_id": entry.id,
                }
            )

    entries = entries_between(database, start - timedelta(days=1), today)
    by_employee: dict[int, list[TimeEntry]] = {}
    for entry in entries:
        by_employee.setdefault(entry.employee_id, []).append(entry)

    absences = absences_between(database, start, today)

    for employee in active_employees(database, today):
        mine = by_employee.get(employee.id, [])
        per_day: dict[date, int] = {}
        for entry in mine:
            if entry.clock_out:
                per_day[entry.work_date] = per_day.get(entry.work_date, 0) + minutes_of(entry)

        for day, minutes in sorted(per_day.items()):
            if day >= start and minutes > MAX_ORDINARY_MINUTES:
                alerts.append(
                    {
                        "kind": "long_day",
                        "employee_id": employee.id,
                        "employee_name": display_name(employee),
                        "date": day.isoformat(),
                        "title": "Jornada de más de 9 horas",
                        "detail": f"{format_minutes(minutes)} el {day:%d/%m}. Revisa si son horas extra (máx. 80 al año) o compénsalas.",
                    }
                )

        closed = sorted((item for item in mine if item.clock_out), key=lambda item: item.clock_in)
        for previous, current in zip(closed, closed[1:]):
            if current.work_date == previous.work_date or current.work_date < start:
                continue
            rest = int((current.clock_in - previous.clock_out).total_seconds() // 60)
            if 0 <= rest < MIN_REST_MINUTES:
                alerts.append(
                    {
                        "kind": "short_rest",
                        "employee_id": employee.id,
                        "employee_name": display_name(employee),
                        "date": current.work_date.isoformat(),
                        "title": "Descanso entre jornadas inferior a 12 h",
                        "detail": f"Salió el {previous.clock_out:%d/%m a las %H:%M} y entró el {current.clock_in:%d/%m a las %H:%M}.",
                    }
                )

        registered = {item.work_date for item in mine}
        missing = [
            start + timedelta(days=offset)
            for offset in range((today - start).days)
            if works_on(employee, start + timedelta(days=offset), absences.get(employee.id, set()))
            and start + timedelta(days=offset) not in registered
        ]
        if missing:
            alerts.append(
                {
                    "kind": "missing",
                    "employee_id": employee.id,
                    "employee_name": display_name(employee),
                    "date": missing[0].isoformat(),
                    "title": f"{len(missing)} día(s) laborable(s) sin registro",
                    "detail": "Días: " + ", ".join(day.strftime("%d/%m") for day in missing[:8]) + ("…" if len(missing) > 8 else ""),
                    "days": [day.isoformat() for day in missing],
                }
            )

    order = {"forgotten": 0, "short_rest": 1, "long_day": 2, "missing": 3}
    alerts.sort(key=lambda item: (order[item["kind"]], item["date"]))
    return alerts


# ---------------------------------------------------------------------
# Exportaciones
# ---------------------------------------------------------------------


def build_month_pdf(database: Session, year: int, month: int, employee_id: int | None = None) -> bytes:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    ink, muted, line_color, accent = HexColor("#1c1a17"), HexColor("#6b675f"), HexColor("#e5e1da"), HexColor("#8c1d33")
    company = database.scalar(select(CompanyProfile).limit(1))
    summary = month_summary(database, year, month)
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    entries = entries_between(database, start, end, employee_id)
    rows = [row for row in summary["rows"] if not employee_id or row["employee_id"] == employee_id]

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left, right = 16 * mm, width - 16 * mm
    pdf.setTitle(f"Registro de jornada {summary['label']}")

    for row in rows:
        employee = database.get(Employee, row["employee_id"])
        y = height - 20 * mm
        pdf.setFillColor(accent)
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(left, y, "REGISTRO DIARIO DE JORNADA")
        pdf.setFont("Helvetica", 9)
        pdf.setFillColor(muted)
        pdf.drawRightString(right, y, summary["label"].capitalize())
        y -= 7 * mm
        pdf.setFillColor(ink)
        pdf.setFont("Helvetica", 9)
        pdf.drawString(left, y, f"Empresa: {(company.name if company else '') or '—'}   NIF: {(company.tax_id if company else '') or '—'}")
        y -= 5 * mm
        pdf.drawString(left, y, f"Persona: {row['name']}   NIF: {employee.tax_id or '—'}   Jornada: {row['workday_percent']} %")
        y -= 8 * mm

        headers = [("Día", left), ("Entrada", left + 30 * mm), ("Salida", left + 55 * mm), ("Horas", left + 80 * mm), ("Observaciones", left + 105 * mm)]
        pdf.setFont("Helvetica-Bold", 7.5)
        pdf.setFillColor(muted)
        for label, x in headers:
            pdf.drawString(x, y, label.upper())
        y -= 2.5 * mm
        pdf.setStrokeColor(line_color)
        pdf.line(left, y, right, y)

        mine = [item for item in entries if item.employee_id == row["employee_id"]]
        pdf.setFont("Helvetica", 8.5)
        pdf.setFillColor(ink)
        for item in mine:
            y -= 5 * mm
            if y < 45 * mm:
                pdf.showPage()
                y = height - 20 * mm
                pdf.setFont("Helvetica", 8.5)
            pdf.drawString(left, y, f"{WEEKDAYS[item.work_date.weekday()]} {item.work_date:%d/%m}")
            pdf.drawString(left + 30 * mm, y, item.clock_in.strftime("%H:%M"))
            pdf.drawString(left + 55 * mm, y, item.clock_out.strftime("%H:%M") if item.clock_out else "—")
            pdf.drawString(left + 80 * mm, y, format_minutes(minutes_of(item)) if item.clock_out else "abierto")
            remark = " · ".join(filter(None, [item.note, f"Corregido: {item.edit_reason}" if item.edit_reason else None]))
            pdf.drawString(left + 105 * mm, y, remark[:55])

        if not mine:
            y -= 6 * mm
            pdf.setFillColor(muted)
            pdf.drawString(left, y, "Sin registros en el periodo.")
            pdf.setFillColor(ink)

        y -= 10 * mm
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(left, y, f"Total trabajado: {row['worked']}    Jornada prevista: {row['expected']}    Diferencia: {row['balance']}")
        y -= 22 * mm
        pdf.setFont("Helvetica", 8)
        pdf.setStrokeColor(ink)
        pdf.line(left, y, left + 60 * mm, y)
        pdf.line(right - 60 * mm, y, right, y)
        pdf.drawString(left, y - 4 * mm, "Firma de la empresa")
        pdf.drawString(right - 60 * mm, y - 4 * mm, "Firma de la persona trabajadora")
        pdf.setFillColor(muted)
        pdf.setFont("Helvetica", 7)
        pdf.drawString(left, 12 * mm, "Registro conforme al art. 34.9 del Estatuto de los Trabajadores. Se conserva durante cuatro años.")
        pdf.setFillColor(ink)
        pdf.showPage()

    if not rows:
        pdf.drawString(left, height - 30 * mm, "No hay personas en el periodo.")
        pdf.showPage()

    pdf.save()
    return buffer.getvalue()


def build_month_xlsx(database: Session, year: int, month: int) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    summary = month_summary(database, year, month)
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Resumen"
    sheet.append(["Persona", "Días trabajados", "Días laborables", "Horas trabajadas", "Horas previstas", "Diferencia (h)", "Días sin registro", "Jornadas > 9 h"])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in summary["rows"]:
        sheet.append([
            row["name"], row["days_worked"], row["working_days"],
            round(row["worked_minutes"] / 60, 2), round(row["expected_minutes"] / 60, 2),
            round(row["balance_minutes"] / 60, 2), len(row["missing_days"]), len(row["long_days"]),
        ])

    detail = workbook.create_sheet("Registros")
    detail.append(["Persona", "Fecha", "Entrada", "Salida", "Horas", "Origen", "Motivo de corrección", "Observaciones"])
    for cell in detail[1]:
        cell.font = Font(bold=True)
    for item in entries_between(database, start, end):
        detail.append([
            display_name(item.employee), item.work_date, item.clock_in.strftime("%H:%M"),
            item.clock_out.strftime("%H:%M") if item.clock_out else "", round(minutes_of(item) / 60, 2),
            item.source, item.edit_reason or "", item.note or "",
        ])

    for ws in (sheet, detail):
        for column in ws.columns:
            ws.column_dimensions[column[0].column_letter].width = max(12, min(40, max(len(str(cell.value or "")) for cell in column) + 2))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
