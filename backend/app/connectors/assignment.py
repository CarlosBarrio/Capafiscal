"""
Fuentes compartidas en multiempresa: a qué cliente pertenecen.

El buzón de correo (IMAP y la carpeta data/buzon) y la carpeta de la DEHú se configuran una sola vez para
toda la instalación (variables de entorno), pero los datos de cada cliente viven aislados (app/tenancy.py).
Sin una regla explícita, el planificador recorre los clientes uno a uno y el primero se quedaría con todo lo
que hubiera en el buzón, sea de quien sea.

Regla: una fuente compartida solo se procesa para el cliente al que está asignada (EMAIL_CLIENT_ID,
DEHU_CLIENT_ID). Sin asignación, en multiempresa no se procesa para nadie: lo que llega queda sin leer. Es
preferible dejarlo sin asignar que guardarlo en el cliente equivocado.

Con una sola empresa (AUTH_REQUIRED=false) no hay a quién equivocarse y todo sigue como siempre.

Lo que falta para un buzón por cliente o un buzón de gestoría con reparto (por NIF, remitente o reglas, con
cola de «sin asignar»): credenciales por cliente guardadas cifradas y una bandeja global de entrada previa
al cliente. Ver docs/correo_multiempresa.md.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

SOURCES = {
    "email": ("email_client_id", "EMAIL_CLIENT_ID", "El buzón de correo"),
    "dehu": ("dehu_client_id", "DEHU_CLIENT_ID", "La carpeta de la DEHú"),
}


class SourceNotAssigned(RuntimeError):
    pass


def assigned_client(source: str) -> int | None:
    from app.config import settings

    return getattr(settings, SOURCES[source][0])


def blocked_reason(database: Session, source: str) -> str | None:
    """None si esta sesión (su cliente) puede procesar la fuente compartida; si no, por qué."""
    from app.config import settings
    from app.tenancy import current_tenant

    if not settings.auth_required:
        return None
    _attribute, variable, label = SOURCES[source]
    assigned = assigned_client(source)
    if assigned is None:
        return (f"{label} es común a toda la instalación y no está asignado a ningún cliente ({variable}): "
                "no se procesa para no guardarlo en el cliente equivocado. Lo recibido queda sin leer.")
    if current_tenant(database) != assigned:
        return f"{label} está asignado a otro cliente: aquí no se procesa."
    return None


def require_assigned(database: Session, source: str) -> None:
    reason = blocked_reason(database, source)
    if reason:
        raise SourceNotAssigned(reason)


def status(database: Session, source: str) -> dict[str, object]:
    from app.config import settings

    reason = blocked_reason(database, source)
    return {"multiempresa": bool(settings.auth_required), "assigned_client": assigned_client(source),
            "available_here": reason is None, "reason": reason}
