"""
Banco: importación de extractos (CSV/Excel), conciliación con facturas,
previsión de tesorería y salud del negocio.

La conexión bancaria directa (PSD2) requiere un proveedor autorizado;
mientras tanto el extracto descargado de la banca online cubre el mismo
flujo de datos.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from datetime import date
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.calendar_es import add_months
from app.extractor import normalize_amount
from app.extractor import normalize_search_text
from app.extractor import parse_date_value
from app.models import BankImport
from app.models import BankTransaction
from app.models import Invoice
from app.reports_service import ZERO
from app.reports_service import counterparty
from app.reports_service import format_eur
from app.reports_service import is_issued
from app.reports_service import money


MAX_HEADER_SCAN = 25
DEFAULT_PAYMENT_TERM_DAYS = 30

HEADER_KEYWORDS = {
    "date": ("fecha operacion", "fecha de operacion", "f. operacion",
             "fecha contable", "fecha", "date", "f.valor", "fecha valor"),
    "description": ("concepto", "descripcion", "detalle", "movimiento",
                    "observaciones", "description", "referencia"),
    "amount": ("importe", "cantidad", "monto", "amount", "euros"),
    "debit": ("cargo", "debe", "salida", "reintegro", "pagos", "debit",
              "withdrawal"),
    "credit": ("abono", "haber", "entrada", "ingresos", "cobros", "credit",
               "deposit"),
    "balance": ("saldo", "balance", "disponible"),
}

STOP_WORDS = {
    "s.a.", "s.l.", "s.a.u.", "sociedad", "limitada", "anonima",
    "espana", "clientes", "comercial", "servicios", "energia", "grupo",
}


class BankImportError(ValueError):
    pass


# -------------------------------------------------------------------
# Lectura de ficheros
# -------------------------------------------------------------------

def read_rows(content: bytes, filename: str) -> list[list[str]]:
    lower_name = filename.lower()

    if lower_name.endswith((".xlsx", ".xlsm")):
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as error:  # noqa: BLE001 - formato ilegible
            raise BankImportError(f"No se pudo leer el Excel: {error}") from error

        sheet = workbook.active
        rows: list[list[str]] = []

        for row in sheet.iter_rows(values_only=True):
            cells = []

            for value in row:
                if value is None:
                    cells.append("")
                elif isinstance(value, (date,)):
                    cells.append(value.strftime("%d/%m/%Y"))
                else:
                    cells.append(str(value))

            rows.append(cells)

        return rows

    text: str | None = None

    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        raise BankImportError("No se pudo leer el fichero como texto.")

    sample = "\n".join(text.splitlines()[:30])

    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","

    return [
        [cell.strip() for cell in row]
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
    ]


def detect_columns(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    """
    Busca la fila de cabecera y la posición de cada columna. Devuelve
    (índice de cabecera, {rol: índice de columna}).
    """
    for row_index, row in enumerate(rows[:MAX_HEADER_SCAN]):
        normalized = [normalize_search_text(cell).strip() for cell in row]
        mapping: dict[str, int] = {}

        for role, keywords in HEADER_KEYWORDS.items():
            for keyword in keywords:
                for column_index, cell in enumerate(normalized):
                    if column_index in mapping.values():
                        continue

                    if cell == keyword or cell.startswith(keyword):
                        mapping.setdefault(role, column_index)
                        break

                if role in mapping:
                    break

        has_amount = "amount" in mapping or (
            "debit" in mapping or "credit" in mapping
        )

        if "date" in mapping and has_amount:
            return row_index, mapping

    raise BankImportError(
        "No se encontró la cabecera del extracto. Debe tener al menos "
        "columnas de fecha e importe (o cargo/abono)."
    )


def parse_bank_rows(
    content: bytes,
    filename: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_rows(content, filename)
    header_index, mapping = detect_columns(rows)
    header = rows[header_index]

    parsed: list[dict[str, Any]] = []
    skipped = 0

    for row in rows[header_index + 1:]:
        if not any(cell.strip() for cell in row):
            continue

        def cell(role: str) -> str:
            index = mapping.get(role)

            if index is None or index >= len(row):
                return ""

            return row[index]

        booking_date = parse_date_value(cell("date").split(" ")[0])

        if booking_date is None:
            skipped += 1
            continue

        amount: Decimal | None = None

        if "amount" in mapping:
            amount = normalize_amount(cell("amount"))
        else:
            debit = normalize_amount(cell("debit")) or ZERO
            credit = normalize_amount(cell("credit")) or ZERO
            amount = credit - abs(debit)

        if amount is None:
            skipped += 1
            continue

        description = cell("description") or " ".join(
            value
            for index, value in enumerate(row)
            if index not in mapping.values() and value
        )

        parsed.append(
            {
                "booking_date": booking_date,
                "description": re.sub(r"\s+", " ", description).strip()[:500] or "(sin concepto)",
                "amount": amount,
                "balance": normalize_amount(cell("balance")) if "balance" in mapping else None,
            }
        )

    if not parsed:
        raise BankImportError("El fichero no contiene movimientos legibles.")

    column_mapping = {
        role: header[index] if index < len(header) else str(index)
        for role, index in mapping.items()
    }

    return parsed, {"columns": column_mapping, "skipped_rows": skipped}


def fingerprint(row: dict[str, Any], occurrence: int) -> str:
    # Sin la etiqueta de cuenta: reimportar el mismo extracto con otra
    # etiqueta no debe duplicar movimientos.
    raw = "|".join(
        [
            row["booking_date"].isoformat(),
            format(row["amount"], "f"),
            normalize_search_text(row["description"]),
            format(row["balance"], "f") if row["balance"] is not None else "",
            str(occurrence),
        ]
    )

    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def import_bank_file(
    database: Session,
    *,
    content: bytes,
    filename: str,
    account_label: str | None,
    actor: str,
) -> dict[str, Any]:
    rows, meta = parse_bank_rows(content, filename)
    return store_rows(database, rows, source=filename, account_label=account_label, actor=actor, columns=meta["columns"], skipped=meta["skipped_rows"])


CONNECTED_SOURCE = "Banco conectado"


def store_rows(
    database: Session,
    rows: list[dict[str, Any]],
    *,
    source: str,
    account_label: str | None,
    actor: str,
    columns: dict[str, Any] | None = None,
    skipped: int = 0,
    external: bool = False,
) -> dict[str, Any]:
    """Guarda movimientos (de un extracto o del banco conectado) sin duplicar, y concilia.

    external=True: filas del banco conectado, con su identificador propio («external_id»). Un
    movimiento que ya entró por un extracto CSV (misma fecha e importe en esa cuenta) no se repite.
    """
    from app.invoice_service import add_audit_event

    bank_import = BankImport(
        filename=source[:255],
        account_label=account_label,
        rows_total=len(rows),
        column_mapping=columns or {},
    )
    database.add(bank_import)
    database.flush()

    occurrences: dict[tuple, int] = defaultdict(int)
    imported = 0
    duplicated = 0

    for row in rows:
        if external:
            print_hash = hashlib.sha256(f"banco:{row['external_id']}".encode()).hexdigest()
            same_day = (row["booking_date"], row["amount"])
            occurrences[same_day] += 1
            already = database.scalar(select(BankTransaction.id).where(BankTransaction.fingerprint == print_hash))
            from_statement = database.scalar(
                select(func.count()).select_from(BankTransaction).join(BankImport, BankImport.id == BankTransaction.import_id).where(
                    BankTransaction.booking_date == row["booking_date"], BankTransaction.amount == row["amount"],
                    ~BankImport.filename.like(f"{CONNECTED_SOURCE}%"),
                )
            ) or 0
            if already or occurrences[same_day] <= from_statement:
                duplicated += 1
                continue
        else:
            key = (row["booking_date"], row["amount"], row["description"], row["balance"])
            occurrences[key] += 1
            print_hash = fingerprint(row, occurrences[key])
            if database.scalar(select(BankTransaction.id).where(BankTransaction.fingerprint == print_hash)):
                duplicated += 1
                continue

        database.add(
            BankTransaction(
                import_id=bank_import.id,
                account_label=account_label,
                booking_date=row["booking_date"],
                description=row["description"],
                amount=row["amount"],
                balance=row["balance"],
                fingerprint=print_hash,
            )
        )
        imported += 1

    bank_import.rows_imported = imported
    bank_import.rows_duplicated = duplicated
    database.flush()

    suggestions = suggest_matches(database)
    from app.reconciliation import reconcile

    reconciliation = reconcile(database, actor="conciliacion-automatica")

    add_audit_event(
        database,
        action="bank.imported",
        entity_type="bank",
        entity_id=bank_import.id,
        actor=actor,
        event_data={
            "filename": source,
            "rows": len(rows),
            "imported": imported,
            "duplicated": duplicated,
            "suggestions": suggestions,
            "columns": columns or {},
        },
    )

    return {
        "import_id": bank_import.id,
        "rows": len(rows),
        "imported": imported,
        "duplicated": duplicated,
        "skipped": skipped,
        "suggestions": suggestions,
        "auto_matched": reconciliation["auto_matched"],
        "reconciliation": reconciliation["counts"],
        "columns": columns or {},
        "message": (
            f"{imported} movimiento(s) importado(s), {duplicated} ya "
            f"existían. {reconciliation['auto_matched']} conciliado(s) automáticamente "
            f"y {reconciliation['counts'].get('POSIBLE', 0)} propuesta(s) para confirmar."
        ),
    }


# -------------------------------------------------------------------
# Conciliación
# -------------------------------------------------------------------

def significant_words(value: str | None) -> list[str]:
    words = re.findall(r"[a-z0-9]+", normalize_search_text(value or ""))

    return [
        word
        for word in words
        if len(word) >= 4 and word not in STOP_WORDS
    ]


def match_score(transaction: BankTransaction, invoice: Invoice) -> int:
    score = 60
    description = normalize_search_text(transaction.description)
    name, tax_id = counterparty(invoice)

    words = significant_words(name)

    if words and words[0] in description:
        score += 25
    elif any(word in description for word in words[1:3]):
        score += 12

    if tax_id and tax_id.lower() in description.replace(" ", ""):
        score += 10

    if invoice.invoice_number and normalize_search_text(
        invoice.invoice_number
    ).replace(" ", "") in description.replace(" ", ""):
        score += 15

    reference_date = invoice.due_date or invoice.invoice_date

    if reference_date and abs((transaction.booking_date - reference_date).days) <= 5:
        score += 5

    return min(score, 100)


def candidate_invoices(
    database: Session,
    transaction: BankTransaction,
) -> list[tuple[int, Invoice]]:
    amount = abs(money(transaction.amount))
    expected_issued = transaction.amount > 0

    statement = select(Invoice).where(
        Invoice.review_status == "APPROVED",
        Invoice.paid_at.is_(None),
        Invoice.total == amount,
    )

    candidates: list[tuple[int, Invoice]] = []

    for invoice in database.scalars(statement).all():
        if is_issued(invoice) != expected_issued:
            continue

        if invoice.invoice_date and not (
            invoice.invoice_date - timedelta(days=5)
            <= transaction.booking_date
            <= invoice.invoice_date + timedelta(days=180)
        ):
            continue

        already_matched = database.scalar(
            select(BankTransaction.id).where(
                BankTransaction.matched_invoice_id == invoice.id,
                BankTransaction.id != transaction.id,
                BankTransaction.match_status.in_({"SUGGESTED", "MATCHED"}),
            )
        )

        if already_matched:
            continue

        candidates.append((match_score(transaction, invoice), invoice))

    candidates.sort(key=lambda item: -item[0])

    return candidates


def suggest_matches(database: Session) -> int:
    transactions = database.scalars(
        select(BankTransaction).where(BankTransaction.match_status == "UNMATCHED")
    ).all()

    suggested = 0

    for transaction in transactions:
        candidates = candidate_invoices(database, transaction)

        if not candidates:
            continue

        best_score, best_invoice = candidates[0]

        # Si hay empate claro entre facturas, mejor que decida una persona.
        if len(candidates) > 1 and candidates[1][0] >= best_score - 10:
            continue

        transaction.matched_invoice_id = best_invoice.id
        transaction.match_status = "SUGGESTED"
        transaction.match_score = best_score
        suggested += 1
        database.flush()

    return suggested


def payment_method_from_description(description: str) -> str:
    normalized = normalize_search_text(description)

    if any(word in normalized for word in ("recibo", "adeudo", "domicili", "sepa dd")):
        return "DOMICILIACION"

    if any(word in normalized for word in ("tarjeta", "tpv", "compra con")):
        return "TARJETA"

    return "TRANSFERENCIA"


def confirm_match(
    database: Session,
    *,
    transaction: BankTransaction,
    invoice_id: int | None,
    actor: str,
) -> BankTransaction:
    from app.invoice_service import add_audit_event

    target_id = invoice_id or transaction.matched_invoice_id

    if target_id is None:
        raise ValueError("Indica la factura con la que conciliar el movimiento.")

    invoice = database.get(Invoice, target_id)

    if invoice is None:
        raise ValueError("Factura no encontrada.")

    if invoice.review_status != "APPROVED":
        raise ValueError("Solo se pueden conciliar facturas aprobadas.")

    if is_issued(invoice) != (transaction.amount > 0):
        raise ValueError(
            "El sentido no coincide: los cobros se concilian con facturas "
            "emitidas y los pagos con facturas recibidas."
        )

    transaction.matched_invoice_id = invoice.id
    transaction.match_status = "MATCHED"
    invoice.paid_at = transaction.booking_date
    invoice.payment_method = invoice.payment_method or payment_method_from_description(
        transaction.description
    )

    add_audit_event(
        database,
        action="bank.reconciled",
        entity_type="invoice",
        entity_id=invoice.id,
        actor=actor,
        event_data={
            "document_id": invoice.document_id,
            "transaction_id": transaction.id,
            "amount": transaction.amount,
            "booking_date": transaction.booking_date,
            "description": transaction.description,
        },
    )
    database.flush()

    return transaction


def confirm_all_suggestions(
    database: Session,
    *,
    min_score: int,
    actor: str,
) -> int:
    from app.reconciliation import reconcile

    # «Confirmar coincidencias seguras» confirma solo lo que la conciliación clasifica como SEGURO,
    # nunca una propuesta con otra factura igual de plausible, por alta que sea su puntuación.
    safe = {row["transaction_id"] for row in reconcile(database, auto=False)["movements"] if row.get("level") == "SEGURO" and row["state"] == "POSIBLE"}
    transactions = database.scalars(
        select(BankTransaction).where(
            BankTransaction.match_status == "SUGGESTED",
            BankTransaction.match_score >= min_score,
        )
    ).all()

    confirmed = 0

    for transaction in transactions:
        if transaction.id not in safe:
            continue
        try:
            confirm_match(database, transaction=transaction, invoice_id=None, actor=actor)
            confirmed += 1
        except ValueError:
            continue

    return confirmed


def unmatch(
    database: Session,
    *,
    transaction: BankTransaction,
    ignore: bool,
    actor: str,
) -> BankTransaction:
    from app.invoice_service import add_audit_event

    from app.allocations import clear

    invoice = transaction.matched_invoice
    clear(database, transaction)  # pagos repartidos o justificaciones (varias facturas, traspaso, comisión…)

    if transaction.match_status == "MATCHED" and invoice is not None:
        invoice.paid_at = None
        invoice.payment_method = None
        add_audit_event(
            database,
            action="bank.unreconciled",
            entity_type="invoice",
            entity_id=invoice.id,
            actor=actor,
            event_data={
                "document_id": invoice.document_id,
                "transaction_id": transaction.id,
            },
        )

    transaction.matched_invoice_id = None
    transaction.match_score = None
    transaction.match_status = "IGNORED" if ignore else "UNMATCHED"
    database.flush()

    return transaction


def serialize_transaction(
    database: Session,
    transaction: BankTransaction,
    include_candidates: bool = False,
) -> dict[str, Any]:
    invoice = transaction.matched_invoice
    data: dict[str, Any] = {
        "id": transaction.id,
        "booking_date": transaction.booking_date.isoformat(),
        "description": transaction.description,
        "amount": float(transaction.amount),
        "balance": float(transaction.balance) if transaction.balance is not None else None,
        "account_label": transaction.account_label,
        "match_status": transaction.match_status,
        "match_score": transaction.match_score,
        "invoice": None,
    }

    if invoice is not None:
        name, _tax_id = counterparty(invoice)
        data["invoice"] = {
            "id": invoice.id,
            "document_id": invoice.document_id,
            "name": name,
            "number": invoice.invoice_number,
            "total": float(money(invoice.total)),
            "invoice_date": invoice.invoice_date.isoformat() if invoice.invoice_date else None,
        }

    if include_candidates and transaction.match_status in {"UNMATCHED", "SUGGESTED"}:
        data["candidates"] = [
            {
                "invoice_id": candidate.id,
                "document_id": candidate.document_id,
                "name": counterparty(candidate)[0],
                "number": candidate.invoice_number,
                "total": float(money(candidate.total)),
                "score": score,
            }
            for score, candidate in candidate_invoices(database, transaction)[:5]
        ]

    return data


def list_transactions(
    database: Session,
    *,
    status: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    statement = (
        select(BankTransaction)
        .options(selectinload(BankTransaction.matched_invoice))
        .order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc())
        .limit(limit)
    )

    if status:
        statement = statement.where(BankTransaction.match_status == status.upper())

    return [
        serialize_transaction(database, transaction, include_candidates=True)
        for transaction in database.scalars(statement).all()
    ]


# -------------------------------------------------------------------
# Tesorería
# -------------------------------------------------------------------

def current_balance(database: Session) -> tuple[float | None, str | None]:
    transaction = database.scalar(
        select(BankTransaction)
        .where(BankTransaction.balance.is_not(None))
        .order_by(BankTransaction.booking_date.desc(), BankTransaction.id.desc())
        .limit(1)
    )

    if transaction is None:
        return None, None

    return float(transaction.balance), transaction.booking_date.isoformat()


def expected_date(invoice: Invoice) -> date:
    if invoice.due_date:
        return invoice.due_date

    if invoice.invoice_date:
        return invoice.invoice_date + timedelta(days=DEFAULT_PAYMENT_TERM_DAYS)

    return date.today()


def payroll_movements(database: Session, current_day: date) -> list[dict[str, Any]]:
    """Pago de nóminas aprobadas y de seguros sociales (TC1)."""
    from app.calendar_es import last_day_of_month
    from app.calendar_es import next_business_day as next_working_day
    from app.models import PayrollRun
    from app.payroll_service import period_label
    from app.payroll_service import run_totals

    movements: list[dict[str, Any]] = []
    runs = database.scalars(
        select(PayrollRun)
        .where(PayrollRun.status.in_({"APPROVED", "PAID"}))
        .options(selectinload(PayrollRun.payslips))
    ).all()

    for run in runs:
        totals = run_totals(run)

        if run.status == "APPROVED":
            pay_day = last_day_of_month(run.year, run.month)
            movements.append(
                {
                    "date": max(pay_day, current_day).isoformat(),
                    "original_date": pay_day.isoformat(),
                    "overdue": pay_day < current_day,
                    "type": "payroll",
                    "label": f"Nóminas · {period_label(run)} (líquido)",
                    "amount": -totals["net"],
                    "document_id": None,
                    "estimated_date": False,
                }
            )

        # Seguros sociales: último día del mes siguiente.
        following_month = run.month % 12 + 1
        following_year = run.year + (1 if run.month == 12 else 0)
        ss_due = last_day_of_month(following_year, following_month)

        while not next_working_day(ss_due) == ss_due:
            ss_due -= timedelta(days=1)

        if ss_due >= current_day:
            movements.append(
                {
                    "date": ss_due.isoformat(),
                    "original_date": ss_due.isoformat(),
                    "overdue": False,
                    "type": "social_security",
                    "label": f"Seguros sociales · {period_label(run)}",
                    "amount": -(totals["ss_employee"] + totals["ss_employer"]),
                    "document_id": None,
                    "estimated_date": False,
                }
            )

    return movements


def payroll_cost_by_month(database: Session) -> dict[str, Decimal]:
    from app.models import PayrollRun

    costs: dict[str, Decimal] = defaultdict(lambda: ZERO)

    for run in database.scalars(
        select(PayrollRun)
        .where(PayrollRun.status.in_({"APPROVED", "PAID"}))
        .options(selectinload(PayrollRun.payslips))
    ).all():
        key = f"{run.year}-{run.month:02d}"
        costs[key] += sum((money(payslip.company_cost) for payslip in run.payslips), ZERO)

    return costs


def build_cashflow_forecast(
    database: Session,
    *,
    horizon_days: int = 90,
    today: date | None = None,
) -> dict[str, Any]:
    from app.tax_service import build_tax_calendar

    current_day = today or date.today()
    horizon = current_day + timedelta(days=horizon_days)

    invoices = database.scalars(
        select(Invoice).where(
            Invoice.review_status == "APPROVED",
            Invoice.paid_at.is_(None),
        )
    ).all()

    movements: list[dict[str, Any]] = []

    for invoice in invoices:
        when = expected_date(invoice)
        overdue = when < current_day
        amount = money(invoice.total)
        name, _tax_id = counterparty(invoice)

        movements.append(
            {
                "date": max(when, current_day).isoformat(),
                "original_date": when.isoformat(),
                "overdue": overdue,
                "type": "collection" if is_issued(invoice) else "payment",
                "label": (
                    f"{'Cobro' if is_issued(invoice) else 'Pago'} · "
                    f"{name or 'Sin nombre'} · {invoice.invoice_number or 's/n'}"
                ),
                "amount": float(amount if is_issued(invoice) else -amount),
                "document_id": invoice.document_id,
                "estimated_date": invoice.due_date is None,
            }
        )

    for year in {current_day.year, horizon.year}:
        for entry in build_tax_calendar(database, year=year, today=current_day)["entries"]:
            due = date.fromisoformat(entry["due_date"])

            if entry["status"] == "FILED" or not (current_day <= due <= horizon):
                continue

            if not entry.get("estimate") or entry["estimate"] <= 0:
                continue

            movements.append(
                {
                    "date": due.isoformat(),
                    "original_date": due.isoformat(),
                    "overdue": False,
                    "type": "tax",
                    "label": f"Modelo {entry['model']} · {entry['period_label']} (estimado)",
                    "amount": -entry["estimate"],
                    "document_id": None,
                    "estimated_date": False,
                }
            )

    movements.extend(payroll_movements(database, current_day))

    in_horizon = [
        movement
        for movement in movements
        if date.fromisoformat(movement["date"]) <= horizon
    ]
    in_horizon.sort(key=lambda item: (item["date"], item["amount"]))

    balance, balance_date = current_balance(database)
    running = Decimal(str(balance)) if balance is not None else None
    lowest: dict[str, Any] | None = None

    weeks: dict[str, dict[str, Decimal]] = {}

    for movement in in_horizon:
        amount = Decimal(str(movement["amount"]))
        week_start = date.fromisoformat(movement["date"])
        week_start -= timedelta(days=week_start.weekday())
        bucket = weeks.setdefault(
            week_start.isoformat(),
            {"inflow": ZERO, "outflow": ZERO},
        )

        if amount >= 0:
            bucket["inflow"] += amount
        else:
            bucket["outflow"] += -amount

        if running is not None:
            running += amount
            movement["balance_after"] = float(running)

            if lowest is None or running < Decimal(str(lowest["balance"])):
                lowest = {"date": movement["date"], "balance": float(running)}

    inflows = sum((Decimal(str(m["amount"])) for m in in_horizon if m["amount"] > 0), ZERO)
    outflows = sum((-Decimal(str(m["amount"])) for m in in_horizon if m["amount"] < 0), ZERO)

    warnings: list[str] = []

    if balance is None:
        warnings.append(
            "Importa un extracto bancario con columna de saldo para "
            "proyectar la caja disponible."
        )
    elif lowest is not None and lowest["balance"] < 0:
        warnings.append(
            f"Con los cobros y pagos previstos la caja quedaría en negativo "
            f"el {date.fromisoformat(lowest['date']).strftime('%d/%m/%Y')}. "
            "Adelanta cobros o negocia plazos de pago."
        )

    overdue_collections = [m for m in in_horizon if m["overdue"] and m["type"] == "collection"]

    if overdue_collections:
        total = sum(m["amount"] for m in overdue_collections)
        warnings.append(
            f"Tienes {len(overdue_collections)} cobro(s) vencido(s) por "
            f"{format_eur(total)}: reclámalos."
        )

    return {
        "today": current_day.isoformat(),
        "horizon_days": horizon_days,
        "current_balance": balance,
        "balance_date": balance_date,
        "expected_inflows": float(inflows),
        "expected_outflows": float(outflows),
        "projected_balance": float(running) if running is not None else None,
        "lowest_point": lowest,
        "weeks": [
            {
                "week_start": key,
                "inflow": float(value["inflow"]),
                "outflow": float(value["outflow"]),
            }
            for key, value in sorted(weeks.items())
        ],
        "movements": in_horizon,
        "warnings": warnings,
        "note": (
            "Previsión con facturas aprobadas pendientes (vencimiento o, si "
            "no lo hay, fecha de factura + 30 días), nóminas aprobadas, "
            "seguros sociales e impuestos estimados. No incluye gastos sin factura."
        ),
    }


# -------------------------------------------------------------------
# Salud del negocio
# -------------------------------------------------------------------

def average_days(pairs: list[tuple[date, date]]) -> float | None:
    if not pairs:
        return None

    return round(sum((paid - issued).days for issued, paid in pairs) / len(pairs), 1)


def build_business_health(
    database: Session,
    *,
    year: int,
    quarter: int | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    from app.reports_service import build_payments_overview
    from app.reports_service import quarter_range

    current_day = today or date.today()
    date_from, date_to = quarter_range(year, quarter)

    invoices = database.scalars(
        select(Invoice).where(Invoice.review_status == "APPROVED")
    ).all()

    in_period = [
        invoice
        for invoice in invoices
        if invoice.invoice_date and date_from <= invoice.invoice_date <= date_to
    ]

    income = sum((money(i.subtotal) for i in in_period if is_issued(i)), ZERO)
    expenses = sum((money(i.subtotal) for i in in_period if not is_issued(i)), ZERO)
    payroll_months = payroll_cost_by_month(database)
    payroll_cost = sum(
        (
            amount
            for key, amount in payroll_months.items()
            if date_from.strftime("%Y-%m") <= key <= date_to.strftime("%Y-%m")
        ),
        ZERO,
    )
    vat_out = sum((money(i.tax_total) for i in in_period if is_issued(i)), ZERO)
    vat_in = sum((money(i.tax_total) for i in in_period if not is_issued(i)), ZERO)
    result = income - expenses - payroll_cost

    # Serie de los últimos 12 meses hasta el final del periodo.
    months: list[dict[str, Any]] = []
    first_month = add_months(date(date_to.year, date_to.month, 1), -11)

    for offset in range(12):
        month_start = add_months(first_month, offset)
        key = month_start.strftime("%Y-%m")
        month_income = sum(
            (
                money(i.subtotal)
                for i in invoices
                if is_issued(i) and i.invoice_date and i.invoice_date.strftime("%Y-%m") == key
            ),
            ZERO,
        )
        month_expenses = sum(
            (
                money(i.subtotal)
                for i in invoices
                if not is_issued(i) and i.invoice_date and i.invoice_date.strftime("%Y-%m") == key
            ),
            ZERO,
        )
        month_payroll = payroll_months.get(key, ZERO)
        months.append(
            {
                "month": key,
                "income": float(month_income),
                "expenses": float(month_expenses + month_payroll),
                "payroll": float(month_payroll),
                "result": float(month_income - month_expenses - month_payroll),
            }
        )

    one_year_ago = current_day - timedelta(days=365)
    collection_days = average_days(
        [
            (i.invoice_date, i.paid_at)
            for i in invoices
            if is_issued(i) and i.invoice_date and i.paid_at and i.paid_at >= one_year_ago
        ]
    )
    payment_days = average_days(
        [
            (i.invoice_date, i.paid_at)
            for i in invoices
            if not is_issued(i) and i.invoice_date and i.paid_at and i.paid_at >= one_year_ago
        ]
    )

    receivables = build_payments_overview(database, direction="ISSUED", today=current_day)
    payables = build_payments_overview(database, direction="RECEIVED", today=current_day)

    top_clients: dict[str, Decimal] = defaultdict(lambda: ZERO)
    top_expenses: dict[str, Decimal] = defaultdict(lambda: ZERO)

    for invoice in in_period:
        if is_issued(invoice):
            top_clients[counterparty(invoice)[0] or "Sin nombre"] += money(invoice.subtotal)
        else:
            top_expenses[invoice.category or "Otros gastos"] += money(invoice.subtotal)

    balance, balance_date = current_balance(database)

    insights: list[str] = []

    if income and result < 0:
        insights.append(
            "En este periodo los gastos superan a los ingresos facturados."
        )

    if income and top_clients:
        biggest_name, biggest_amount = max(top_clients.items(), key=lambda item: item[1])
        share = biggest_amount / income * 100

        if share >= 40:
            insights.append(
                f"{biggest_name} concentra el {share:.0f} % de tu facturación: "
                "dependes mucho de un solo cliente."
            )

    if collection_days is not None and collection_days > 60:
        insights.append(
            f"Tardas de media {collection_days:.0f} días en cobrar; la Ley de "
            "Morosidad fija un máximo de 60 días entre empresas."
        )

    if receivables["overdue_count"]:
        insights.append(
            f"{receivables['overdue_count']} factura(s) emitida(s) vencida(s) "
            "sin cobrar."
        )

    if income and payroll_cost and payroll_cost / income >= Decimal("0.5"):
        insights.append(
            f"El coste de personal supone el {payroll_cost / income * 100:.0f} % "
            "de tus ingresos del periodo."
        )

    if not income:
        insights.append(
            "No hay facturas emitidas en el periodo: súbelas para ver "
            "ingresos, beneficio e IVA repercutido."
        )

    period_label = f"{quarter}T {year}" if quarter else f"Año {year}"

    return {
        "period": period_label,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "income": float(income),
        "expenses": float(expenses),
        "payroll_cost": float(payroll_cost),
        "result": float(result),
        "margin": round(float(result / income * 100), 1) if income else None,
        "vat_output": float(vat_out),
        "vat_input": float(vat_in),
        "vat_balance": float(vat_out - vat_in),
        "receivables_total": receivables["unpaid_total"],
        "receivables_overdue": receivables["overdue_total"],
        "receivables_count": receivables["unpaid_count"],
        "payables_total": payables["unpaid_total"],
        "payables_overdue": payables["overdue_total"],
        "collection_days": collection_days,
        "payment_days": payment_days,
        "bank_balance": balance,
        "bank_balance_date": balance_date,
        "months": months,
        "top_clients": [
            {"name": name, "amount": float(amount)}
            for name, amount in sorted(top_clients.items(), key=lambda item: -item[1])[:5]
        ],
        "top_expenses": [
            {"category": name, "amount": float(amount)}
            for name, amount in sorted(top_expenses.items(), key=lambda item: -item[1])[:5]
        ],
        "insights": insights,
        "note": (
            "Ingresos y gastos por base imponible de facturas aprobadas "
            "(sin IVA) más el coste de las nóminas aprobadas. Es una visión "
            "de gestión, no contabilidad oficial."
        ),
    }
