from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import msal
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.exc import IntegrityError

from app.config import settings as app_settings
from app.database import Base, SessionLocal, engine
from app.models import TenantMixin


router = APIRouter(
    prefix="/api/connectors/outlook",
    tags=["outlook"],
)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPES = ["User.Read", "Mail.Read"]

# Misma carpeta de datos que la base de datos (configurable con DATA_DIR).
DATA_DIR: Path = app_settings.data_dir
TOKEN_CACHE_FILE = DATA_DIR / "outlook_token_cache.enc"

# Ubicación usada por versiones anteriores (carpeta data/ en la raíz).
_LEGACY_TOKEN_CACHE_FILE = (
    Path(__file__).resolve().parents[2] / "data" / "outlook_token_cache.enc"
)

if (
    _LEGACY_TOKEN_CACHE_FILE.exists()
    and not TOKEN_CACHE_FILE.exists()
    and _LEGACY_TOKEN_CACHE_FILE != TOKEN_CACHE_FILE
):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _LEGACY_TOKEN_CACHE_FILE.replace(TOKEN_CACHE_FILE)

# Los flujos OAuth pendientes viven en memoria.
# Para un despliegue con varios procesos se moverán a Redis o BD.
_PENDING_AUTH_FLOWS: dict[str, dict[str, Any]] = {}


class OutlookImport(TenantMixin, Base):
    """Adjuntos ya importados, por cliente (la tabla filtra y marca tenant_id como las demás)."""

    __tablename__ = "outlook_imports"

    id = Column(Integer, primary_key=True)
    message_id = Column(String(512), nullable=False)
    attachment_id = Column(String(512), nullable=False)
    attachment_name = Column(String(512), nullable=False)
    document_id = Column(Integer, nullable=True)
    imported_at = Column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "message_id",
            "attachment_id",
            name="uq_outlook_tenant_message_attachment",
        ),
    )


# La tabla la crean las migraciones (app/migrate.py).


def _settings() -> dict[str, str]:
    # Se leen de la configuración central para que funcionen tanto las
    # variables de entorno como el archivo .env.
    return {
        "client_id": app_settings.outlook_client_id.strip(),
        "client_secret": app_settings.outlook_client_secret.strip(),
        "tenant_id": app_settings.outlook_tenant_id.strip(),
        "redirect_uri": app_settings.outlook_redirect_uri.strip(),
        "encryption_key": app_settings.app_encryption_key.strip(),
    }


def _configured() -> bool:
    settings = _settings()

    return bool(
        settings["client_id"]
        and settings["client_secret"]
        and settings["tenant_id"]
        and settings["redirect_uri"]
        and settings["encryption_key"]
    )


def _require_configuration() -> dict[str, str]:
    settings = _settings()

    missing = [
        name
        for name, value in settings.items()
        if not value
    ]

    if missing:
        raise HTTPException(
            status_code=503,
            detail={
                "message": (
                    "El conector de Outlook no está configurado."
                ),
                "missing": missing,
            },
        )

    try:
        Fernet(settings["encryption_key"].encode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "message": (
                    "APP_ENCRYPTION_KEY no es una clave Fernet válida."
                )
            },
        ) from exc

    return settings


def _fernet() -> Fernet:
    settings = _require_configuration()
    return Fernet(settings["encryption_key"].encode("utf-8"))


def _load_token_cache() -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()

    if not TOKEN_CACHE_FILE.exists():
        return cache

    try:
        encrypted = TOKEN_CACHE_FILE.read_bytes()
        serialized = _fernet().decrypt(encrypted).decode("utf-8")
        cache.deserialize(serialized)
    except (InvalidToken, OSError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": (
                    "No se pudo leer de forma segura la sesión "
                    "de Outlook."
                )
            },
        ) from exc

    return cache


def _save_token_cache(
    cache: msal.SerializableTokenCache,
) -> None:
    if not cache.has_state_changed:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    serialized = cache.serialize().encode("utf-8")
    encrypted = _fernet().encrypt(serialized)

    TOKEN_CACHE_FILE.write_bytes(encrypted)


def _build_msal_app(
    cache: msal.SerializableTokenCache,
) -> msal.ConfidentialClientApplication:
    settings = _require_configuration()

    authority = (
        "https://login.microsoftonline.com/"
        f"{settings['tenant_id']}"
    )

    return msal.ConfidentialClientApplication(
        client_id=settings["client_id"],
        client_credential=settings["client_secret"],
        authority=authority,
        token_cache=cache,
    )


