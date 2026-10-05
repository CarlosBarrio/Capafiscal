"""
Borradores de modelos tributarios y calendario fiscal.

Los cálculos usan solo facturas aprobadas en CapaFiscal y se presentan
como borradores orientativos: no incluyen nóminas, bienes de inversión,
prorrata, regímenes especiales ni compensaciones de periodos anteriores.
"""
from __future__ import annotations

from app import clock
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calendar_es import days_until
from app.calendar_es import last_day_of_month
from app.calendar_es import next_business_day
from app.company_service import legal_form
from app.models import Invoice
from app.models import TaxFiling
from app.reports_service import RECEIVED
from app.reports_service import ZERO
from app.reports_service import counterparty
from app.reports_service import format_eur
from app.reports_service import invoice_tax_breakdown
from app.reports_service import is_issued
from app.reports_service import load_invoices_in_period
from app.reports_service import money
from app.reports_service import normalize_rate
from app.reports_service import quarter_range


RENT_CATEGORY = "Arrendamientos"
THRESHOLD_347 = Decimal("3005.06")

MODEL_NAMES = {
    "303": "IVA trimestral",
    "130": "Pago fraccionado IRPF (autónomos)",
    "111": "Retenciones de profesionales y trabajo",
    "115": "Retenciones de alquileres",
    "202": "Pago fraccionado Impuesto sobre Sociedades",
    "390": "Resumen anual de IVA",
    "190": "Resumen anual de retenciones (111)",
    "180": "Resumen anual de retenciones de alquileres (115)",
    "347": "Operaciones con terceros > 3.005,06 €",
    "200": "Impuesto sobre Sociedades",
    "100": "Renta (IRPF)",
}

DUE_SOON_DAYS = 15


def approved(invoices: list[Invoice]) -> list[Invoice]:
    return [
        invoice
        for invoice in invoices
        if invoice.review_status == "APPROVED"
    ]


def pending_warning(
    database: Session,
    date_from: date,
    date_to: date,
) -> list[str]:
    pending = [
        invoice
        for invoice in load_invoices_in_period(
            database,
            date_from=date_from,
            date_to=date_to,
            review_statuses={"PENDING"},
        )
    ]

    if not pending:
        return []

    return [
        "1 factura del periodo sigue pendiente de revisión y no se incluye. Revísala para que el borrador sea completo."
        if len(pending) == 1 else
        f"{len(pending)} facturas del periodo siguen pendientes de revisión y no se incluyen. Revísalas para que el borrador sea completo."
    ]


def as_float(value: Decimal) -> float:
    return float(money(value))


# -------------------------------------------------------------------
# Modelo 303
# -------------------------------------------------------------------

VAT_BOXES = {
    Decimal("4"): ("01", "02", "03"),
    Decimal("10"): ("04", "05", "06"),
    Decimal("21"): ("07", "08", "09"),
}


