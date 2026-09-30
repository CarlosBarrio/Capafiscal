"""
Equipo: fichas de personas, documentos, organigrama, proyectos,
ausencias y checklist de incorporación y baja.
"""
from __future__ import annotations

import re
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.calendar_es import add_business_days
from app.calendar_es import days_until
from app.calendar_es import is_business_day
from app.extractor import is_valid_spanish_tax_id
from app.extractor import normalize_search_text
from app.models import Absence
from app.models import Employee
from app.models import EmployeeDocument
from app.models import Project
from app.models import ProjectAssignment


CONTRACT_TYPES = {
    "INDEFINIDO": "Indefinido",
    "TEMPORAL": "Temporal",
    "PRACTICAS": "Prácticas",
    "FORMACION": "Formación en alternancia",
    "FIJO_DISCONTINUO": "Fijo discontinuo",
}

SS_STATUSES = {
    "PENDIENTE_ALTA": "Pendiente de alta",
    "ALTA": "De alta",
    "BAJA": "De baja",
}

DOCUMENT_KINDS = {
    "CV": "Currículum",
    "CONTRATO": "Contrato de trabajo",
    "ALTA_SS": "Resguardo de alta en la Seguridad Social",
    "BAJA_SS": "Resguardo de baja en la Seguridad Social",
    "DNI": "DNI / NIE",
    "MODELO_145": "Modelo 145 (datos para el IRPF)",
    "NOMINA": "Nómina",
    "CERTIFICADO": "Certificado o título",
    "OTRO": "Otro documento",
}

ABSENCE_KINDS = {
    "VACACIONES": "Vacaciones",
    "BAJA_IT": "Baja médica (IT)",
    "PERMISO": "Permiso retribuido",
    "ASUNTOS_PROPIOS": "Asuntos propios",
    "OTRO": "Otra ausencia",
}

PROJECT_STATUSES = {"ACTIVE": "Activo", "PAUSED": "En pausa", "DONE": "Terminado"}

SKILL_KEYWORDS = (
    "excel", "sql", "python", "java", "javascript", "react", "angular",
    "contabilidad", "nominas", "fiscal", "sap", "a3", "holded", "power bi",
    "marketing", "ventas", "atencion al cliente", "logistica", "ingles",
    "frances", "aleman", "carnet de conducir", "carretilla", "prl",
    "soldadura", "electricidad", "fontaneria", "cocina", "hosteleria",
    "photoshop", "autocad", "gestion de proyectos", "scrum", "rrhh",
)

SKILL_LABELS = {
    "sql": "SQL", "sap": "SAP", "a3": "A3", "power bi": "Power BI", "prl": "PRL",
    "rrhh": "RR. HH.", "javascript": "JavaScript", "nominas": "Nóminas",
    "atencion al cliente": "Atención al cliente", "logistica": "Logística",
    "ingles": "Inglés", "frances": "Francés", "aleman": "Alemán",
    "fontaneria": "Fontanería", "hosteleria": "Hostelería",
    "gestion de proyectos": "Gestión de proyectos", "autocad": "AutoCAD",
}

AVATAR_COLORS = (
    "#8c1d33", "#2563a8", "#1f7a4d", "#9a6700", "#6b3fa0",
    "#b4532a", "#0f766e", "#4b5563",
)


def display_name(employee: Employee) -> str:
    return " ".join(part for part in (employee.first_name, employee.last_name) if part)


def initials(employee: Employee) -> str:
    parts = [employee.first_name or "", employee.last_name or ""]
    letters = "".join(part.strip()[:1] for part in parts if part.strip())

    return letters.upper() or "?"


def avatar_color(employee: Employee) -> str:
    return AVATAR_COLORS[(employee.id or 0) % len(AVATAR_COLORS)]


def employment_status(employee: Employee, today: date) -> str:
    if employee.termination_date and employee.termination_date < today:
        return "TERMINATED"

    if employee.hire_date and employee.hire_date > today:
        return "INCOMING"

    return "ACTIVE"


# -------------------------------------------------------------------
# Checklist de incorporación y baja
# -------------------------------------------------------------------

