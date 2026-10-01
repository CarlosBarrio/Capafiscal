"""API de contabilización: /api/accounting (solo lee)."""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Query
from fastapi import Response

from app.deps import DatabaseDependency

router = APIRouter(prefix="/api/accounting", tags=["Contabilidad"])


def period(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    from app.closing import default_period
    from app.closing import parse_period

    if date_from is None or date_to is None:
        start, end = parse_period(default_period())
        date_from, date_to = date_from or start, date_to or end
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="La fecha inicial es posterior a la final.")
    return date_from, date_to


@router.get("/journal")
def journal_view(database: DatabaseDependency, date_from: date | None = None, date_to: date | None = None,
                 format: str = Query(default="json", pattern="^(json|csv|xlsx)$")) -> Any:
    """Libro diario del periodo (por defecto, el mes anterior): asientos, saldos y comprobaciones."""
    from app import accounting

    start, end = period(date_from, date_to)
    report = accounting.journal(database, start, end)
    name = f"borrador_libro_diario_{start:%Y%m%d}_{end:%Y%m%d}"
    if format == "csv":
        return Response(accounting.to_csv(report), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    if format == "xlsx":
        return Response(accounting.to_xlsx(report), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    return accounting.serialize(report)