def build_model_303(
    database: Session,
    *,
    year: int,
    quarter: int,
) -> dict[str, Any]:
    date_from, date_to = quarter_range(year, quarter)
    invoices = approved(
        load_invoices_in_period(
            database,
            date_from=date_from,
            date_to=date_to,
        )
    )

    accrued: dict[Decimal, dict[str, Decimal]] = defaultdict(
        lambda: {"base": ZERO, "tax": ZERO}
    )
    other_accrued = {"base": ZERO, "tax": ZERO}
    deductible_base = ZERO
    deductible_tax = ZERO
    issued_count = 0
    received_count = 0

    for invoice in invoices:
        if is_issued(invoice):
            issued_count += 1

            for line in invoice_tax_breakdown(invoice):
                rate = (
                    normalize_rate(line["tax_rate"])
                    if line["tax_rate"] is not None
                    else None
                )
                bucket = accrued[rate] if rate in VAT_BOXES else other_accrued
                bucket["base"] += money(line["tax_base"])
                bucket["tax"] += money(line["tax_amount"])
        else:
            received_count += 1
            deductible_base += money(invoice.subtotal)
            deductible_tax += money(invoice.tax_total)

    boxes: list[dict[str, Any]] = []
    total_accrued = ZERO

    for rate, (base_box, rate_box, tax_box) in VAT_BOXES.items():
        values = accrued.get(rate, {"base": ZERO, "tax": ZERO})
        total_accrued += values["tax"]
        boxes.extend(
            [
                {
                    "box": base_box,
                    "label": f"Base imponible al {rate} %",
                    "value": as_float(values["base"]),
                },
                {
                    "box": rate_box,
                    "label": "Tipo %",
                    "value": float(rate),
                    "is_rate": True,
                },
                {
                    "box": tax_box,
                    "label": f"Cuota devengada al {rate} %",
                    "value": as_float(values["tax"]),
                },
            ]
        )

    total_accrued += other_accrued["tax"]
    result = total_accrued - deductible_tax

    boxes.extend(
        [
            {
                "box": "27",
                "label": "Total cuota devengada",
                "value": as_float(total_accrued),
                "highlight": True,
            },
            {
                "box": "28",
                "label": "Base IVA deducible (operaciones interiores corrientes)",
                "value": as_float(deductible_base),
            },
            {
                "box": "29",
                "label": "Cuota IVA deducible",
                "value": as_float(deductible_tax),
            },
            {
                "box": "45",
                "label": "Total a deducir",
                "value": as_float(deductible_tax),
                "highlight": True,
            },
            {
                "box": "46",
                "label": "Resultado régimen general (27 − 45)",
                "value": as_float(result),
                "highlight": True,
            },
        ]
    )

    warnings = pending_warning(database, date_from, date_to)

    if other_accrued["tax"] or other_accrued["base"]:
        warnings.append(
            "Hay ventas con tipos distintos de 4 %, 10 % y 21 % "
            f"(base {format_eur(other_accrued['base'])}). Se suman al total "
            "devengado pero debes revisar su casilla."
        )

    if not issued_count:
        warnings.append(
            "No hay facturas emitidas aprobadas en el trimestre. Si has "
            "facturado, súbelas para calcular el IVA repercutido."
        )

    if result < 0:
        outcome = (
            "A devolver" if quarter == 4 else "A compensar en próximos trimestres"
        )
    elif result == 0:
        outcome = "Sin actividad / resultado cero"
    else:
        outcome = "A ingresar"

    return {
        "model": "303",
        "name": MODEL_NAMES["303"],
        "year": year,
        "quarter": quarter,
        "period_label": f"{quarter}T {year}",
        "boxes": boxes,
        "result": as_float(result),
        "outcome": outcome,
        "issued_invoices": issued_count,
        "received_invoices": received_count,
        "warnings": warnings,
        "note": (
            "Borrador orientativo. No incluye bienes de inversión, "
            "adquisiciones intracomunitarias, importaciones, prorrata, "
            "recargo de equivalencia ni cuotas a compensar de periodos "
            "anteriores (casilla 110)."
        ),
    }


# -------------------------------------------------------------------
# Modelo 130 (autónomos en estimación directa)
# -------------------------------------------------------------------

def _model_130_raw(
    database: Session,
    year: int,
    quarter: int,
) -> dict[str, Decimal]:
    date_from = date(year, 1, 1)
    _start, date_to = quarter_range(year, quarter)

    invoices = approved(
        load_invoices_in_period(
            database,
            date_from=date_from,
            date_to=date_to,
        )
    )

    income = sum(
        (money(invoice.subtotal) for invoice in invoices if is_issued(invoice)),
        ZERO,
    )
    expenses = sum(
        (
            money(invoice.subtotal)
            for invoice in invoices
            if not is_issued(invoice)
        ),
        ZERO,
    )
    withholdings = sum(
        (
            money(invoice.withholding_total)
            for invoice in invoices
            if is_issued(invoice)
        ),
        ZERO,
    )

    net = income - expenses
    twenty_percent = (net * Decimal("0.20")).quantize(Decimal("0.01"))

    if twenty_percent < 0:
        twenty_percent = ZERO

    return {
        "income": income,
        "expenses": expenses,
        "net": net,
        "twenty_percent": twenty_percent,
        "withholdings": withholdings,
    }


