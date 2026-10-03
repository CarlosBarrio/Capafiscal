"""
De dónde llegan los correos.

  · IMAP: el buzón de la empresa (IMAP_HOST, IMAP_USER, IMAP_PASSWORD…). Se
    leen los no leídos y se marcan como leídos solo si se procesaron bien.
  · Carpeta local data/buzon: deja ahí archivos .eml (por ejemplo, una
    regla de Outlook que los guarde); los procesados pasan a
    data/buzon/procesados.
  · Importación manual de un .eml desde la pantalla o la API.
"""
from __future__ import annotations

import imaplib
import logging
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.connectors.email.mapper import ingest_email
from app.connectors.email.parser import parse_eml

logger = logging.getLogger(__name__)


def mailbox_folder() -> Path:
    return settings.data_dir / "buzon"


def imap_configured() -> bool:
    return bool(settings.imap_host and settings.imap_user and settings.imap_password)


def status(database: Session | None = None) -> dict[str, Any]:
    from app.connectors.assignment import status as assignment_status

    folder = mailbox_folder()
    pending = sorted(folder.glob("*.eml")) if folder.exists() else []
    return {
        "assignment": assignment_status(database, "email") if database is not None else None,
        "imap": {"configured": imap_configured(), "host": settings.imap_host or None, "user": settings.imap_user or None, "folder": settings.imap_folder},
        "local_folder": {"path": str(folder), "exists": folder.exists(), "pending": len(pending)},
    }


def import_eml(database: Session, raw: bytes, *, provider: str = "importacion") -> dict[str, Any]:
    return ingest_email(database, parse_eml(raw), provider=provider)


# Transacciones: cada correo es una unidad de trabajo. import_eml confirma (commit) lo que va guardando
# (documento, lectura, evento), igual que una subida manual; si un correo falla, rollback() deshace solo lo
# que quedaba sin confirmar de ESE correo. Por eso quien llama no puede tener trabajo pendiente ni envolver
# esto en un savepoint (ver Automation.own_transactions). Un correo fallido no se marca como leído ni se
# mueve: se reintenta en la siguiente vuelta y la idempotencia (huella del documento + evento
# «<Message-ID>#<huella>») evita duplicarlo.


def poll_folder(database: Session, failures: list[str] | None = None) -> list[dict[str, Any]]:
    folder = mailbox_folder()
    if not folder.exists():
        return []
    done = folder / "procesados"
    results = []
    for path in sorted(folder.glob("*.eml")):
        try:
            results.append(import_eml(database, path.read_bytes(), provider="carpeta"))
        except Exception:
            database.rollback()
            logger.exception("No se pudo procesar el correo %s", path.name)
            if failures is not None:
                failures.append(path.name)
            continue
        done.mkdir(exist_ok=True)
        shutil.move(str(path), str(done / path.name))
    return results


def poll_imap(database: Session, *, limit: int = 25, failures: list[str] | None = None) -> list[dict[str, Any]]:
    if not imap_configured():
        return []
    connection_class = imaplib.IMAP4_SSL if settings.imap_use_ssl else imaplib.IMAP4
    results = []
    with connection_class(settings.imap_host, settings.imap_port) as connection:
        connection.login(settings.imap_user, settings.imap_password)
        connection.select(settings.imap_folder)
        _status, data = connection.uid("search", None, "UNSEEN")
        uids = (data[0] or b"").split()[:limit]
        for uid in uids:
            _status, fetched = connection.uid("fetch", uid, "(BODY.PEEK[])")
            raw = next((item[1] for item in fetched if isinstance(item, tuple)), None)
            if not raw:
                continue
            try:
                results.append(import_eml(database, raw, provider="imap"))
            except Exception:
                database.rollback()
                logger.exception("No se pudo procesar el correo IMAP %s", uid)
                if failures is not None:
                    failures.append(f"imap:{uid.decode() if isinstance(uid, bytes) else uid}")
                continue  # queda sin leer: se reintentará
            connection.uid("store", uid, "+FLAGS", "(\\Seen)")
    return results


def poll(database: Session) -> dict[str, Any]:
    """Lee el buzón común (carpeta e IMAP). En multiempresa, solo para el cliente al que está asignado."""
    from app.connectors.assignment import require_assigned

    require_assigned(database, "email")  # nunca en el cliente equivocado (lanza SourceNotAssigned)
    failures: list[str] = []
    folder = poll_folder(database, failures)
    imap = poll_imap(database, failures=failures)
    messages = folder + imap
    return {
        "messages": len(messages),
        "failed": len(failures),
        "documents": sum(len(item["attachments"]) for item in messages),
        "cases": sorted({attachment["case_code"] for item in messages for attachment in item["attachments"] if attachment["case_code"]}),
        "results": messages,
    }
