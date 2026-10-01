"""Acceso (setup, login, logout, me) y administración de clientes y usuarios."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy import select

from app.auth import CLIENT_COOKIE
from app.auth import ROLES
from app.auth import SESSION_COOKIE
from app.auth import adopt_single_company_data
from app.auth import client_ids
from app.auth import create_session
from app.auth import hash_password
from app.auth import token_hash
from app.auth import verify_password
from app.deps import DatabaseDependency
from app.models import ApiSession
from app.models import Client
from app.models import Organization
from app.models import User
from app.models import UserClient

router = APIRouter(prefix="/api")


class SetupPayload(BaseModel):
    organization: str = Field(min_length=2, max_length=255)
    admin_email: str = Field(min_length=3, max_length=255)
    admin_password: str = Field(min_length=10, max_length=200)
    admin_name: str | None = Field(default=None, max_length=255)
    client_name: str = Field(min_length=2, max_length=255)
    client_tax_id: str | None = Field(default=None, max_length=20)
    adopt_existing_data: bool = True


class LoginPayload(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class ClientPayload(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    tax_id: str | None = Field(default=None, max_length=20)


class UserPayload(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=10, max_length=200)
    name: str | None = Field(default=None, max_length=255)
    role: str = Field(pattern="^(ADMIN|GESTOR|REVISOR|CLIENTE|LECTURA)$")
    client_ids: list[int] = []


class UserUpdate(BaseModel):
    role: str | None = Field(default=None, pattern="^(ADMIN|GESTOR|REVISOR|CLIENTE|LECTURA)$")
    active: bool | None = None
    client_ids: list[int] | None = None
    password: str | None = Field(default=None, min_length=10, max_length=200)


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Inicia sesión.")
    return user


def serialize_user(database, user: User) -> dict[str, Any]:
    clients = database.scalars(select(Client).where(Client.id.in_(client_ids(database, user)))).all()
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role, "active": user.active,
            "clients": [{"id": item.id, "name": item.name, "tax_id": item.tax_id} for item in clients]}


@router.post("/auth/setup", tags=["Acceso"], status_code=201)
def setup(payload: SetupPayload, database: DatabaseDependency, response: Response) -> dict[str, Any]:
    """Primer arranque multiempresa: gestoría, primer cliente y usuario administrador."""
    if database.scalar(select(User.id).limit(1)) is not None:
        raise HTTPException(status_code=409, detail="Ya está configurado: entra con un usuario administrador.")
    organization = Organization(name=payload.organization)
    database.add(organization)
    database.flush()
    client = Client(organization_id=organization.id, name=payload.client_name, tax_id=payload.client_tax_id)
    admin = User(organization_id=organization.id, email=payload.admin_email.lower().strip(), name=payload.admin_name,
                 password_hash=hash_password(payload.admin_password), role="ADMIN")
    database.add_all([client, admin])
    database.flush()
    moved = adopt_single_company_data(database, client.id) if payload.adopt_existing_data else 0
    token = create_session(database, admin)
    database.commit()
    set_session_cookie(response, token)
    return {"token": token, "user": serialize_user(database, admin), "client_id": client.id, "adopted_rows": moved}


@router.post("/auth/login", tags=["Acceso"])
def login(payload: LoginPayload, database: DatabaseDependency, response: Response) -> dict[str, Any]:
    user = database.scalar(select(User).where(User.email == payload.email.lower().strip()))
    if user is None or not user.active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos.")
    token = create_session(database, user)
    database.commit()
    set_session_cookie(response, token)
    return {"token": token, "user": serialize_user(database, user)}


@router.post("/auth/logout", tags=["Acceso"])
def logout(request: Request, database: DatabaseDependency, response: Response) -> dict[str, Any]:
    authorization = request.headers.get("authorization") or ""
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else request.cookies.get(SESSION_COOKIE)
    if token:
        session = database.scalar(select(ApiSession).where(ApiSession.token_hash == token_hash(token)))
        if session is not None:
            database.delete(session)
            database.commit()
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(CLIENT_COOKIE)
    return {"success": True}


@router.get("/auth/status", tags=["Acceso"])
def status() -> dict[str, Any]:
    """¿Hace falta iniciar sesión? (la interfaz lo pregunta al arrancar)."""
    from app.config import settings

    return {"auth_required": settings.auth_required}


class ClientChoice(BaseModel):
    client_id: int


@router.post("/auth/client", tags=["Acceso"])
def choose_client(payload: ClientChoice, request: Request, database: DatabaseDependency, response: Response) -> dict[str, Any]:
    """Elige el cliente con el que trabaja el navegador (cookie)."""
    user = current_user(request)
    if payload.client_id not in client_ids(database, user):
        raise HTTPException(status_code=403, detail="No tienes acceso a ese cliente.")
    response.set_cookie(CLIENT_COOKIE, str(payload.client_id), httponly=True, samesite="lax")
    return {"client_id": payload.client_id}


def set_session_cookie(response: Response, token: str) -> None:
    from app.config import settings

    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=settings.session_hours * 3600)


@router.get("/auth/me", tags=["Acceso"])
def me(request: Request, database: DatabaseDependency) -> dict[str, Any]:
    return serialize_user(database, current_user(request))


@router.get("/admin/clients", tags=["Administración"])
def list_clients(request: Request, database: DatabaseDependency) -> list[dict[str, Any]]:
    user = current_user(request)
    clients = database.scalars(select(Client).where(Client.organization_id == user.organization_id).order_by(Client.name)).all()
    return [{"id": item.id, "name": item.name, "tax_id": item.tax_id, "active": item.active} for item in clients]


@router.post("/admin/clients", tags=["Administración"], status_code=201)
def create_client(payload: ClientPayload, request: Request, database: DatabaseDependency) -> dict[str, Any]:
    user = current_user(request)
    client = Client(organization_id=user.organization_id, name=payload.name, tax_id=payload.tax_id)
    database.add(client)
    database.commit()
    return {"id": client.id, "name": client.name, "tax_id": client.tax_id, "active": client.active}


@router.get("/admin/users", tags=["Administración"])
def list_users(request: Request, database: DatabaseDependency) -> list[dict[str, Any]]:
    user = current_user(request)
    return [serialize_user(database, item) for item in database.scalars(select(User).where(User.organization_id == user.organization_id).order_by(User.email)).all()]


def assign_clients(database, admin: User, user: User, ids: list[int]) -> None:
    own = set(database.scalars(select(Client.id).where(Client.organization_id == admin.organization_id)).all())
    if set(ids) - own:
        raise HTTPException(status_code=400, detail="Alguno de esos clientes no es de tu gestoría.")
    for link in database.scalars(select(UserClient).where(UserClient.user_id == user.id)).all():
        database.delete(link)
    database.flush()
    database.add_all(UserClient(user_id=user.id, client_id=client_id) for client_id in sorted(set(ids)))


@router.post("/admin/users", tags=["Administración"], status_code=201)
def create_user(payload: UserPayload, request: Request, database: DatabaseDependency) -> dict[str, Any]:
    admin = current_user(request)
    if database.scalar(select(User.id).where(User.email == payload.email.lower().strip())):
        raise HTTPException(status_code=409, detail="Ya existe un usuario con ese correo.")
    user = User(organization_id=admin.organization_id, email=payload.email.lower().strip(), name=payload.name,
                password_hash=hash_password(payload.password), role=payload.role)
    database.add(user)
    database.flush()
    assign_clients(database, admin, user, payload.client_ids)
    database.commit()
    return serialize_user(database, user)


@router.patch("/admin/users/{user_id}", tags=["Administración"])
def update_user(user_id: int, payload: UserUpdate, request: Request, database: DatabaseDependency) -> dict[str, Any]:
    admin = current_user(request)
    user = database.get(User, user_id)
    if user is None or user.organization_id != admin.organization_id:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if payload.role is not None:
        user.role = payload.role
    if payload.active is not None:
        user.active = payload.active
    if payload.password:
        user.password_hash = hash_password(payload.password)
    if payload.client_ids is not None:
        assign_clients(database, admin, user, payload.client_ids)
    database.commit()
    return serialize_user(database, user)


assert set(ROLES) == {"ADMIN", "GESTOR", "REVISOR", "CLIENTE", "LECTURA"}
