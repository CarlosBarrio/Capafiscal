"""
Usuarios, sesiones, roles y cliente activo.

Activado con AUTH_REQUIRED=true. Cada petición a /api lleva:

    Authorization: Bearer <token>     (POST /api/auth/login)
    X-Client-Id: <id del cliente>     (si el usuario tiene más de uno)

Roles:
    ADMIN    todo, en todos los clientes de su gestoría; gestiona clientes y usuarios
    GESTOR   todo en los clientes asignados, salvo administrar usuarios y clientes
    REVISOR  ve y decide (aprobar, rechazar, corregir, resolver) en sus clientes;
             no cambia configuración ni borra
    CLIENTE  ve su empresa y aporta documentos (subidas, correo, extracto, adjuntos)
    LECTURA  solo ve

El aislamiento de datos no depende de este módulo sino de app/tenancy.py: aquí
solo se decide QUIÉN es y PARA QUÉ cliente trabaja la petición.
"""
from __future__ import annotations

from app import clock
import hashlib
import hmac
import re
import secrets
from datetime import timedelta
from datetime import timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiSession
from app.models import Client
from app.models import User
from app.models import UserClient

ROLES = ("ADMIN", "GESTOR", "REVISOR", "CLIENTE", "LECTURA")
PUBLIC_PATHS = ("/api/auth/login", "/api/auth/setup", "/api/auth/status", "/api/portal/", "/portal/")
SESSION_COOKIE = "cf_session"
CLIENT_COOKIE = "cf_client"
CSRF_HEADER = "x-capafiscal"  # las escrituras autenticadas por cookie deben llevarla (otra web no puede ponerla)
NO_CLIENT_PATHS = ("/api/auth/", "/api/admin/")  # no trabajan sobre los datos de un cliente
CLIENT_WRITES = (
    r"^/api/upload$", r"^/api/connectors/email/import$", r"^/api/bank/import$", r"^/api/cases/\d+/attachments$", r"^/api/auth/logout$",
    r"^/api/simulate$",  # calcula sin guardar nada
)
REVIEW_WRITES = (
    r"^/api/invoices/\d+(/(approve|reject|reopen|payment))?$", r"^/api/cases/\d+(/(approve|resolve|reopen|rerun|file|request-documents))?$",
    r"^/api/notifications/\d+$", r"^/api/bank/transactions/\d+/(confirm|unmatch|allocate|accept-proposal)$", r"^/api/bank/(confirm-suggestions|reconcile)$",
    r"^/api/events/\d+/retry$", r"^/api/intelligence/items/\d+/status$",
)
ADMIN_WRITES = (r"^/api/learning/rules/\d+/(aprobar|rechazar|retirar)$",)  # cambiar cómo trabaja el sistema es cosa del administrador
ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algorithm, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
    return hmac.compare_digest(candidate, digest)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(database: Session, user: User) -> str:
    from app.config import settings

    token = secrets.token_urlsafe(32)
    database.add(ApiSession(token_hash=token_hash(token), user_id=user.id, expires_at=clock.now() + timedelta(hours=settings.session_hours)))
    database.flush()
    return token


def user_for_token(database: Session, token: str | None) -> User | None:
    if not token:
        return None
    session = database.scalar(select(ApiSession).where(ApiSession.token_hash == token_hash(token)))
    if session is None:
        return None
    expires = session.expires_at if session.expires_at.tzinfo else session.expires_at.replace(tzinfo=timezone.utc)
    if expires < clock.now():
        return None
    user = database.get(User, session.user_id)
    return user if user is not None and user.active else None


def client_ids(database: Session, user: User) -> list[int]:
    if user.role == "ADMIN":
        return list(database.scalars(select(Client.id).where(Client.organization_id == user.organization_id, Client.active.is_(True))).all())
    return list(database.scalars(
        select(UserClient.client_id).join(Client, Client.id == UserClient.client_id).where(UserClient.user_id == user.id, Client.active.is_(True))
    ).all())


def permitted(role: str, method: str, path: str) -> bool:
    """¿Puede este rol hacer esta petición?"""
    method = method.upper()
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if path.startswith("/api/admin/") or any(re.match(pattern, path) for pattern in ADMIN_WRITES):
        return role == "ADMIN"
    if role in {"ADMIN", "GESTOR"}:
        return True
    if role == "LECTURA":
        return path in ("/api/auth/logout", "/api/simulate")
    if method == "DELETE":
        return False
    writes = CLIENT_WRITES + (REVIEW_WRITES if role == "REVISOR" else ())
    return any(re.match(pattern, path) for pattern in writes)


def is_public(path: str) -> bool:
    return not path.startswith("/api/") or any(path.startswith(prefix) for prefix in PUBLIC_PATHS)


def needs_client(path: str) -> bool:
    return not any(path.startswith(prefix) for prefix in NO_CLIENT_PATHS)


def resolve_request(database: Session, *, method: str, path: str, authorization: str | None, client_header: str | None,
                    cookie_token: str | None = None, csrf: str | None = None) -> dict[str, Any]:
    """Quién es, qué puede hacer y para qué cliente: {"user", "tenant_id"} o {"error": (código, motivo)}."""
    token = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else None
    if token is None and cookie_token:
        if method.upper() not in {"GET", "HEAD", "OPTIONS"} and not csrf:
            return {"error": (403, "Falta la cabecera X-CapaFiscal (protección CSRF).")}
        token = cookie_token
    user = user_for_token(database, token)
    if user is None:
        return {"error": (401, "Inicia sesión (POST /api/auth/login).")}
    if not permitted(user.role, method, path):
        return {"error": (403, f"Tu rol ({user.role}) no permite esta acción.")}
    tenant = None
    if needs_client(path):
        allowed = client_ids(database, user)
        if client_header:
            try:
                tenant = int(client_header)
            except ValueError:
                return {"error": (400, "X-Client-Id no es válido.")}
            if tenant not in allowed:
                return {"error": (403, "No tienes acceso a ese cliente.")}
        elif len(allowed) == 1:
            tenant = allowed[0]
        else:
            return {"error": (400, "Elige el cliente con la cabecera X-Client-Id.")}
    return {"user": user, "tenant_id": tenant}


def adopt_single_company_data(database: Session, client_id: int) -> int:
    """Pasa los datos de la instalación de una sola empresa (tenant 0) al primer cliente."""
    from sqlalchemy import text

    from app.database import Base
    from app.models import TenantMixin

    moved = 0
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if isinstance(cls, type) and issubclass(cls, TenantMixin):
            result = database.execute(text(f'UPDATE "{cls.__tablename__}" SET tenant_id = :client WHERE tenant_id = 0'), {"client": client_id})
            moved += result.rowcount or 0
    return moved
