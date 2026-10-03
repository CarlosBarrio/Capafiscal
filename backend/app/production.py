"""
¿Está esta instalación lista para producción? (APP_ENVIRONMENT=production)

    python -m app.production        la lista, con lo que falta y cómo arreglarlo

Al arrancar en producción se hace la misma comprobación: lo que pondría datos en peligro impide
arrancar (mejor no arrancar que exponer datos fiscales); lo demás queda en los registros como aviso.
"""
from __future__ import annotations

import os
import sys
from typing import Any

CRITICAL = "crítico"
WARNING = "aviso"


def problems(settings: Any | None = None) -> list[tuple[str, str]]:
    """[(gravedad, qué falta y cómo arreglarlo)]"""
    from app.config import settings as current

    settings = settings or current
    found: list[tuple[str, str]] = []

    def add(level: str, text: str) -> None:
        found.append((level, text))

    if settings.debug:
        add(CRITICAL, "DEBUG=true: un error mostraría el detalle interno al usuario. Pon DEBUG=false.")
    if settings.reset_data_on_startup:
        add(CRITICAL, "RESET_DATA_ON_STARTUP=true borraría los datos al arrancar. Quítalo.")
    if settings.seed_demo_data or settings.enable_demo_connectors:
        add(CRITICAL, "Datos o conectores de demostración activados (SEED_DEMO_DATA / ENABLE_DEMO_CONNECTORS). Desactívalos.")
    if not settings.auth_required:
        add(CRITICAL, "AUTH_REQUIRED=false: cualquiera que llegue a la dirección vería los datos. Pon AUTH_REQUIRED=true.")
    if not settings.public_base_url.lower().startswith("https://"):
        add(CRITICAL, "PUBLIC_BASE_URL no es https://: contraseñas y cookies viajarían sin cifrar. Sirve CapaFiscal detrás de HTTPS.")
    if settings.database_url.startswith("sqlite"):
        add(WARNING, "SQLite en producción: válido para una empresa y un solo proceso; para varias o varios procesos, PostgreSQL.")
    if not settings.app_secret_key:
        add(WARNING, "Sin APP_SECRET_KEY: los enlaces de contraseña se firman con una clave guardada en DATA_DIR. "
                     "Con varias instancias, define la misma APP_SECRET_KEY en todas.")
    if not settings.app_encryption_key and settings.outlook_client_id:
        add(CRITICAL, "Outlook configurado sin APP_ENCRYPTION_KEY: los tokens del correo quedarían sin cifrar.")
    if settings.auth_required and (settings.imap_host or settings.imap_user) and settings.email_client_id is None:
        add(WARNING, "Multiempresa con IMAP configurado y sin EMAIL_CLIENT_ID: el buzón es común y no se procesa para "
                     "ningún cliente (los correos quedan sin leer). Asígnalo a un cliente o quita IMAP_*.")
    if settings.auth_required and settings.dehu_inbox_dir and settings.dehu_client_id is None:
        add(WARNING, "Multiempresa con DEHU_INBOX_DIR y sin DEHU_CLIENT_ID: la carpeta de la DEHú no se lee para ningún cliente.")
    if settings.auth_required and settings.outlook_client_id:
        add(WARNING, "Outlook está configurado pero no funciona en multiempresa (una sola conexión para toda la instalación).")
    if not os.environ.get("BACKUP_PASSPHRASE"):
        add(WARNING, "Sin BACKUP_PASSPHRASE: las copias de seguridad irían sin cifrar.")
    if not settings.smtp_host:
        add(WARNING, "Sin SMTP: no llegan los enlaces de «He olvidado mi contraseña» (el administrador puede generarlos).")
    elif settings.smtp_host.strip().lower() in {"localhost", "127.0.0.1", "::1", "mailpit", "mailhog"}:
        add(WARNING, f"SMTP_HOST={settings.smtp_host} es un buzón de pruebas: los correos no llegarían a nadie. Usa el servidor de correo real.")
    return found


def check_on_startup() -> None:
    """En producción: se niega a arrancar si hay algo crítico; registra los avisos."""
    import logging

    from app.config import settings

    if settings.app_environment.lower() != "production":
        return
    logger = logging.getLogger("capafiscal.security")
    found = problems(settings)
    for level, text in found:
        if level == WARNING:
            logger.warning("Producción: %s", text)
    critical = [text for level, text in found if level == CRITICAL]
    if critical:
        raise RuntimeError("CapaFiscal no arranca en producción con esta configuración:\n- " + "\n- ".join(critical))


def main() -> int:
    found = problems()
    if not found:
        print("✓ Lista para producción.")
        return 0
    for level, text in found:
        print(f"{'✗' if level == CRITICAL else '!'} {text}")
    return 1 if any(level == CRITICAL for level, _ in found) else 0


if __name__ == "__main__":
    sys.exit(main())