MANUAL_CHECKS = {
    "CONTRATA": "Contrato comunicado al SEPE (Contrat@)",
    "PRL": "Formación e información en prevención de riesgos",
    "RECONOCIMIENTO": "Reconocimiento médico ofrecido",
    "RGPD": "Información de protección de datos entregada",
    "HERRAMIENTAS": "Accesos y herramientas de trabajo entregados",
}


def build_checklist(employee: Employee, today: date) -> list[dict[str, Any]]:
    documents = {document.kind for document in employee.documents}
    manual = employee.checklist or {}
    items: list[dict[str, Any]] = []

    def add(code: str, title: str, done: bool, due: date | None, detail: str, auto: bool) -> None:
        if done:
            status = "done"
        elif due and due < today:
            status = "overdue"
        elif due and days_until(due, today) <= 3:
            status = "due_soon"
        else:
            status = "pending"

        items.append(
            {
                "code": code,
                "title": title,
                "status": status,
                "done": done,
                "due_date": due.isoformat() if due else None,
                "detail": detail,
                "automatic": auto,
                "done_at": manual.get(code) if not auto else None,
            }
        )

    hire = employee.hire_date
    # Incorporaciones antiguas (anteriores a usar CapaFiscal) se muestran
    # sin plazos para no generar alertas falsas.
    tracked = bool(hire and hire >= today - timedelta(days=60))

    def due(value: date | None) -> date | None:
        return value if tracked else None

    if hire:
        # El alta en la Seguridad Social debe hacerse antes del inicio.
        alta_due = hire - timedelta(days=1)
        add(
            "ALTA_SS",
            "Alta en la Seguridad Social antes del primer día",
            employee.ss_status in {"ALTA", "BAJA"},
            due(alta_due),
            "Se tramita en Sistema RED como máximo el día anterior al inicio.",
            True,
        )
        add(
            "RESGUARDO_ALTA",
            "Resguardo de alta archivado",
            "ALTA_SS" in documents,
            None,
            "Sube el resguardo (IDC/TA2) en la ficha.",
            True,
        )
        add(
            "CONTRATO",
            "Contrato firmado archivado",
            "CONTRATO" in documents,
            due(hire),
            "Sube el contrato firmado por ambas partes.",
            True,
        )
        contrata_due = add_business_days(hire, 10)
        add(
            "CONTRATA",
            MANUAL_CHECKS["CONTRATA"],
            "CONTRATA" in manual,
            due(contrata_due),
            "Plazo: 10 días hábiles desde la firma del contrato.",
            False,
        )
        add(
            "MODELO_145",
            "Modelo 145 (situación familiar para el IRPF)",
            "MODELO_145" in documents,
            due(hire),
            "Necesario para calcular bien la retención.",
            True,
        )

        for code in ("PRL", "RECONOCIMIENTO", "RGPD", "HERRAMIENTAS"):
            add(code, MANUAL_CHECKS[code], code in manual, due(hire), "", False)

    if employee.termination_date:
        baja_due = employee.termination_date + timedelta(days=3)
        add(
            "BAJA_SS",
            "Baja en la Seguridad Social (hasta 3 días naturales)",
            employee.ss_status == "BAJA",
            baja_due,
            "Se tramita en Sistema RED; guarda el resguardo de baja.",
            True,
        )
        add(
            "FINIQUITO",
            "Finiquito y certificado de empresa",
            "FINIQUITO" in manual,
            employee.termination_date,
            "El certificado de empresa se envía por Certific@2.",
            False,
        )

    return items


# -------------------------------------------------------------------
# Ausencias
# -------------------------------------------------------------------

def business_days_between(start: date, end: date) -> int:
    count = 0
    current = start

    while current <= end:
        if is_business_day(current):
            count += 1
        current += timedelta(days=1)

    return count


def vacation_balance(
    database: Session,
    employee: Employee,
    year: int,
) -> dict[str, int]:
    absences = database.scalars(
        select(Absence).where(
            Absence.employee_id == employee.id,
            Absence.kind == "VACACIONES",
            Absence.start_date <= date(year, 12, 31),
            Absence.end_date >= date(year, 1, 1),
        )
    ).all()

    used = sum(
        business_days_between(max(item.start_date, date(year, 1, 1)), min(item.end_date, date(year, 12, 31)))
        for item in absences
    )

    return {
        "entitled": employee.vacation_days,
        "used": used,
        "remaining": employee.vacation_days - used,
    }


