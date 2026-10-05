"""
Contadores atómicos por cliente.

    next_value(database, "case:2026")       → 1, 2, 3… sin repetir aunque lleguen 100 eventos a la vez
    next_value(database, "sales-chain")     → además bloquea la fila hasta el commit: sirve de cerrojo
                                              para que la cadena de huellas de facturación sea lineal

Un único INSERT … ON CONFLICT DO UPDATE SET value = value + 1 RETURNING value, en PostgreSQL y SQLite.
`seed` da el valor de partida la primera vez (p. ej. los expedientes que ya existían antes del contador).
"""
from __future__ import annotations

from typing import Callable

from sqlalchemy.orm import Session

from app.models import Counter


def next_value(database: Session, name: str, *, seed: Callable[[], int] | None = None) -> int:
    from app.tenancy import TenantError
    from app.tenancy import current_tenant
    from app.tenancy import strict

    tenant = current_tenant(database)
    if tenant is None and strict():
        raise TenantError("No se puede numerar sin cliente elegido.")
    if database.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    start = (seed() if seed else 0) + 1
    statement = insert(Counter).values(tenant_id=tenant or 0, name=name, value=start)
    statement = statement.on_conflict_do_update(
        index_elements=["tenant_id", "name"], set_={"value": Counter.__table__.c.value + 1}
    ).returning(Counter.__table__.c.value)
    return int(database.execute(statement).scalar_one())