def build_model_130(
    database: Session,
    *,
    year: int,
    quarter: int,
) -> dict[str, Any]:
    # Los pagos de trimestres anteriores (casilla 05) son los resultados
    # positivos de los 130 anteriores del mismo año.
    previous_payments = ZERO

    for previous_quarter in range(1, quarter):
        raw = _model_130_raw(database, year, previous_quarter)
        previous_result = (
            raw["twenty_percent"] - previous_payments - raw["withholdings"]
        )

        if previous_result > 0:
            previous_payments += previous_result

    raw = _model_130_raw(database, year, quarter)
    result = raw["twenty_percent"] - previous_payments - raw["withholdings"]
    to_pay = result if result > 0 else ZERO

    date_from, date_to = quarter_range(year, quarter)
    warnings = pending_warning(database, date(year, 1, 1), date_to)

    if legal_form(database) != "AUTONOMO":
        warnings.append(
            "Tu empresa figura como sociedad: el modelo 130 es solo para "
            "autónomos en estimación directa. Cambia la forma jurídica en "
            "Mi empresa si eres autónomo."
        )

    return {
        "model": "130",
        "name": MODEL_NAMES["130"],
        "year": year,
        "quarter": quarter,
        "period_label": f"{quarter}T {year}",
        "boxes": [
            {"box": "01", "label": "Ingresos computables (acumulado año)", "value": as_float(raw["income"])},
            {"box": "02", "label": "Gastos fiscalmente deducibles (acumulado año)", "value": as_float(raw["expenses"])},
            {"box": "03", "label": "Rendimiento neto (01 − 02)", "value": as_float(raw["net"]), "highlight": True},
            {"box": "04", "label": "20 % del rendimiento neto", "value": as_float(raw["twenty_percent"])},
            {"box": "05", "label": "Pagos fraccionados de trimestres anteriores", "value": as_float(previous_payments)},
            {"box": "06", "label": "Retenciones e ingresos a cuenta soportados", "value": as_float(raw["withholdings"])},
            {"box": "07", "label": "Resultado (04 − 05 − 06)", "value": as_float(result), "highlight": True},
        ],
        "result": as_float(to_pay),
        "outcome": "A ingresar" if to_pay > 0 else "Sin importe a ingresar",
        "warnings": warnings,
        "note": (
            "Borrador orientativo con todas las facturas recibidas aprobadas "
            "como gasto deducible. No incluye cuota de autónomos, "
            "amortizaciones ni gastos sin factura: añádelos en la "
            "presentación real."
        ),
    }


# -------------------------------------------------------------------
# Modelos 111 y 115 (retenciones practicadas)
# -------------------------------------------------------------------

def payroll_withholdings(
    database: Session,
    year: int,
    quarter: int,
) -> dict[str, Any]:
    """Rendimientos del trabajo de nóminas aprobadas del trimestre."""
    from app.models import PayrollRun

    months = range((quarter - 1) * 3 + 1, quarter * 3 + 1)
    runs = database.scalars(
        select(PayrollRun).where(
            PayrollRun.year == year,
            PayrollRun.month.in_(list(months)),
            PayrollRun.status.in_({"APPROVED", "PAID"}),
        )
    ).all()

    employees = set()
    gross = ZERO
    irpf = ZERO

    for run in runs:
        for payslip in run.payslips:
            employees.add(payslip.employee_id or payslip.employee_name)
            gross += money(payslip.gross)
            irpf += money(payslip.irpf)

    return {"recipients": len(employees), "gross": gross, "irpf": irpf, "runs": len(runs)}


