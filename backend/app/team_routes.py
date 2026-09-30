"""
API de Equipo (personas, documentos, organigrama, proyectos, ausencias)
y Nóminas.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import Response
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.calendar_es import last_day_of_month
from app.calendar_es import next_business_day
from app.config import settings
from app.deps import ActorHeader
from app.deps import DatabaseDependency
from app.deps import normalize_actor
from app.invoice_service import add_audit_event
from app.models import Absence
from app.models import CompanyProfile
from app.models import Employee
from app.models import EmployeeDocument
from app.models import PayrollRun
from app.models import Payslip
from app.models import Project
from app.models import ProjectAssignment
from app.payroll_service import build_payslips_pdf
from app.payroll_service import build_sepa_xml
from app.payroll_service import build_summary_xlsx
from app.payroll_service import company_at_ep
from app.payroll_service import create_run
from app.payroll_service import get_run
from app.payroll_service import iban_is_valid
from app.payroll_service import params_for
from app.payroll_service import recalculate_run
from app.payroll_service import serialize_payslip
from app.payroll_service import serialize_run
from app.payroll_service import set_run_status
from app.payroll_service import simulate_employee_cost
from app.payroll_service import update_payslip_variables
from app.team_service import ABSENCE_KINDS
from app.team_service import CONTRACT_TYPES
from app.team_service import DOCUMENT_KINDS
from app.team_service import MANUAL_CHECKS
from app.team_service import PROJECT_STATUSES
from app.team_service import SS_STATUSES
from app.team_service import apply_employee_changes
from app.team_service import build_org_chart
from app.team_service import build_team_overview
from app.team_service import create_absence
from app.team_service import get_employee
from app.team_service import list_projects
from app.team_service import load_employees
from app.team_service import parse_cv_text
from app.team_service import serialize_absence
from app.team_service import serialize_employee
from app.team_service import serialize_project
from app.team_service import store_employee_file


router = APIRouter(prefix="/api")

MAX_EMPLOYEE_FILE = 10 * 1024 * 1024


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def unprocessable(error: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


# -------------------------------------------------------------------
# Personas
# -------------------------------------------------------------------

class EmployeePayload(BaseModel):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=150)
    tax_id: str | None = Field(default=None, max_length=20)
    ss_number: str | None = Field(default=None, max_length=20)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=30)
    birth_date: date | None = None
    job_title: str | None = Field(default=None, max_length=150)
    department: str | None = Field(default=None, max_length=100)
    manager_id: int | None = None
    hire_date: date | None = None
    termination_date: date | None = None
    contract_end_date: date | None = None
    contract_type: str | None = Field(default=None, max_length=30)
    workday_percent: int | None = Field(default=None, ge=1, le=100)
    annual_salary: Decimal | None = Field(default=None, ge=0, le=10_000_000)
    payments_per_year: int | None = None
    irpf_rate: Decimal | None = Field(default=None, ge=0, le=60)
    children: int | None = Field(default=None, ge=0, le=20)
    contribution_group: int | None = Field(default=None, ge=1, le=11)
    collective_agreement: str | None = Field(default=None, max_length=255)
    iban: str | None = Field(default=None, max_length=40)
    ss_status: str | None = Field(default=None, max_length=20)
    ss_registered_at: date | None = None
    ss_deregistered_at: date | None = None
    vacation_days: int | None = Field(default=None, ge=0, le=60)
    skills: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=5000)


class ChecklistPayload(BaseModel):
    code: str = Field(max_length=30)
    done: bool = True


@router.get("/team/catalog", tags=["Equipo"])
def team_catalog() -> dict[str, Any]:
    def items(mapping: dict[str, str]) -> list[dict[str, str]]:
        return [{"code": code, "label": label} for code, label in mapping.items()]

    return {
        "contract_types": items(CONTRACT_TYPES),
        "ss_statuses": items(SS_STATUSES),
        "document_kinds": items(DOCUMENT_KINDS),
        "absence_kinds": items(ABSENCE_KINDS),
        "project_statuses": items(PROJECT_STATUSES),
    }


@router.get("/team/overview", tags=["Equipo"])
def team_overview(database: DatabaseDependency) -> dict[str, Any]:
    return build_team_overview(database)


@router.get("/team/employees", tags=["Equipo"])
def list_employees(database: DatabaseDependency) -> list[dict[str, Any]]:
    return [serialize_employee(database, employee) for employee in load_employees(database)]


@router.post("/team/employees", tags=["Equipo"], status_code=201)
def create_employee(
    payload: EmployeePayload,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    employee = Employee(
        first_name=changes.get("first_name") or "",
        contract_type="INDEFINIDO",
        ss_status="PENDIENTE_ALTA",
        payments_per_year=14,
        workday_percent=100,
        vacation_days=22,
        children=0,
        checklist={},
    )
    database.add(employee)

    try:
        apply_employee_changes(database, employee, changes)
    except ValueError as error:
        database.rollback()
        raise unprocessable(error) from error

    add_audit_event(
        database,
        action="employee.created",
        entity_type="employee",
        entity_id=employee.id,
        actor=normalize_actor(actor_header),
        event_data={"name": f"{employee.first_name} {employee.last_name or ''}".strip()},
    )
    database.commit()

    return serialize_employee(database, get_employee(database, employee.id), detail=True)


@router.get("/team/employees/{employee_id}", tags=["Equipo"])
def employee_detail(employee_id: int, database: DatabaseDependency) -> dict[str, Any]:
    employee = get_employee(database, employee_id)

    if employee is None:
        raise not_found("Persona no encontrada.")

    data = serialize_employee(database, employee, detail=True)

    if employee.annual_salary:
        data["cost"] = simulate_employee_cost(
            employee,
            year=date.today().year,
            at_ep_rate=company_at_ep(database),
        )

    return data


@router.patch("/team/employees/{employee_id}", tags=["Equipo"])
def update_employee(
    employee_id: int,
    payload: EmployeePayload,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    employee = get_employee(database, employee_id)

    if employee is None:
        raise not_found("Persona no encontrada.")

    changes = payload.model_dump(exclude_unset=True)

    try:
        apply_employee_changes(database, employee, changes)
    except ValueError as error:
        database.rollback()
        raise unprocessable(error) from error

    add_audit_event(
        database,
        action="employee.updated",
        entity_type="employee",
        entity_id=employee.id,
        actor=normalize_actor(actor_header),
        event_data={"fields": sorted(changes)},
    )
    database.commit()

    return employee_detail(employee_id, database)


@router.delete("/team/employees/{employee_id}", tags=["Equipo"])
def delete_employee(
    employee_id: int,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    employee = get_employee(database, employee_id)

    if employee is None:
        raise not_found("Persona no encontrada.")

    has_payslips = database.scalar(select(Payslip.id).where(Payslip.employee_id == employee_id).limit(1))

    if has_payslips:
        raise HTTPException(
            status_code=409,
            detail="Tiene nóminas registradas: indica una fecha de baja en lugar de borrarla.",
        )

    for document in employee.documents:
        (settings.upload_dir / document.stored_filename).unlink(missing_ok=True)

    for report in database.scalars(select(Employee).where(Employee.manager_id == employee_id)).all():
        report.manager_id = employee.manager_id

    add_audit_event(
        database,
        action="employee.deleted",
        entity_type="employee",
        entity_id=employee_id,
        actor=normalize_actor(actor_header),
        event_data={"name": f"{employee.first_name} {employee.last_name or ''}".strip()},
    )
    database.delete(employee)
    database.commit()

    return {"success": True, "message": "Ficha eliminada."}


@router.post("/team/employees/{employee_id}/checklist", tags=["Equipo"])
def toggle_checklist(
    employee_id: int,
    payload: ChecklistPayload,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    employee = get_employee(database, employee_id)

    if employee is None:
        raise not_found("Persona no encontrada.")

    if payload.code not in MANUAL_CHECKS and payload.code != "FINIQUITO":
        raise HTTPException(status_code=422, detail="Esta tarea se completa sola al subir el documento o registrar el alta.")

    checklist = dict(employee.checklist or {})

    if payload.done:
        checklist[payload.code] = date.today().isoformat()
    else:
        checklist.pop(payload.code, None)

    employee.checklist = checklist
    add_audit_event(
        database,
        action="employee.checklist",
        entity_type="employee",
        entity_id=employee.id,
        actor=normalize_actor(actor_header),
        event_data={"code": payload.code, "done": payload.done},
    )
    database.commit()

    return employee_detail(employee_id, database)


@router.post("/team/employees/{employee_id}/documents", tags=["Equipo"], status_code=201)
async def upload_employee_document(
    employee_id: int,
    database: DatabaseDependency,
    uploaded_file: UploadFile = File(...),
    kind: str = Form(...),
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    employee = get_employee(database, employee_id)

    if employee is None:
        raise not_found("Persona no encontrada.")

    content = await uploaded_file.read(MAX_EMPLOYEE_FILE + 1)
    await uploaded_file.close()

    if len(content) > MAX_EMPLOYEE_FILE:
        raise HTTPException(status_code=413, detail="El archivo supera 10 MB.")

    try:
        document = store_employee_file(
            employee=employee,
            kind=kind,
            filename=uploaded_file.filename or "documento",
            content=content,
            content_type=uploaded_file.content_type,
            upload_dir=settings.upload_dir,
        )
    except ValueError as error:
        raise unprocessable(error) from error

    database.add(document)

    # El resguardo confirma el alta o la baja en la Seguridad Social.
    if kind == "ALTA_SS" and employee.ss_status == "PENDIENTE_ALTA":
        employee.ss_status = "ALTA"
        employee.ss_registered_at = employee.ss_registered_at or employee.hire_date or date.today()

    if kind == "BAJA_SS":
        employee.ss_status = "BAJA"
        employee.ss_deregistered_at = employee.ss_deregistered_at or employee.termination_date or date.today()

    add_audit_event(
        database,
        action="employee.document_uploaded",
        entity_type="employee",
        entity_id=employee.id,
        actor=normalize_actor(actor_header),
        event_data={"kind": kind, "filename": uploaded_file.filename},
    )
    database.commit()

    return employee_detail(employee_id, database)


@router.get("/team/documents/{document_id}/file", tags=["Equipo"])
def employee_document_file(document_id: int, database: DatabaseDependency) -> FileResponse:
    document = database.get(EmployeeDocument, document_id)

    if document is None:
        raise not_found("Documento no encontrado.")

    base = settings.upload_dir.resolve()
    path = (base / document.stored_filename).resolve()

    try:
        path.relative_to(base)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Ruta no válida.") from error

    if not path.is_file():
        raise not_found("El archivo no está en el disco.")

    return FileResponse(
        path=path,
        filename=document.original_filename,
        media_type=document.mime_type or "application/octet-stream",
        content_disposition_type="inline",
    )


@router.delete("/team/documents/{document_id}", tags=["Equipo"])
def delete_employee_document(
    document_id: int,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    document = database.get(EmployeeDocument, document_id)

    if document is None:
        raise not_found("Documento no encontrado.")

    (settings.upload_dir / document.stored_filename).unlink(missing_ok=True)
    add_audit_event(
        database,
        action="employee.document_deleted",
        entity_type="employee",
        entity_id=document.employee_id,
        actor=normalize_actor(actor_header),
        event_data={"kind": document.kind, "filename": document.original_filename},
    )
    database.delete(document)
    database.commit()

    return {"success": True}


@router.post("/team/cv/parse", tags=["Equipo"])
async def parse_cv(uploaded_file: UploadFile = File(...)) -> dict[str, Any]:
    """Lee un CV y devuelve un borrador de ficha (no guarda nada)."""
    import tempfile

    from app.extractor import read_document

    content = await uploaded_file.read(MAX_EMPLOYEE_FILE + 1)
    await uploaded_file.close()
    suffix = Path(uploaded_file.filename or "cv.pdf").suffix.lower()

    if suffix not in {".pdf", ".txt"}:
        raise HTTPException(status_code=400, detail="Sube el CV en PDF o TXT para leerlo.")

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)

    try:
        text, _pages, requires_ocr, _page_texts = read_document(temporary)
    finally:
        temporary.unlink(missing_ok=True)

    if requires_ocr or not text.strip():
        raise HTTPException(status_code=422, detail="El CV no tiene texto legible (¿es una imagen escaneada?).")

    return parse_cv_text(text)


@router.get("/team/org-chart", tags=["Equipo"])
def org_chart(database: DatabaseDependency) -> dict[str, Any]:
    return build_org_chart(database)


@router.get("/team/simulate", tags=["Equipo"])
def simulate_cost(
    database: DatabaseDependency,
    annual_salary: Decimal = Query(ge=0, le=10_000_000),
    payments_per_year: int = Query(default=14),
    contract_type: str = Query(default="INDEFINIDO"),
    children: int = Query(default=0, ge=0, le=20),
    workday_percent: int = Query(default=100, ge=1, le=100),
) -> dict[str, Any]:
    employee = Employee(
        first_name="Simulación",
        annual_salary=annual_salary,
        payments_per_year=payments_per_year if payments_per_year in (12, 14) else 14,
        contract_type=contract_type if contract_type in CONTRACT_TYPES else "INDEFINIDO",
        children=children,
        workday_percent=workday_percent,
    )

    return simulate_employee_cost(
        employee,
        year=date.today().year,
        at_ep_rate=company_at_ep(database),
    )


# -------------------------------------------------------------------
# Proyectos
# -------------------------------------------------------------------

class ProjectPayload(BaseModel):
    name: str | None = Field(default=None, max_length=150)
    client_name: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, max_length=20)
    start_date: date | None = None
    end_date: date | None = None
    description: str | None = Field(default=None, max_length=5000)


class AssignmentPayload(BaseModel):
    employee_id: int
    role: str | None = Field(default=None, max_length=100)
    allocation_percent: int = Field(default=100, ge=1, le=100)


@router.get("/team/projects", tags=["Equipo"])
def get_projects(database: DatabaseDependency) -> list[dict[str, Any]]:
    return list_projects(database)


def load_project(database, project_id: int) -> Project:
    project = database.scalar(
        select(Project)
        .where(Project.id == project_id)
        .options(selectinload(Project.assignments).selectinload(ProjectAssignment.employee))
    )

    if project is None:
        raise not_found("Proyecto no encontrado.")

    return project


def apply_project(project: Project, changes: dict[str, Any]) -> None:
    for field, value in changes.items():
        setattr(project, field, value.strip() if isinstance(value, str) else value)

    if not project.name:
        raise ValueError("El proyecto necesita un nombre.")

    if project.status not in PROJECT_STATUSES:
        raise ValueError("Estado de proyecto no válido.")


@router.post("/team/projects", tags=["Equipo"], status_code=201)
def create_project(payload: ProjectPayload, database: DatabaseDependency) -> dict[str, Any]:
    project = Project(status="ACTIVE", name="")

    try:
        apply_project(project, payload.model_dump(exclude_unset=True))
    except ValueError as error:
        raise unprocessable(error) from error

    database.add(project)
    database.commit()

    return serialize_project(load_project(database, project.id))


@router.patch("/team/projects/{project_id}", tags=["Equipo"])
def update_project(project_id: int, payload: ProjectPayload, database: DatabaseDependency) -> dict[str, Any]:
    project = load_project(database, project_id)

    try:
        apply_project(project, payload.model_dump(exclude_unset=True))
    except ValueError as error:
        database.rollback()
        raise unprocessable(error) from error

    database.commit()

    return serialize_project(load_project(database, project_id))


@router.delete("/team/projects/{project_id}", tags=["Equipo"])
def delete_project(project_id: int, database: DatabaseDependency) -> dict[str, Any]:
    project = load_project(database, project_id)
    database.delete(project)
    database.commit()

    return {"success": True}


@router.post("/team/projects/{project_id}/members", tags=["Equipo"])
def add_member(project_id: int, payload: AssignmentPayload, database: DatabaseDependency) -> dict[str, Any]:
    project = load_project(database, project_id)

    if database.get(Employee, payload.employee_id) is None:
        raise not_found("Persona no encontrada.")

    assignment = database.scalar(
        select(ProjectAssignment).where(
            ProjectAssignment.project_id == project_id,
            ProjectAssignment.employee_id == payload.employee_id,
        )
    )

    if assignment is None:
        assignment = ProjectAssignment(project_id=project.id, employee_id=payload.employee_id)
        database.add(assignment)

    assignment.role = (payload.role or "").strip() or None
    assignment.allocation_percent = payload.allocation_percent
    database.commit()
    database.expire_all()

    return serialize_project(load_project(database, project_id))


@router.delete("/team/assignments/{assignment_id}", tags=["Equipo"])
def remove_member(assignment_id: int, database: DatabaseDependency) -> dict[str, Any]:
    assignment = database.get(ProjectAssignment, assignment_id)

    if assignment is None:
        raise not_found("Asignación no encontrada.")

    project_id = assignment.project_id
    database.delete(assignment)
    database.commit()
    database.expire_all()

    return serialize_project(load_project(database, project_id))


# -------------------------------------------------------------------
# Ausencias
# -------------------------------------------------------------------

class AbsencePayload(BaseModel):
    employee_id: int
    kind: str = Field(max_length=30)
    start_date: date
    end_date: date
    notes: str | None = Field(default=None, max_length=1000)


@router.get("/team/absences", tags=["Equipo"])
def list_absences(
    database: DatabaseDependency,
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
) -> list[dict[str, Any]]:
    today = date.today()
    start = date_from or date(today.year, today.month, 1)
    end = date_to or last_day_of_month(today.year, today.month)
    absences = database.scalars(
        select(Absence)
        .where(Absence.start_date <= end, Absence.end_date >= start)
        .options(selectinload(Absence.employee))
        .order_by(Absence.start_date)
    ).all()

    return [serialize_absence(absence) for absence in absences]


@router.post("/team/absences", tags=["Equipo"], status_code=201)
def post_absence(
    payload: AbsencePayload,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    try:
        absence = create_absence(database, payload.model_dump())
    except ValueError as error:
        database.rollback()
        raise unprocessable(error) from error

    add_audit_event(
        database,
        action="absence.created",
        entity_type="employee",
        entity_id=absence.employee_id,
        actor=normalize_actor(actor_header),
        event_data={
            "kind": absence.kind,
            "start_date": absence.start_date,
            "end_date": absence.end_date,
        },
    )
    database.commit()

    return serialize_absence(absence)


@router.delete("/team/absences/{absence_id}", tags=["Equipo"])
def delete_absence(absence_id: int, database: DatabaseDependency) -> dict[str, Any]:
    absence = database.get(Absence, absence_id)

    if absence is None:
        raise not_found("Ausencia no encontrada.")

    database.delete(absence)
    database.commit()

    return {"success": True}


# -------------------------------------------------------------------
# Nóminas
# -------------------------------------------------------------------

class RunPayload(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class StatusPayload(BaseModel):
    status: str = Field(pattern="^(DRAFT|APPROVED|PAID)$")
    paid_at: date | None = None


class PayslipPayload(BaseModel):
    overtime: Decimal | None = Field(default=None, ge=0, le=100000)
    bonus: Decimal | None = Field(default=None, ge=0, le=100000)
    advance: Decimal | None = Field(default=None, ge=0, le=100000)


def run_or_404(database, run_id: int) -> PayrollRun:
    run = get_run(database, run_id)

    if run is None:
        raise not_found("Nómina no encontrada.")

    return run


@router.get("/payroll/runs", tags=["Nóminas"])
def list_runs(database: DatabaseDependency) -> dict[str, Any]:
    runs = database.scalars(
        select(PayrollRun)
        .options(selectinload(PayrollRun.payslips))
        .order_by(PayrollRun.year.desc(), PayrollRun.month.desc())
    ).all()
    today = date.today()

    return {
        "runs": [serialize_run(run) for run in runs],
        "params": params_for(today.year).source,
        "current_period_done": any(run.year == today.year and run.month == today.month for run in runs),
    }


@router.post("/payroll/runs", tags=["Nóminas"], status_code=201)
def post_run(
    payload: RunPayload,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    try:
        run = create_run(database, year=payload.year, month=payload.month)
    except ValueError as error:
        database.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error

    add_audit_event(
        database,
        action="payroll.created",
        entity_type="payroll",
        entity_id=run.id,
        actor=normalize_actor(actor_header),
        event_data={"year": run.year, "month": run.month, "payslips": len(run.payslips)},
    )
    database.commit()

    return serialize_run(run_or_404(database, run.id), with_payslips=True)


@router.get("/payroll/runs/{run_id}", tags=["Nóminas"])
def run_detail(run_id: int, database: DatabaseDependency) -> dict[str, Any]:
    run = run_or_404(database, run_id)
    data = serialize_run(run, with_payslips=True)
    company = database.scalar(select(CompanyProfile).limit(1))
    data["company_iban_ok"] = bool(company and company.iban and iban_is_valid(company.iban))

    return data


@router.post("/payroll/runs/{run_id}/recalculate", tags=["Nóminas"])
def recalculate(run_id: int, database: DatabaseDependency) -> dict[str, Any]:
    run = run_or_404(database, run_id)

    try:
        recalculate_run(database, run)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    database.commit()

    return run_detail(run_id, database)


@router.post("/payroll/runs/{run_id}/status", tags=["Nóminas"])
def change_status(
    run_id: int,
    payload: StatusPayload,
    database: DatabaseDependency,
    actor_header: ActorHeader = None,
) -> dict[str, Any]:
    run = run_or_404(database, run_id)

    try:
        set_run_status(run, payload.status, payload.paid_at)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

    add_audit_event(
        database,
        action=f"payroll.{payload.status.lower()}",
        entity_type="payroll",
        entity_id=run.id,
        actor=normalize_actor(actor_header),
        event_data={"year": run.year, "month": run.month},
    )
    database.commit()

    return run_detail(run_id, database)


@router.delete("/payroll/runs/{run_id}", tags=["Nóminas"])
def delete_run(run_id: int, database: DatabaseDependency) -> dict[str, Any]:
    run = run_or_404(database, run_id)

    if run.status != "DRAFT":
        raise HTTPException(status_code=409, detail="Solo se pueden borrar nóminas en borrador.")

    database.delete(run)
    database.commit()

    return {"success": True}


@router.patch("/payroll/payslips/{payslip_id}", tags=["Nóminas"])
def update_payslip(
    payslip_id: int,
    payload: PayslipPayload,
    database: DatabaseDependency,
) -> dict[str, Any]:
    payslip = database.scalar(
        select(Payslip)
        .where(Payslip.id == payslip_id)
        .options(selectinload(Payslip.run), selectinload(Payslip.employee))
    )

    if payslip is None:
        raise not_found("Recibo no encontrado.")

    try:
        update_payslip_variables(database, payslip, payload.model_dump(exclude_unset=True))
    except ValueError as error:
        database.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error

    database.commit()

    return serialize_payslip(payslip)


def file_response(content: bytes, filename: str, media_type: str, inline: bool = False) -> Response:
    disposition = "inline" if inline else "attachment"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/payroll/runs/{run_id}/payslips.pdf", tags=["Nóminas"])
def payslips_pdf(run_id: int, database: DatabaseDependency) -> Response:
    run = run_or_404(database, run_id)
    company = database.scalar(select(CompanyProfile).limit(1))

    return file_response(
        build_payslips_pdf(run, company),
        f"nominas_{run.year}_{run.month:02d}.pdf",
        "application/pdf",
        inline=True,
    )


@router.get("/payroll/payslips/{payslip_id}/pdf", tags=["Nóminas"])
def payslip_pdf(payslip_id: int, database: DatabaseDependency) -> Response:
    payslip = database.get(Payslip, payslip_id)

    if payslip is None:
        raise not_found("Recibo no encontrado.")

    run = run_or_404(database, payslip.run_id)
    company = database.scalar(select(CompanyProfile).limit(1))
    selected = [item for item in run.payslips if item.id == payslip_id]
    safe_name = "".join(ch if ch.isalnum() else "_" for ch in payslip.employee_name)[:40]

    return file_response(
        build_payslips_pdf(run, company, selected),
        f"nomina_{run.year}_{run.month:02d}_{safe_name}.pdf",
        "application/pdf",
        inline=True,
    )


@router.get("/payroll/runs/{run_id}/sepa.xml", tags=["Nóminas"])
def payroll_sepa(
    run_id: int,
    database: DatabaseDependency,
    execution_date: date | None = Query(default=None),
    actor_header: ActorHeader = None,
) -> Response:
    run = run_or_404(database, run_id)

    if run.status == "DRAFT":
        raise HTTPException(status_code=409, detail="Aprueba la nómina antes de generar la remesa de pago.")

    company = database.scalar(select(CompanyProfile).limit(1))
    when = execution_date or next_business_day(date.today())

    try:
        content, skipped = build_sepa_xml(run, company, execution_date=when)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    add_audit_event(
        database,
        action="payroll.sepa_generated",
        entity_type="payroll",
        entity_id=run.id,
        actor=normalize_actor(actor_header),
        event_data={"execution_date": when, "skipped": skipped},
    )
    database.commit()

    response = file_response(content, f"remesa_nominas_{run.year}_{run.month:02d}.xml", "application/xml")

    if skipped:
        response.headers["X-Skipped-Employees"] = ", ".join(skipped).encode("ascii", "ignore").decode()

    return response


@router.get("/payroll/runs/{run_id}/summary.xlsx", tags=["Nóminas"])
def payroll_summary(run_id: int, database: DatabaseDependency) -> Response:
    run = run_or_404(database, run_id)

    return file_response(
        build_summary_xlsx(run),
        f"resumen_nominas_{run.year}_{run.month:02d}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
