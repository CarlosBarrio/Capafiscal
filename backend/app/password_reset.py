"""
Recuperar el acceso sin que nadie conozca la contraseña de otro.

    «He olvidado mi contraseña»   llega un enlace al correo del usuario (si existe; la respuesta es la
                                  misma exista o no, para no revelar quién tiene cuenta)
    enlace del administrador      sin servidor de correo, el administrador genera el enlace y se lo hace
                                  llegar; tampoco él elige ni ve la contraseña

El enlace lleva una firma (HMAC-SHA256) de: usuario, caducidad (1 hora) y el hash de la contraseña
actual. Por eso sirve una sola vez: al cambiar la contraseña, la firma deja de cuadrar. No se guarda
nada en la base de datos. Al cambiarla se cierran todas las sesiones abiertas de ese usuario.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import clock
from app.models import ApiSession
from app.models import User

VALID_SECONDS = 3600
logger = logging.getLogger(__name__)


def server_secret() -> bytes:
    """APP_SECRET_KEY; si no está, una clave aleatoria que se crea una vez y se guarda en DATA_DIR."""
    from app.config import settings

    if settings.app_secret_key:
        return settings.app_secret_key.encode()
    path = Path(settings.data_dir) / ".secret_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(secrets.token_urlsafe(48))
    return path.read_text().strip().encode()


def signature(user: User, expires: int) -> str:
    digest = hmac.new(server_secret(), f"{user.id}.{expires}.{user.password_hash}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def make_token(user: User) -> str:
    expires = int(clock.now().timestamp()) + VALID_SECONDS
    return f"{user.id}.{expires}.{signature(user, expires)}"


def user_for_token(database: Session, token: str) -> User | None:
    """El usuario del enlace, si la firma es buena, no ha caducado, no se ha usado y la cuenta sigue activa."""
    try:
        user_id, expires, signed = token.split(".", 2)
        user_id_number, expires_number = int(user_id), int(expires)
    except ValueError:
        return None
    if expires_number < clock.now().timestamp():
        return None
    user = database.get(User, user_id_number)
    if user is None or not user.active:
        return None
    return user if hmac.compare_digest(signature(user, expires_number), signed) else None


def link(token: str) -> str:
    from app.config import settings

    return f"{settings.public_base_url.rstrip('/')}/?reset={token}"


def set_password(database: Session, user: User, password: str) -> int:
    """Cambia la contraseña y cierra todas las sesiones del usuario. Devuelve cuántas se cerraron."""
    from app.auth import hash_password

    user.password_hash = hash_password(password)
    closed = database.execute(delete(ApiSession).where(ApiSession.user_id == user.id)).rowcount or 0
    return closed


def send_link(user: User, token: str) -> bool:
    """Envía el enlace por correo. Sin SMTP no se envía (y el enlace nunca va a los registros)."""
    from app.config import settings
    from app.outbox_service import smtp_configured
    from app.outbox_service import smtp_deliver

    if not smtp_configured():
        logger.warning("Recuperación de contraseña pedida para el usuario %s, pero no hay SMTP: un administrador puede generar el enlace.", user.id)
        return False
    email = EmailMessage()
    email["From"] = settings.smtp_from or settings.smtp_user
    email["To"] = user.email
    email["Subject"] = "CapaFiscal · Cambiar la contraseña"
    email.set_content(
        "Hola:\n\nAlguien (esperamos que tú) ha pedido cambiar la contraseña de CapaFiscal de esta cuenta.\n\n"
        f"Abre este enlace en la próxima hora para elegir una nueva:\n{link(token)}\n\n"
        "El enlace solo sirve una vez. Si no lo has pedido tú, ignora este correo: tu contraseña no cambia.\n"
    )
    try:
        smtp_deliver(email)
    except Exception:  # noqa: BLE001 — el correo no puede romper la respuesta (que es siempre la misma)
        logger.exception("No se pudo enviar el enlace de recuperación al usuario %s", user.id)
        return False
    return True