def _withholding_model(
    database: Session,
    *,
    year: int,
    quarter: int,
    rent: bool,
) -> dict[str, Any]:
    date_from, date_to = quarter_range(year, quarter)
    invoices = [
        invoice
        for invoice in approved(
            load_invoices_in_period(
                database,
                date_from=date_from,
                date_to=date_to,
                direction=RECEIVED,
            )
        )
        if money(invoice.withholding_total) > 0
        and ((invoice.category == RENT_CATEGORY) == rent)
    ]

    recipients = {
        counterparty(invoice)[1] or counterparty(invoice)[0]
        for invoice in invoices
    }
    base = sum((money(invoice.subtotal) for invoice in invoices), ZERO)
    withheld = sum(
        (money(invoice.withholding_total) for invoice in invoices),
        ZERO,
    )

    model = "115" if rent else "111"
    payroll = (
        payroll_withholdings(database, year, quarter)
        if not rent
        else {"recipients": 0, "gross": ZERO, "irpf": ZERO, "runs": 0}
    )

    boxes = (
        [
            {"box": "01", "label": "Número de perceptores (arrendadores)", "value": len(recipients), "is_count": True},
            {"box": "02", "label": "Base de las retenciones", "value": as_float(base)},
            {"box": "03", "label": "Retenciones e ingresos a cuenta", "value": as_float(withheld), "highlight": True},
        ]
        if rent
        else [
            {"box": "01", "label": "Rendimientos del trabajo: nº perceptores", "value": payroll["recipients"], "is_count": True},
            {"box": "02", "label": "Rendimientos del trabajo: importe de las percepciones", "value": as_float(payroll["gross"])},
            {"box": "03", "label": "Rendimientos del trabajo: retenciones", "value": as_float(payroll["irpf"])},
            {"box": "07", "label": "Actividades económicas: nº perceptores", "value": len(recipients), "is_count": True},
            {"box": "08", "label": "Actividades económicas: importe de las percepciones", "value": as_float(base)},
            {"box": "09", "label": "Actividades económicas: retenciones", "value": as_float(withheld)},
            {"box": "28", "label": "Total liquidación (03 + 09)", "value": as_float(withheld + payroll["irpf"]), "highlight": True},
        ]
    )
    withheld_total = withheld + payroll["irpf"]

    return {
        "model": model,
        "name": MODEL_NAMES[model],
        "year": year,
        "quarter": quarter,
        "period_label": f"{quarter}T {year}",
        "boxes": boxes,
        "result": as_float(withheld_total),
        "outcome": "A ingresar" if withheld_total > 0 else "Sin retenciones en el periodo",
        "applies": bool(invoices) or payroll["runs"] > 0,
        "invoices": [
            {
                "invoice_id": invoice.id,
                "document_id": invoice.document_id,
                "name": counterparty(invoice)[0],
                "tax_id": counterparty(invoice)[1],
                "base": as_float(invoice.subtotal),
                "withholding": as_float(invoice.withholding_total),
            }
            for invoice in invoices
        ],
        "warnings": pending_warning(database, date_from, date_to),
        "note": (
            "Incluye solo retenciones practicadas en facturas recibidas "
            + (
                "de alquiler de inmuebles."
                if rent
                else "de profesionales y las de nóminas aprobadas en "
                "CapaFiscal. Añade retenciones en especie o de otros "
                "rendimientos si los hubiera."
            )
        ),
    }


def build_model_111(database: Session, *, year: int, quarter: int) -> dict[str, Any]:
    return _withholding_model(database, year=year, quarter=quarter, rent=False)


def build_model_115(database: Session, *, year: int, quarter: int) -> dict[str, Any]:
    return _withholding_model(database, year=year, quarter=quarter, rent=True)


# -------------------------------------------------------------------
# Modelo 347
# -------------------------------------------------------------------

