from __future__ import annotations

import re
from typing import Annotated

from fastapi import Depends
from fastapi import Header
from fastapi import Request
from sqlalchemy.orm import Session

from app.database import get_db


SAFE_ACTOR_PATTERN = re.compile(
    r"[^a-zA-Z0-9@._\-\s]"
)

ANONYMOUS_ACTOR = "usuario-local"  # una sola empresa sin inicio de sesión: no hay identidad que registrar


DatabaseDependency = Annotated[
    Session,
    Depends(get_db),
]


def clean_actor(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = SAFE_ACTOR_PATTERN.sub("", value).strip()[:100]
    return cleaned or None


def resolve_actor(
    request: Request,
    declared: Annotated[str | None, Header(alias="X-Actor")] = None,
) -> str:
    """Quién firma la auditoría de esta petición: el usuario autenticado, nunca la cabecera.

    X-Actor lo puede poner cualquiera: el middleware identify_request lo guarda aparte como «declarado»
    (request_context.declared_actor; add_audit_event lo anota en event_data["declared_actor"]) y no
    suplanta a nadie. El parámetro se declara solo para que siga apareciendo en OpenAPI. Sin sesión (una sola
    empresa sin AUTH_REQUIRED) el actor es «usuario-local».
    """
    user = getattr(request.state, "user", None)
    if user is not None:
        return clean_actor(user.email) or f"usuario-{user.id}"
    return ANONYMOUS_ACTOR


# Se mantiene el nombre (64 rutas lo usan): ya no es la cabecera, es la identidad resuelta.
ActorHeader = Annotated[
    str,
    Depends(resolve_actor),
]


def normalize_actor(
    actor: str | None,
) -> str:
    """Limpia un actor YA resuelto (resolve_actor). No convierte ninguna cabecera en identidad."""
    return clean_actor(actor) or ANONYMOUS_ACTOR
