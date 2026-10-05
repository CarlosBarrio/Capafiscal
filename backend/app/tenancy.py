"""
Aislamiento por cliente (multiempresa) en una sola capa.

Todas las tablas de datos llevan tenant_id (TenantMixin). La sesión de base de
datos sabe para qué cliente trabaja (session.info["tenant_id"]) y:

    - filtra TODAS las lecturas, actualizaciones y borrados ORM por ese cliente
      (incluidas las cargas de relaciones): ninguna consulta puede olvidarlo;
    - marca con ese cliente todo lo que se inserta, y se niega a escribir datos
      de otro cliente.

Modos:
    una sola empresa (AUTH_REQUIRED=false, sin cliente elegido): tenant 0 y sin
        filtro, como siempre.
    multiempresa (AUTH_REQUIRED=true): sin cliente elegido no se ve NADA
        (se filtra por un cliente imposible): falla cerrado.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.orm import Session
from sqlalchemy.orm import with_loader_criteria

from app.models import TenantMixin

NO_TENANT = -1  # en modo estricto, sin cliente elegido: no se ve nada


class TenantError(RuntimeError):
    pass


def strict() -> bool:
    from app.config import settings

    return bool(settings.auth_required)


def set_tenant(session: Session, tenant_id: int | None) -> None:
    session.info["tenant_id"] = tenant_id


def current_tenant(session: Session) -> int | None:
    return session.info.get("tenant_id")


@contextmanager
def tenant_session(tenant_id: int) -> Iterator[Session]:
    """Sesión para trabajar como un cliente (jobs programados, tareas internas)."""
    from app.database import SessionLocal

    session = SessionLocal()
    set_tenant(session, tenant_id)
    try:
        yield session
    finally:
        session.close()


@event.listens_for(Session, "do_orm_execute")
def _filter_by_tenant(state) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.execution_options.get("all_tenants"):
        return
    tenant = state.session.info.get("tenant_id")
    if tenant is None:
        if not strict():
            return  # una sola empresa: sin filtro
        tenant = NO_TENANT
    state.statement = state.statement.options(
        with_loader_criteria(TenantMixin, lambda cls: cls.tenant_id == tenant, include_aliases=True)
    )


@event.listens_for(Session, "before_flush")
def _stamp_tenant(session: Session, _context, _instances) -> None:
    tenant = session.info.get("tenant_id")
    for obj in session.new:
        if not isinstance(obj, TenantMixin):
            continue
        if obj.tenant_id in (None, 0):
            if tenant is None and strict():
                raise TenantError(f"No se puede guardar {type(obj).__name__} sin cliente elegido.")
            obj.tenant_id = tenant or 0
        elif tenant is not None and obj.tenant_id != tenant:
            raise TenantError(f"Intento de guardar datos del cliente {obj.tenant_id} trabajando para el cliente {tenant}.")
    for obj in session.dirty:
        if isinstance(obj, TenantMixin) and tenant is not None and obj.tenant_id not in (tenant,):
            raise TenantError(f"Intento de modificar datos del cliente {obj.tenant_id} trabajando para el cliente {tenant}.")