def absences_on(database: Session, day: date) -> list[Absence]:
    return list(
        database.scalars(
            select(Absence)
            .where(Absence.start_date <= day, Absence.end_date >= day)
            .options(selectinload(Absence.employee))
        ).all()
    )


def serialize_absence(absence: Absence) -> dict[str, Any]:
    return {
        "id": absence.id,
        "employee_id": absence.employee_id,
        "employee_name": display_name(absence.employee) if absence.employee else "",
        "kind": absence.kind,
        "kind_label": ABSENCE_KINDS.get(absence.kind, absence.kind),
        "start_date": absence.start_date.isoformat(),
        "end_date": absence.end_date.isoformat(),
        "business_days": business_days_between(absence.start_date, absence.end_date),
        "notes": absence.notes,
    }


def create_absence(database: Session, data: dict[str, Any]) -> Absence:
    if data["kind"] not in ABSENCE_KINDS:
        raise ValueError("Tipo de ausencia no válido.")

    if data["end_date"] < data["start_date"]:
        raise ValueError("La fecha de fin es anterior a la de inicio.")

    employee = database.get(Employee, data["employee_id"])

    if employee is None:
        raise ValueError("Persona no encontrada.")

    overlapping = database.scalar(
        select(Absence.id).where(
            Absence.employee_id == employee.id,
            Absence.start_date <= data["end_date"],
            Absence.end_date >= data["start_date"],
        )
    )

    if overlapping:
        raise ValueError("Ya hay una ausencia registrada en esas fechas.")

    if data["kind"] == "VACACIONES":
        requested = business_days_between(data["start_date"], data["end_date"])
        balance = vacation_balance(database, employee, data["start_date"].year)

        if requested > balance["remaining"]:
            raise ValueError(
                f"Solo le quedan {balance['remaining']} día(s) de vacaciones "
                f"y la solicitud es de {requested}."
            )

    absence = Absence(
        employee_id=employee.id,
        kind=data["kind"],
        start_date=data["start_date"],
        end_date=data["end_date"],
        notes=data.get("notes"),
    )
    database.add(absence)
    database.flush()
    absence.employee = employee

    return absence


# -------------------------------------------------------------------
# Fichas
# -------------------------------------------------------------------

EDITABLE_FIELDS = (
    "first_name", "last_name", "tax_id", "ss_number", "email", "phone",
    "birth_date", "job_title", "department", "manager_id", "hire_date",
    "termination_date", "contract_end_date", "contract_type",
    "workday_percent", "annual_salary", "payments_per_year", "irpf_rate",
    "children", "contribution_group", "collective_agreement", "iban",
    "ss_status", "ss_registered_at", "ss_deregistered_at", "vacation_days",
    "skills", "notes",
)


def apply_employee_changes(
    database: Session,
    employee: Employee,
    changes: dict[str, Any],
) -> Employee:
    for field in EDITABLE_FIELDS:
        if field not in changes:
            continue

        value = changes[field]

        if isinstance(value, str):
            value = value.strip() or None

        if field in {"tax_id", "ss_number", "iban"} and value:
            value = re.sub(r"[\s\-]", "", value).upper()

        setattr(employee, field, value)

    if not employee.first_name:
        raise ValueError("El nombre es obligatorio.")

    if employee.contract_type not in CONTRACT_TYPES:
        raise ValueError("Tipo de contrato no válido.")

    if employee.ss_status not in SS_STATUSES:
        raise ValueError("Estado de Seguridad Social no válido.")

    if employee.payments_per_year not in (12, 14):
        raise ValueError("El número de pagas debe ser 12 o 14.")

    if not 1 <= (employee.workday_percent or 0) <= 100:
        raise ValueError("La jornada debe estar entre el 1 % y el 100 %.")

    if employee.manager_id is not None:
        if employee.id is not None and employee.manager_id == employee.id:
            raise ValueError("Una persona no puede ser su propio responsable.")

        # Evita ciclos en el organigrama.
        seen = {employee.id}
        current = database.get(Employee, employee.manager_id)

        if current is None:
            raise ValueError("El responsable indicado no existe.")

        while current is not None:
            if current.id in seen:
                raise ValueError("Ese responsable crearía un ciclo en el organigrama.")
            seen.add(current.id)
            current = database.get(Employee, current.manager_id) if current.manager_id else None

    # Coherencia automática del estado en la Seguridad Social.
    if employee.ss_registered_at and employee.ss_status == "PENDIENTE_ALTA":
        employee.ss_status = "ALTA"

    if employee.ss_deregistered_at:
        employee.ss_status = "BAJA"

    database.flush()

    return employee


