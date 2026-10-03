"""
Quién está detrás de la petición en curso (para la auditoría), sin pasarlo por todas las funciones.

    authenticated_user   el usuario de la sesión (identify_request en main.py). Es la ÚNICA identidad.
    declared_actor       lo que el cliente dice en la cabecera X-Actor: se guarda aparte, como dato
                         declarado, y nunca sustituye al usuario autenticado.

Fuera de una petición (planificador, conectores, scripts) no hay usuario: el actor lo pone quien
llama («automatizacion», «conector-correo», «system»…).
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

_user: ContextVar[dict[str, Any] | None] = ContextVar("capafiscal_user", default=None)
_declared: ContextVar[str | None] = ContextVar("capafiscal_declared_actor", default=None)


def set_user(user: Any | None):
    value = {"id": user.id, "email": user.email, "role": user.role} if user is not None else None
    return _user.set(value)


def reset_user(token) -> None:
    _user.reset(token)


def authenticated_user() -> dict[str, Any] | None:
    return _user.get()


def set_declared_actor(value: str | None):
    return _declared.set(value or None)


def reset_declared_actor(token) -> None:
    _declared.reset(token)


def declared_actor() -> str | None:
    return _declared.get()