def _account_username(account: dict[str, Any] | None) -> str | None:
    if not account:
        return None

    return (
        account.get("username")
        or account.get("name")
        or account.get("home_account_id")
    )


def _get_access_token() -> tuple[str, str | None]:
    cache = _load_token_cache()
    application = _build_msal_app(cache)
    accounts = application.get_accounts()

    if not accounts:
        raise HTTPException(
            status_code=401,
            detail={
                "message": (
                    "Outlook no está conectado. Autoriza primero "
                    "la cuenta."
                )
            },
        )

    account = accounts[0]

    result = application.acquire_token_silent(
        scopes=GRAPH_SCOPES,
        account=account,
    )

    _save_token_cache(cache)

    if not result or "access_token" not in result:
        raise HTTPException(
            status_code=401,
            detail={
                "message": (
                    "La autorización de Outlook ha caducado. "
                    "Vuelve a conectar la cuenta."
                ),
                "microsoft_error": (
                    result.get("error_description")
                    if isinstance(result, dict)
                    else None
                ),
            },
        )

    return result["access_token"], _account_username(account)


async def _graph_get(
    client: httpx.AsyncClient,
    token: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = await client.get(
        f"{GRAPH_BASE_URL}{path}",
        params=params,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    if response.status_code >= 400:
        try:
            graph_error = response.json()
        except ValueError:
            graph_error = response.text

        raise HTTPException(
            status_code=502,
            detail={
                "message": "Microsoft Graph devolvió un error.",
                "status": response.status_code,
                "graph_error": graph_error,
            },
        )

    return response.json()


def _already_imported(
    db,
    message_id: str,
    attachment_id: str,
) -> bool:
    return (
        db.query(OutlookImport)
        .filter(
            OutlookImport.message_id == message_id,
            OutlookImport.attachment_id == attachment_id,
        )
        .first()
        is not None
    )


def _extract_document_id(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None

    candidates = [
        payload.get("document_id"),
        payload.get("id"),
        payload.get("document", {}).get("id")
        if isinstance(payload.get("document"), dict)
        else None,
    ]

    for candidate in candidates:
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            continue

    return None


async def _send_to_existing_upload(
    request: Request,
    filename: str,
    content: bytes,
) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=request.app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://capafiscal.local",
        timeout=120.0,
    ) as internal_client:
        response = await internal_client.post(
            "/api/upload",
            files={
                "uploaded_file": (
                    filename,
                    content,
                    "application/pdf",
                )
            },
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {
            "message": response.text or "Respuesta no válida."
        }

    if response.status_code >= 400:
        message = (
            payload.get("detail")
            if isinstance(payload, dict)
            else payload
        )

        raise HTTPException(
            status_code=502,
            detail={
                "message": (
                    f"No se pudo procesar el adjunto {filename}."
                ),
                "upload_error": message,
            },
        )

    return payload


@router.get("/status")
def outlook_status() -> dict[str, Any]:
    if not _configured():
        return {
            "configured": False,
            "connected": False,
            "account": None,
            "message": (
                "Faltan variables de configuración de Microsoft Graph."
            ),
        }

    try:
        cache = _load_token_cache()
        application = _build_msal_app(cache)
        accounts = application.get_accounts()
    except HTTPException as exc:
        return {
            "configured": True,
            "connected": False,
            "account": None,
            "message": str(exc.detail),
        }

    account = accounts[0] if accounts else None

    return {
        "configured": True,
        "connected": bool(account),
        "account": _account_username(account),
        "message": (
            "Cuenta autorizada."
            if account
            else "Pendiente de autorización."
        ),
    }


@router.get("/login")
def outlook_login() -> RedirectResponse:
    settings = _require_configuration()

    cache = _load_token_cache()
    application = _build_msal_app(cache)

    flow = application.initiate_auth_code_flow(
        scopes=GRAPH_SCOPES,
        redirect_uri=settings["redirect_uri"],
        prompt="select_account",
    )

    state = flow.get("state")
    authorization_url = flow.get("auth_uri")

    if not state or not authorization_url:
        raise HTTPException(
            status_code=500,
            detail={
                "message": (
                    "No se pudo iniciar la autorización de Outlook."
                )
            },
        )

    _PENDING_AUTH_FLOWS[state] = flow

    return RedirectResponse(
        url=authorization_url,
        status_code=302,
    )


@router.get("/callback")
def outlook_callback(request: Request) -> RedirectResponse:
    state = request.query_params.get("state", "")
    flow = _PENDING_AUTH_FLOWS.pop(state, None)

    if not flow:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "El flujo de autorización no existe o ha caducado."
                )
            },
        )

    cache = _load_token_cache()
    application = _build_msal_app(cache)

    auth_response = dict(request.query_params)

    try:
        result = application.acquire_token_by_auth_code_flow(
            auth_code_flow=flow,
            auth_response=auth_response,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "Microsoft devolvió una respuesta de "
                    "autorización no válida."
                )
            },
        ) from exc

    if "access_token" not in result:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "No se pudo autorizar Outlook.",
                "microsoft_error": result.get(
                    "error_description",
                    result.get("error"),
                ),
            },
        )

    _save_token_cache(cache)

    return RedirectResponse(
        url="/?outlook=connected",
        status_code=302,
    )


