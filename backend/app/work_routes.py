"""API del Centro de trabajo: /api/work."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi import Request

from app.deps import DatabaseDependency

router = APIRouter(prefix="/api/work", tags=["Centro de trabajo"])


@router.get("")
def work(request: Request, database: DatabaseDependency) -> dict[str, Any]:
    """Una sola lista: qué decides tú, qué falta, qué está haciendo CapaFiscal y qué se ha resuelto. Solo lee."""
    from app.workcenter import work_center

    user = getattr(request.state, "user", None)
    return work_center(database, user_name=getattr(user, "name", None))
