"""Equipo y nóminas: fichas, incorporación, ausencias, organigrama y
proceso de nómina completo (cálculo, aprobación, SEPA, PDF, Excel, 111)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import Employee
from app.payroll_service import calculate_payslip
from app.payroll_service import iban_is_valid
from app.team_service import build_checklist
from app.team_service import parse_cv_text

IBAN = "ES2800000000000000000002"  # ficticio: entidad y oficina 0000 (no existen), dígito de control válido


def create_employee(client, **fields) -> dict:
    body = {
        "first_name": "Ana",
        "last_name": "Pérez Gil",
        "job_title": "Administrativa",
        "department": "Administración",
        "hire_date": "2024-01-15",
        "contract_type": "INDEFINIDO",
        "annual_salary": 30000,
        "payments_per_year": 14,
        "iban": IBAN,
        "ss_status": "ALTA",
    }
    body.update(fields)
    response = client.post("/api/team/employees", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_payslip_math_14_payments():
    employee = Employee(
        first_name="Ana",
        annual_salary=Decimal("30000"),
        payments_per_year=14,
        contract_type="INDEFINIDO",
        workday_percent=100,
        children=0,
        hire_date=date(2024, 1, 1),
    )

    ordinary = calculate_payslip(employee, year=2026, month=3)
    assert float(ordinary["gross"]) == 2142.86
    assert float(ordinary["contribution_base"]) == 2500.0
    assert ordinary["net"] < ordinary["gross"]
    assert ordinary["company_cost"] > ordinary["gross"]

    june = calculate_payslip(employee, year=2026, month=6)
    assert float(june["gross"]) == 4285.72
    # La base de cotización no cambia con la paga extra (va prorrateada).
    assert june["contribution_base"] == ordinary["contribution_base"]


def test_partial_month_prorates_days():
    employee = Employee(
        first_name="Pablo",
        annual_salary=Decimal("24000"),
        payments_per_year=12,
        contract_type="TEMPORAL",
        workday_percent=100,
        children=0,
        hire_date=date(2026, 9, 21),
    )
    payslip = calculate_payslip(employee, year=2026, month=9)
    assert payslip["days"] == 10
    assert float(payslip["gross"]) == round(2000 * 10 / 30, 2)


def test_iban_validation():
    assert iban_is_valid(IBAN)
    assert iban_is_valid("ES5500000000000000000001")
    assert not iban_is_valid("ES5500000000000000000009")


def test_checklist_for_recent_hire_has_deadlines():
    employee = Employee(
        first_name="Irene",
        hire_date=date(2026, 9, 21),
        contract_type="INDEFINIDO",
        ss_status="PENDIENTE_ALTA",
        checklist={},
        documents=[],
    )
    items = {item["code"]: item for item in build_checklist(employee, date(2026, 9, 30))}
    assert items["ALTA_SS"]["status"] == "overdue"
    assert items["ALTA_SS"]["due_date"] == "2026-09-20"
    assert "CONTRATA" in items

    veteran = Employee(
        first_name="Lucía",
        hire_date=date(2020, 1, 1),
        contract_type="INDEFINIDO",
        ss_status="ALTA",
        checklist={},
        documents=[],
    )
    for item in build_checklist(veteran, date(2026, 9, 30)):
        assert item["status"] != "overdue"


def test_cv_parse_extracts_contact_data():
    draft = parse_cv_text(
        "María López Fernández\n"
        "Técnica de contabilidad\n"
        "maria.lopez@example.com · 612 345 678\n"
        "Experiencia: contabilidad, nóminas, Excel, inglés\n"
    )
    assert draft["email"] == "maria.lopez@example.com"
    assert "612" in (draft["phone"] or "")
    assert draft["first_name"] == "María"
    assert "Excel" in draft["skills"]
    assert "Nóminas" in draft["skills"]


def test_employee_crud_and_org_chart_cycle(client):
    boss = create_employee(client, first_name="Lucía", last_name="Martín")
    worker = create_employee(client, first_name="Javier", last_name="Ruiz", manager_id=boss["id"])

    chart = client.get("/api/team/org-chart").json()
    assert len(chart["roots"]) == 1
    assert chart["roots"][0]["children"][0]["id"] == worker["id"]

    cycle = client.patch(f"/api/team/employees/{boss['id']}", json={"manager_id": worker["id"]})
    assert cycle.status_code == 422

    itself = client.patch(f"/api/team/employees/{boss['id']}", json={"manager_id": boss["id"]})
    assert itself.status_code == 422


def test_absences_validate_overlap_and_balance(client):
    person = create_employee(client, vacation_days=5)

    first = client.post(
        "/api/team/absences",
        json={"employee_id": person["id"], "kind": "VACACIONES", "start_date": "2026-10-05", "end_date": "2026-10-07"},
    )
    assert first.status_code == 201, first.text

    overlap = client.post(
        "/api/team/absences",
        json={"employee_id": person["id"], "kind": "PERMISO", "start_date": "2026-10-07", "end_date": "2026-10-08"},
    )
    assert overlap.status_code == 422

    over_balance = client.post(
        "/api/team/absences",
        json={"employee_id": person["id"], "kind": "VACACIONES", "start_date": "2026-10-19", "end_date": "2026-10-23"},
    )
    assert over_balance.status_code == 422


def test_payroll_run_lifecycle(client):
    client.put("/api/company", json={"name": "Taller S.L.", "tax_id": "B12345674", "iban": "ES5500000000000000000001"})
    create_employee(client, first_name="Ana")
    create_employee(client, first_name="Luis", annual_salary=24000, payments_per_year=12)

    run = client.post("/api/payroll/runs", json={"year": 2026, "month": 4})
    assert run.status_code == 201, run.text
    run = run.json()
    assert run["status"] == "DRAFT"
    assert run["totals"]["employees"] == 2

    duplicate = client.post("/api/payroll/runs", json={"year": 2026, "month": 4})
    assert duplicate.status_code == 409

    detail = client.get(f"/api/payroll/runs/{run['id']}").json()
    payslip = detail["payslips"][0]
    edited = client.patch(f"/api/payroll/payslips/{payslip['id']}", json={"bonus": 150})
    assert edited.status_code == 200, edited.text
    assert edited.json()["gross"] == round(payslip["gross"] + 150, 2)

    assert client.get(f"/api/payroll/runs/{run['id']}/sepa.xml").status_code == 409

    approved = client.post(f"/api/payroll/runs/{run['id']}/status", json={"status": "APPROVED"})
    assert approved.status_code == 200, approved.text

    locked = client.patch(f"/api/payroll/payslips/{payslip['id']}", json={"bonus": 10})
    assert locked.status_code == 409

    sepa = client.get(f"/api/payroll/runs/{run['id']}/sepa.xml")
    assert sepa.status_code == 200
    assert b"pain.001.001.03" in sepa.content
    assert b"<NbOfTxs>2</NbOfTxs>" in sepa.content

    pdf = client.get(f"/api/payroll/runs/{run['id']}/payslips.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    xlsx = client.get(f"/api/payroll/runs/{run['id']}/summary.xlsx")
    assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")

    model = client.get("/api/taxes/models/111", params={"year": 2026, "quarter": 2}).json()
    boxes = {box["box"]: box["value"] for box in model["boxes"]}
    assert boxes["01"] == 2
    assert boxes["28"] > 0

    assert client.delete(f"/api/payroll/runs/{run['id']}").status_code == 409


def test_uploading_ss_receipt_marks_employee_registered(client):
    person = create_employee(client, ss_status="PENDIENTE_ALTA", hire_date="2026-09-21")

    response = client.post(
        f"/api/team/employees/{person['id']}/documents",
        data={"kind": "ALTA_SS"},
        files={"uploaded_file": ("resguardo.pdf", b"%PDF-1.4 resguardo", "application/pdf")},
    )
    assert response.status_code == 201, response.text

    detail = client.get(f"/api/team/employees/{person['id']}").json()
    assert detail["ss_status"] == "ALTA"