def build_model_347(database: Session, *, year: int) -> dict[str, Any]:
    invoices = approved(
        load_invoices_in_period(
            database,
            date_from=date(year, 1, 1),
            date_to=date(year, 12, 31),
        )
    )

    totals: dict[tuple[str, str], dict[str, Any]] = {}

    for invoice in invoices:
        # Las operaciones con retención se declaran en 190/180, no en el 347.
        if money(invoice.withholding_total) > 0:
            continue

        name, tax_id = counterparty(invoice)
        key_type = "B" if is_issued(invoice) else "A"
        key = (key_type, tax_id or (name or "").upper())

        entry = totals.setdefault(
            key,
            {
                "key": key_type,
                "key_label": "Ventas (clave B)" if key_type == "B" else "Compras (clave A)",
                "name": name,
                "tax_id": tax_id,
                "total": ZERO,
                "quarters": [ZERO, ZERO, ZERO, ZERO],
            },
        )
        amount = money(invoice.total)
        entry["total"] += amount
        entry["quarters"][(invoice.invoice_date.month - 1) // 3] += amount

    declarable = [
        {
            **entry,
            "total": as_float(entry["total"]),
            "quarters": [as_float(value) for value in entry["quarters"]],
        }
        for entry in totals.values()
        if entry["total"] > THRESHOLD_347
    ]
    declarable.sort(key=lambda item: -item["total"])

    return {
        "model": "347",
        "name": MODEL_NAMES["347"],
        "year": year,
        "threshold": float(THRESHOLD_347),
        "applies": bool(declarable),
        "counterparties": declarable,
        "note": (
            "Terceros con los que el total anual (IVA incluido) supera "
            "3.005,06 €, separados en compras (A) y ventas (B). Excluye "
            "operaciones con retención. Revisa arrendamientos y "
            "operaciones en metálico antes de presentar."
        ),
    }


# -------------------------------------------------------------------
# Calendario fiscal
# -------------------------------------------------------------------

QUARTER_DUE = {
    1: (4, 20),
    2: (7, 20),
    3: (10, 20),
}


def quarterly_due_date(year: int, quarter: int) -> date:
    if quarter == 4:
        return next_business_day(date(year + 1, 1, 30))

    month, day = QUARTER_DUE[quarter]
    return next_business_day(date(year, month, day))


def filings_index(database: Session) -> dict[tuple[str, int, int], TaxFiling]:
    return {
        (filing.model, filing.year, filing.period): filing
        for filing in database.scalars(select(TaxFiling)).all()
    }


def _has_withholdings(
    database: Session,
    year: int,
    rent: bool,
) -> bool:
    for quarter in range(1, 5):
        if _withholding_model(database, year=year, quarter=quarter, rent=rent)["applies"]:
            return True

    return False


def build_tax_calendar(
    database: Session,
    *,
    year: int,
    today: date | None = None,
) -> dict[str, Any]:
    """
    Obligaciones cuyo plazo vence en `year` (incluye el 4T y los
    resúmenes anuales del año anterior, que vencen en enero).
    """
    current_day = today or clock.today()
    form = legal_form(database)
    filings = filings_index(database)

    invoice_dates = [
        value
        for value in database.scalars(
            select(Invoice.invoice_date).where(
                Invoice.review_status.in_({"APPROVED", "PENDING"}),
                Invoice.invoice_date.is_not(None),
            )
        ).all()
    ]

    from app.models import PayrollRun

    payroll_periods = {
        (run_year, (run_month - 1) // 3 + 1)
        for run_year, run_month in database.execute(
            select(PayrollRun.year, PayrollRun.month).where(
                PayrollRun.status.in_({"APPROVED", "PAID"})
            )
        ).all()
    }

    def has_activity(period_year: int, period: int) -> bool:
        if period == 0:
            return any(value.year == period_year for value in invoice_dates) or any(
                run_year == period_year for run_year, _quarter in payroll_periods
            )

        start, end = quarter_range(period_year, period)
        return any(start <= value <= end for value in invoice_dates) or (
            (period_year, period) in payroll_periods
        )

    entries: list[dict[str, Any]] = []

    def add(
        model: str,
        period_year: int,
        period: int,
        due: date,
        *,
        estimate: float | None = None,
        note: str | None = None,
    ) -> None:
        filing = filings.get((model, period_year, period))

        if filing:
            status = "FILED"
        elif due < current_day and not has_activity(period_year, period):
            # Periodo sin datos en CapaFiscal: no se puede saber si se
            # presentó (p. ej. antes de empezar a usar la aplicación).
            status = "NO_DATA"
        elif due < current_day:
            status = "OVERDUE"
        elif days_until(due, current_day) <= DUE_SOON_DAYS:
            status = "DUE_SOON"
        else:
            status = "UPCOMING"

        entries.append(
            {
                "model": model,
                "name": MODEL_NAMES[model],
                "period_year": period_year,
                "period": period,
                "period_label": (
                    f"{period}P {period_year}"
                    if model == "202"
                    else f"{period}T {period_year}"
                    if period
                    else f"Anual {period_year}"
                ),
                "due_date": due.isoformat(),
                "days_left": days_until(due, current_day),
                "status": status,
                "estimate": estimate,
                "filed_at": filing.filed_at.isoformat() if filing else None,
                "filed_amount": (
                    float(filing.amount)
                    if filing and filing.amount is not None
                    else None
                ),
                "reference": filing.reference if filing else None,
                "note": note,
            }
        )

    quarterly_periods = [(year - 1, 4)] + [(year, quarter) for quarter in (1, 2, 3)]

    withholding_111 = {
        period_year: _has_withholdings(database, period_year, rent=False)
        for period_year in {year - 1, year}
    }
    withholding_115 = {
        period_year: _has_withholdings(database, period_year, rent=True)
        for period_year in {year - 1, year}
    }

    for period_year, quarter in quarterly_periods:
        due = quarterly_due_date(period_year, quarter)
        add(
            "303",
            period_year,
            quarter,
            due,
            estimate=build_model_303(database, year=period_year, quarter=quarter)["result"],
            note="Si domicilias el pago, el plazo acaba 5 días antes.",
        )

        if form == "AUTONOMO":
            add(
                "130",
                period_year,
                quarter,
                due,
                estimate=build_model_130(database, year=period_year, quarter=quarter)["result"],
            )

        if withholding_111[period_year]:
            add(
                "111",
                period_year,
                quarter,
                due,
                estimate=build_model_111(database, year=period_year, quarter=quarter)["result"],
            )

        if withholding_115[period_year]:
            add(
                "115",
                period_year,
                quarter,
                due,
                estimate=build_model_115(database, year=period_year, quarter=quarter)["result"],
            )

    previous_year = year - 1
    add("390", previous_year, 0, next_business_day(date(year, 1, 30)))

    if withholding_111[previous_year]:
        add("190", previous_year, 0, next_business_day(date(year, 1, 31)))

    if withholding_115[previous_year]:
        add("180", previous_year, 0, next_business_day(date(year, 1, 31)))

    model_347 = build_model_347(database, year=previous_year)

    if model_347["applies"]:
        add(
            "347",
            previous_year,
            0,
            next_business_day(last_day_of_month(year, 2)),
            note=f"{len(model_347['counterparties'])} tercero(s) superan el umbral.",
        )

    if form == "SOCIEDAD":
        for period, month in ((1, 4), (2, 10), (3, 12)):
            add(
                "202",
                year,
                period,
                next_business_day(date(year, month, 20)),
                note="Solo si procede según la última declaración del 200.",
            )

        add(
            "200",
            previous_year,
            0,
            next_business_day(date(year, 7, 25)),
            note="Para ejercicios que coinciden con el año natural.",
        )
    else:
        add(
            "100",
            previous_year,
            0,
            next_business_day(date(year, 6, 30)),
            note="Declaración de la renta del ejercicio anterior.",
        )

    entries.sort(key=lambda item: (item["due_date"], item["model"]))

    return {
        "year": year,
        "legal_form": form,
        "entries": entries,
        "note": (
            "Plazos generales de la AEAT ajustados a festivos nacionales. "
            "Comprueba festivos autonómicos y obligaciones específicas de tu "
            "actividad con tu asesor."
        ),
    }


def record_filing(
    database: Session,
    *,
    model: str,
    year: int,
    period: int,
    filed_at: date,
    amount: Decimal | None,
    reference: str | None,
    notes: str | None,
) -> TaxFiling:
    if model not in MODEL_NAMES:
        raise ValueError("Modelo no reconocido.")

    filing = database.scalar(
        select(TaxFiling).where(
            TaxFiling.model == model,
            TaxFiling.year == year,
            TaxFiling.period == period,
        )
    )

    if filing is None:
        filing = TaxFiling(model=model, year=year, period=period, filed_at=filed_at)
        database.add(filing)

    filing.filed_at = filed_at
    filing.amount = amount
    filing.reference = reference
    filing.notes = notes
    database.flush()

    return filing


def delete_filing(
    database: Session,
    *,
    model: str,
    year: int,
    period: int,
) -> bool:
    filing = database.scalar(
        select(TaxFiling).where(
            TaxFiling.model == model,
            TaxFiling.year == year,
            TaxFiling.period == period,
        )
    )

    if filing is None:
        return False

    database.delete(filing)
    database.flush()

    return True