@router.post("/disconnect")
def outlook_disconnect() -> dict[str, Any]:
    if TOKEN_CACHE_FILE.exists():
        TOKEN_CACHE_FILE.unlink()

    _PENDING_AUTH_FLOWS.clear()

    return {
        "success": True,
        "message": "Cuenta de Outlook desconectada.",
    }


@router.post("/sync")
async def sync_outlook(
    request: Request,
    limit: int = 25,
) -> dict[str, Any]:
    token, account = _get_access_token()

    limit = max(1, min(limit, 100))

    imported = 0
    duplicates = 0
    ignored = 0
    failures: list[dict[str, str]] = []

    db = SessionLocal()
    db.info["tenant_id"] = getattr(request.state, "tenant_id", None)  # el cliente elegido, como en get_db

    try:
        async with httpx.AsyncClient(timeout=60.0) as graph_client:
            messages_payload = await _graph_get(
                graph_client,
                token,
                "/me/mailFolders/inbox/messages",
                params={
                    "$top": limit,
                    "$filter": "hasAttachments eq true",
                    "$orderby": "receivedDateTime desc",
                    "$select": (
                        "id,subject,receivedDateTime,"
                        "from,hasAttachments"
                    ),
                },
            )

            messages = messages_payload.get("value", [])

            for message in messages:
                message_id = message.get("id")

                if not message_id:
                    continue

                safe_message_id = quote(
                    message_id,
                    safe="",
                )

                attachments_payload = await _graph_get(
                    graph_client,
                    token,
                    (
                        f"/me/messages/{safe_message_id}"
                        "/attachments"
                    ),
                    params={
                        "$select": (
                            "id,name,contentType,size,"
                            "isInline,contentBytes"
                        )
                    },
                )

                for attachment in attachments_payload.get(
                    "value",
                    [],
                ):
                    attachment_id = attachment.get("id")
                    filename = attachment.get("name", "")

                    if not attachment_id:
                        ignored += 1
                        continue

                    is_pdf = (
                        filename.lower().endswith(".pdf")
                        or attachment.get("contentType")
                        == "application/pdf"
                    )

                    if (
                        not is_pdf
                        or attachment.get("isInline") is True
                    ):
                        ignored += 1
                        continue

                    if _already_imported(
                        db,
                        message_id,
                        attachment_id,
                    ):
                        duplicates += 1
                        continue

                    encoded_content = attachment.get(
                        "contentBytes"
                    )

                    if not encoded_content:
                        failures.append({
                            "filename": filename,
                            "error": (
                                "Microsoft Graph no devolvió "
                                "el contenido del adjunto."
                            ),
                        })
                        continue

                    try:
                        content = base64.b64decode(
                            encoded_content,
                            validate=True,
                        )

                        upload_result = (
                            await _send_to_existing_upload(
                                request,
                                filename,
                                content,
                            )
                        )

                        document_id = _extract_document_id(
                            upload_result
                        )

                        record = OutlookImport(
                            message_id=message_id,
                            attachment_id=attachment_id,
                            attachment_name=filename,
                            document_id=document_id,
                        )

                        db.add(record)
                        db.commit()

                        if upload_result.get("duplicate"):
                            duplicates += 1
                        else:
                            imported += 1

                    except IntegrityError:
                        db.rollback()
                        duplicates += 1

                    except Exception as exc:
                        db.rollback()

                        failures.append({
                            "filename": filename,
                            "error": str(exc),
                        })

    finally:
        db.close()

    return {
        "success": not failures,
        "account": account,
        "imported": imported,
        "duplicates": duplicates,
        "ignored": ignored,
        "failures": failures,
        "message": (
            f"Outlook sincronizado: {imported} importado(s), "
            f"{duplicates} duplicado(s), "
            f"{ignored} ignorado(s)."
        ),
    }