def serialize_employee(
    database: Session,
    employee: Employee,
    *,
    detail: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    current_day = today or date.today()
    checklist = build_checklist(employee, current_day)
    pending = [item for item in checklist if not item["done"]]
    manager = database.get(Employee, employee.manager_id) if employee.manager_id else None
    on_leave = database.scalar(
        select(Absence).where(
            Absence.employee_id == employee.id,
            Absence.start_date <= current_day,
            Absence.end_date >= current_day,
        )
    )

    data: dict[str, Any] = {
        "id": employee.id,
        "name": display_name(employee),
        "first_name": employee.first_name,
        "last_name": employee.last_name,
        "initials": initials(employee),
        "color": avatar_color(employee),
        "job_title": employee.job_title,
        "department": employee.department,
        "manager_id": employee.manager_id,
        "manager_name": display_name(manager) if manager else None,
        "email": employee.email,
        "phone": employee.phone,
        "hire_date": employee.hire_date.isoformat() if employee.hire_date else None,
        "termination_date": employee.termination_date.isoformat() if employee.termination_date else None,
        "contract_type": employee.contract_type,
        "contract_label": CONTRACT_TYPES.get(employee.contract_type, employee.contract_type),
        "contract_end_date": employee.contract_end_date.isoformat() if employee.contract_end_date else None,
        "workday_percent": employee.workday_percent,
        "annual_salary": float(employee.annual_salary) if employee.annual_salary is not None else None,
        "ss_status": employee.ss_status,
        "ss_status_label": SS_STATUSES.get(employee.ss_status, employee.ss_status),
        "status": employment_status(employee, current_day),
        "absent_today": (
            ABSENCE_KINDS.get(on_leave.kind, on_leave.kind) if on_leave else None
        ),
        "pending_checks": len(pending),
        "urgent_checks": sum(1 for item in pending if item["status"] in {"overdue", "due_soon"}),
        "projects": [
            {
                "id": assignment.project_id,
                "name": assignment.project.name if assignment.project else "",
                "allocation": assignment.allocation_percent,
                "role": assignment.role,
            }
            for assignment in employee.assignments
        ],
        "has_cv": any(document.kind == "CV" for document in employee.documents),
        "skills": [skill.strip() for skill in (employee.skills or "").split(",") if skill.strip()],
    }

    if detail:
        data.update(
            {
                "tax_id": employee.tax_id,
                "tax_id_valid": is_valid_spanish_tax_id(employee.tax_id),
                "ss_number": employee.ss_number,
                "birth_date": employee.birth_date.isoformat() if employee.birth_date else None,
                "payments_per_year": employee.payments_per_year,
                "irpf_rate": float(employee.irpf_rate) if employee.irpf_rate is not None else None,
                "children": employee.children,
                "contribution_group": employee.contribution_group,
                "collective_agreement": employee.collective_agreement,
                "iban": employee.iban,
                "ss_registered_at": employee.ss_registered_at.isoformat() if employee.ss_registered_at else None,
                "ss_deregistered_at": employee.ss_deregistered_at.isoformat() if employee.ss_deregistered_at else None,
                "vacation_days": employee.vacation_days,
                "vacation": vacation_balance(database, employee, current_day.year),
                "skills_text": employee.skills,
                "notes": employee.notes,
                "checklist": checklist,
                "documents": [
                    {
                        "id": document.id,
                        "kind": document.kind,
                        "kind_label": DOCUMENT_KINDS.get(document.kind, document.kind),
                        "filename": document.original_filename,
                        "size_bytes": document.size_bytes,
                        "uploaded_at": document.uploaded_at.isoformat(),
                    }
                    for document in employee.documents
                ],
                "reports": [
                    {"id": report.id, "name": display_name(report), "job_title": report.job_title}
                    for report in database.scalars(
                        select(Employee).where(Employee.manager_id == employee.id)
                    ).all()
                ],
            }
        )

    return data


def load_employees(database: Session) -> list[Employee]:
    return list(
        database.scalars(
            select(Employee)
            .options(
                selectinload(Employee.documents),
                selectinload(Employee.assignments).selectinload(ProjectAssignment.project),
            )
            .order_by(Employee.first_name, Employee.last_name)
        ).all()
    )


def get_employee(database: Session, employee_id: int) -> Employee | None:
    return database.scalar(
        select(Employee)
        .where(Employee.id == employee_id)
        .options(
            selectinload(Employee.documents),
            selectinload(Employee.assignments).selectinload(ProjectAssignment.project),
        )
    )


# -------------------------------------------------------------------
# Organigrama
# -------------------------------------------------------------------

def build_org_chart(database: Session, today: date | None = None) -> dict[str, Any]:
    current_day = today or date.today()
    employees = [
        employee
        for employee in load_employees(database)
        if employment_status(employee, current_day) != "TERMINATED"
    ]
    by_id = {employee.id: employee for employee in employees}
    children: dict[int | None, list[Employee]] = {}

    for employee in employees:
        parent = employee.manager_id if employee.manager_id in by_id else None
        children.setdefault(parent, []).append(employee)

    def node(employee: Employee) -> dict[str, Any]:
        return {
            "id": employee.id,
            "name": display_name(employee),
            "initials": initials(employee),
            "color": avatar_color(employee),
            "job_title": employee.job_title,
            "department": employee.department,
            "status": employment_status(employee, current_day),
            "children": [node(child) for child in children.get(employee.id, [])],
        }

    roots = [node(employee) for employee in children.get(None, [])]
    departments: dict[str, int] = {}

    for employee in employees:
        key = employee.department or "Sin departamento"
        departments[key] = departments.get(key, 0) + 1

    return {"roots": roots, "departments": departments, "headcount": len(employees)}


# -------------------------------------------------------------------
# Proyectos
# -------------------------------------------------------------------

def serialize_project(project: Project) -> dict[str, Any]:
    members = [
        {
            "assignment_id": assignment.id,
            "employee_id": assignment.employee_id,
            "name": display_name(assignment.employee) if assignment.employee else "",
            "initials": initials(assignment.employee) if assignment.employee else "?",
            "color": avatar_color(assignment.employee) if assignment.employee else "#999",
            "role": assignment.role,
            "allocation": assignment.allocation_percent,
        }
        for assignment in project.assignments
    ]

    return {
        "id": project.id,
        "name": project.name,
        "client_name": project.client_name,
        "status": project.status,
        "status_label": PROJECT_STATUSES.get(project.status, project.status),
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "end_date": project.end_date.isoformat() if project.end_date else None,
        "description": project.description,
        "members": members,
        "fte": round(sum(member["allocation"] for member in members) / 100, 2),
    }


def list_projects(database: Session) -> list[dict[str, Any]]:
    projects = database.scalars(
        select(Project)
        .options(selectinload(Project.assignments).selectinload(ProjectAssignment.employee))
        .order_by(Project.status, Project.name)
    ).all()

    return [serialize_project(project) for project in projects]


def allocation_by_employee(database: Session) -> dict[int, int]:
    totals: dict[int, int] = {}

    for assignment in database.scalars(
        select(ProjectAssignment)
        .join(Project)
        .where(Project.status == "ACTIVE")
    ).all():
        totals[assignment.employee_id] = totals.get(assignment.employee_id, 0) + assignment.allocation_percent

    return totals


# -------------------------------------------------------------------
# Resumen del equipo
# -------------------------------------------------------------------

def build_team_overview(database: Session, today: date | None = None) -> dict[str, Any]:
    from app.payroll_service import company_at_ep
    from app.payroll_service import simulate_employee_cost

    current_day = today or date.today()
    employees = load_employees(database)
    active = [e for e in employees if employment_status(e, current_day) == "ACTIVE"]
    incoming = [e for e in employees if employment_status(e, current_day) == "INCOMING"]
    at_ep = company_at_ep(database)

    annual_cost = Decimal("0")

    for employee in active + incoming:
        if employee.annual_salary:
            annual_cost += Decimal(str(simulate_employee_cost(employee, year=current_day.year, at_ep_rate=at_ep)["company_cost"]))

    absent = absences_on(database, current_day)
    allocations = allocation_by_employee(database)

    alerts: list[dict[str, Any]] = []

    for employee in employees:
        for item in build_checklist(employee, current_day):
            if item["status"] in {"overdue", "due_soon"}:
                alerts.append(
                    {
                        "employee_id": employee.id,
                        "employee_name": display_name(employee),
                        "title": item["title"],
                        "status": item["status"],
                        "due_date": item["due_date"],
                    }
                )

        if (
            employee.contract_end_date
            and not employee.termination_date
            and 0 <= days_until(employee.contract_end_date, current_day) <= 30
        ):
            alerts.append(
                {
                    "employee_id": employee.id,
                    "employee_name": display_name(employee),
                    "title": "Fin de contrato temporal: decide si renovar o preparar la baja",
                    "status": "due_soon",
                    "due_date": employee.contract_end_date.isoformat(),
                }
            )

    alerts.sort(key=lambda item: (item["status"] != "overdue", item["due_date"] or ""))

    departments: dict[str, int] = {}

    for employee in active:
        key = employee.department or "Sin departamento"
        departments[key] = departments.get(key, 0) + 1

    return {
        "headcount": len(active),
        "incoming": len(incoming),
        "terminated": len(employees) - len(active) - len(incoming),
        "fte": round(sum(e.workday_percent for e in active) / 100, 2),
        "annual_cost": float(annual_cost),
        "absent_today": [serialize_absence(item) for item in absent],
        "alerts": alerts,
        "departments": departments,
        "overallocated": [
            {"employee_id": employee_id, "allocation": total}
            for employee_id, total in allocations.items()
            if total > 100
        ],
        "unassigned": [
            display_name(employee)
            for employee in active
            if employee.id not in allocations
        ],
    }


# -------------------------------------------------------------------
# Lectura de CV
# -------------------------------------------------------------------

EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_PATTERN = re.compile(r"(?:\+34[\s.-]?)?(?:[6789]\d{2}[\s.-]?\d{3}[\s.-]?\d{3}|[6789]\d{2}[\s.-]?\d{2}[\s.-]?\d{2}[\s.-]?\d{2})")


def parse_cv_text(text: str) -> dict[str, Any]:
    """Borrador de ficha a partir del texto de un CV (se revisa antes de guardar)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    normalized = normalize_search_text(text)

    name = None

    for line in lines[:6]:
        clean = line.strip(" -|•")

        if (
            2 <= len(clean.split()) <= 5
            and not any(character.isdigit() for character in clean)
            and "@" not in clean
            and not re.search(r"(?i)curriculum|vitae|cv|perfil|contacto", clean)
        ):
            name = clean.title()
            break

    first_name, last_name = None, None

    if name:
        parts = name.split()
        first_name = parts[0]
        last_name = " ".join(parts[1:]) or None

    email_match = EMAIL_PATTERN.search(text)
    phone_match = PHONE_PATTERN.search(text)
    skills = [skill for skill in SKILL_KEYWORDS if skill in normalized]

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email_match.group(0) if email_match else None,
        "phone": re.sub(r"[\s.-]", " ", phone_match.group(0)).strip() if phone_match else None,
        "skills": ", ".join(SKILL_LABELS.get(skill, skill.capitalize()) for skill in skills) or None,
    }


def store_employee_file(
    *,
    employee: Employee,
    kind: str,
    filename: str,
    content: bytes,
    content_type: str | None,
    upload_dir,
) -> EmployeeDocument:
    import uuid
    from pathlib import Path

    if kind not in DOCUMENT_KINDS:
        raise ValueError("Tipo de documento no válido.")

    extension = Path(filename).suffix.lower()

    if extension not in {".pdf", ".jpg", ".jpeg", ".png", ".doc", ".docx", ".txt"}:
        raise ValueError("Formato no admitido (PDF, imagen, Word o TXT).")

    folder = Path(upload_dir) / "employees"
    folder.mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{extension}"
    (folder / stored).write_bytes(content)

    return EmployeeDocument(
        employee=employee,
        kind=kind,
        original_filename=Path(filename).name[:255],
        stored_filename=f"employees/{stored}",
        mime_type=(content_type or "")[:150] or None,
        size_bytes=len(content),
    )
