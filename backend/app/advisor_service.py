"""
Cierre trimestral para la gestoría: un único ZIP con todo lo que el asesor
externo pide cada trimestre (libros registro, borradores de modelos,
facturas, nóminas y movimientos bancarios) y un LEEME con las incidencias.
"""
from __future__ import annotations

from app import clock
import csv
import io
import re
import zipfile
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BankTransaction
from app.models import CompanyProfile
from app.models import Invoice
from app.models import PayrollRun
from app.reports_service import ISSUED
from app.reports_service import RECEIVED
from app.reports_service import build_ledger_rows
from app.reports_service import ledger_to_xlsx
from app.reports_service import quarter_range


def safe_name(value: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", value, flags=re.UNICODE).strip("_")[:80] or "documento"


def models_for(company: CompanyProfile | None, database: Session, year: int, quarter: int) -> list[dict[str, Any]]:
    from app.tax_service import build_model_111
    from app.tax_service import build_model_115
    from app.tax_service import build_model_130
    from app.tax_service import build_model_303

    builders = [("303", build_model_303), ("111", build_model_111), ("115", build_model_115)]
    if not company or company.legal_form == "AUTONOMO":
        builders.insert(1, ("130", build_model_130))

    results = []
    for code, builder in builders:
        try:
            results.append(builder(database, year=year, quarter=quarter))
        except Exception as error:  # un modelo con datos incompletos no bloquea el paquete
            results.append({"model": code, "name": f"Modelo {code}", "boxes": [], "result": None, "warnings": [str(error)]})
    return results


def models_xlsx(models: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    workbook = Workbook()
    workbook.remove(workbook.active)
    for model in models:
        sheet = workbook.create_sheet(f"Modelo {model['model']}")
        sheet.append([model.get("name") or f"Modelo {model['model']}", model.get("period_label") or ""])
        sheet["A1"].font = Font(bold=True, size=12)
        sheet.append([])
        sheet.append(["Casilla", "Concepto", "Importe"])
        for cell in sheet[3]:
            cell.font = Font(bold=True)
        for box in model.get("boxes") or []:
            sheet.append([box.get("box"), box.get("label"), box.get("value")])
        sheet.append([])
        sheet.append(["", "Resultado", model.get("result")])
        for warning in model.get("warnings") or []:
            text = warning.get("message") if isinstance(warning, dict) else str(warning)
            sheet.append(["", f"Aviso: {text}"])
        sheet.column_dimensions["B"].width = 60
        sheet.column_dimensions["C"].width = 16
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def pack_summary(database: Session, year: int, quarter: int) -> dict[str, Any]:
    date_from, date_to = quarter_range(year, quarter)
    invoices = database.scalars(
        select(Invoice).where(Invoice.invoice_date >= date_from, Invoice.invoice_date <= date_to)
    ).all()
    pending = [item for item in invoices if item.review_status == "PENDING"]
    runs = database.scalars(
        select(PayrollRun).where(
            PayrollRun.year == year,
            PayrollRun.month.between((quarter - 1) * 3 + 1, quarter * 3),
        )
    ).all()
    transactions = database.scalars(
        select(BankTransaction).where(BankTransaction.booking_date >= date_from, BankTransaction.booking_date <= date_to)
    ).all()
    unmatched = [item for item in transactions if item.match_status not in {"MATCHED", "CONFIRMED", "IGNORED"}]

    issues = []
    if pending:
        issues.append(f"{len(pending)} factura(s) del trimestre siguen pendientes de revisar y no entran en los libros.")
    draft_runs = [item for item in runs if item.status == "DRAFT"]
    if draft_runs:
        issues.append(f"{len(draft_runs)} nómina(s) del trimestre están en borrador (no cuentan en el 111).")
    if unmatched:
        issues.append(f"{len(unmatched)} movimiento(s) bancario(s) sin conciliar.")

    return {
        "year": year,
        "quarter": quarter,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "issued": sum(1 for item in invoices if item.direction == ISSUED and item.review_status == "APPROVED"),
        "received": sum(1 for item in invoices if item.direction != ISSUED and item.review_status == "APPROVED"),
        "pending": len(pending),
        "payroll_runs": len(runs),
        "transactions": len(transactions),
        "unmatched": len(unmatched),
        "issues": issues,
    }


def build_advisor_pack(database: Session, *, year: int, quarter: int) -> bytes:
    company = database.scalar(select(CompanyProfile).limit(1))
    date_from, date_to = quarter_range(year, quarter)
    label = f"{quarter}T {year}"
    summary = pack_summary(database, year, quarter)
    models = models_for(company, database, year, quarter)
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # Libros registro
        for direction, name in ((ISSUED, "emitidas"), (RECEIVED, "recibidas")):
            rows = build_ledger_rows(database, date_from=date_from, date_to=date_to, direction=direction)
            archive.writestr(
                f"01_libros/libro_facturas_{name}_{year}_{quarter}T.xlsx",
                ledger_to_xlsx(rows, title=f"Libro de facturas {name} {label}", direction=direction),
            )

        # Borradores de modelos
        archive.writestr(f"02_modelos/borradores_modelos_{year}_{quarter}T.xlsx", models_xlsx(models))

        # Facturas originales
        invoices = database.scalars(
            select(Invoice).where(
                Invoice.invoice_date >= date_from,
                Invoice.invoice_date <= date_to,
                Invoice.review_status == "APPROVED",
            )
        ).all()
        used: set[str] = set()
        for invoice in invoices:
            document = invoice.document
            if document is None:
                continue
            path = settings.upload_dir / document.stored_filename
            if not path.exists():
                continue
            folder = "emitidas" if invoice.direction == ISSUED else "recibidas"
            party = invoice.customer_name if invoice.direction == ISSUED else invoice.supplier_name
            stem = safe_name(f"{invoice.invoice_date:%Y%m%d}_{party or 'sin_nombre'}_{invoice.invoice_number or invoice.id}")
            filename = f"03_facturas/{folder}/{stem}{document.extension or '.pdf'}"
            while filename in used:
                filename = filename.replace(".", "_1.", 1)
            used.add(filename)
            archive.write(path, filename)

        # Nóminas
        runs = database.scalars(
            select(PayrollRun).where(
                PayrollRun.year == year,
                PayrollRun.month.between((quarter - 1) * 3 + 1, quarter * 3),
                PayrollRun.status != "DRAFT",
            )
        ).all()
        if runs:
            from app.payroll_service import build_payslips_pdf
            from app.payroll_service import build_summary_xlsx

            for run in runs:
                archive.writestr(f"04_nominas/recibos_{run.year}_{run.month:02d}.pdf", build_payslips_pdf(run, company))
                archive.writestr(f"04_nominas/resumen_{run.year}_{run.month:02d}.xlsx", build_summary_xlsx(run))

        # Banco
        transactions = database.scalars(
            select(BankTransaction)
            .where(BankTransaction.booking_date >= date_from, BankTransaction.booking_date <= date_to)
            .order_by(BankTransaction.booking_date)
        ).all()
        if transactions:
            output = io.StringIO()
            writer = csv.writer(output, delimiter=";")
            writer.writerow(["Fecha", "Concepto", "Importe", "Saldo", "Cuenta", "Conciliación", "Factura"])
            for item in transactions:
                writer.writerow([
                    item.booking_date.strftime("%d/%m/%Y"), item.description,
                    f"{item.amount:.2f}".replace(".", ","),
                    f"{item.balance:.2f}".replace(".", ",") if item.balance is not None else "",
                    item.account_label or "", item.match_status, item.matched_invoice_id or "",
                ])
            archive.writestr(f"05_banco/movimientos_{year}_{quarter}T.csv", "﻿" + output.getvalue())

        # Libro diario (asientos con el PGC) para importar en el programa contable de la gestoría
        from app import accounting

        diary = accounting.journal(database, date_from, date_to)
        archive.writestr(f"06_contabilidad/borrador_libro_diario_{year}_{quarter}T.xlsx", accounting.to_xlsx(diary))
        archive.writestr(f"06_contabilidad/borrador_libro_diario_{year}_{quarter}T.csv", accounting.to_csv(diary))

        # LEEME
        lines = [
            f"CIERRE {label} · {(company.name if company else '') or ''} {('· NIF ' + company.tax_id) if company and company.tax_id else ''}",
            f"Periodo: {date_from:%d/%m/%Y} – {date_to:%d/%m/%Y}",
            f"Generado por CapaFiscal el {clock.today():%d/%m/%Y}.",
            "",
            "CONTENIDO",
            f"  01_libros    Libros registro de facturas emitidas ({summary['issued']}) y recibidas ({summary['received']})",
            "  02_modelos   Borradores de los modelos trimestrales con sus casillas",
            "  03_facturas  PDF de todas las facturas aprobadas del trimestre",
            f"  04_nominas   Recibos y resumen de {len(runs)} nómina(s) aprobada(s)",
            f"  05_banco     {summary['transactions']} movimiento(s) bancario(s) con su conciliación",
            f"  06_contabilidad  BORRADOR de libro diario (a revisar por la gestoría): {diary['count']} asiento(s) con el Plan General Contable (CSV y Excel)"
            + (f"; {len(diary['pending'])} elemento(s) aún sin contabilizar" if diary["pending"] else ""),
            "",
            "RESULTADO ESTIMADO DE LOS MODELOS",
        ]
        for model in models:
            result = model.get("result")
            lines.append(f"  Modelo {model['model']}: {('%.2f €' % result).replace('.', ',') if result is not None else '—'}")
        lines += ["", "INCIDENCIAS"]
        lines += [f"  - {issue}" for issue in summary["issues"]] or ["  Ninguna."]
        lines += ["", "Los borradores son orientativos y deben validarse antes de su presentación."]
        archive.writestr("LEEME.txt", "\r\n".join(lines))

    return buffer.getvalue()


def prepare_advisor_email(database: Session, *, year: int, quarter: int, created_by: str = "user") -> Any:
    from app.outbox_service import create_message

    company = database.scalar(select(CompanyProfile).limit(1))
    summary = pack_summary(database, year, quarter)
    name = (company.name if company else None) or ""
    issues = "\n".join(f"- {item}" for item in summary["issues"]) or "- Ninguna."
    body = (
        f"Hola,\n\n"
        f"Os enviamos la documentación del {quarter}T {year} de {name}: libros registro, borradores de "
        f"los modelos, facturas ({summary['issued']} emitidas y {summary['received']} recibidas), nóminas "
        f"y movimientos bancarios.\n\n"
        f"Incidencias detectadas:\n{issues}\n\n"
        f"Cualquier cosa que necesitéis, decidnos.\n\n"
        f"Un saludo,\n{name}"
    )
    return create_message(
        database,
        kind="ADVISOR",
        subject=f"Documentación {quarter}T {year} · {name}".strip(" ·"),
        body=body,
        to_email=company.advisor_email if company else None,
        to_name="Gestoría",
        attachments=[{"type": "advisor_pack", "year": year, "quarter": quarter, "filename": f"cierre_{year}_{quarter}T.zip"}],
        entity_type="advisor_pack",
        entity_id=year * 10 + quarter,
        created_by=created_by,
    )
