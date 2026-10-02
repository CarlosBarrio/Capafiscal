from app import clock
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


# ---------------------------------------------------------------------------
# Rutas absolutas
# ---------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
BACKEND_DIR = APP_DIR.parent

DATA_DIR = BACKEND_DIR / "data"
UPLOAD_DIR = BACKEND_DIR / "uploads"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DOCUMENTS_FILE = DATA_DIR / "documents.json"
ACTIVITY_FILE = DATA_DIR / "activity.json"
AUDIT_FILE = DATA_DIR / "audit_log.json"

_LOCK = RLock()


# ---------------------------------------------------------------------------
# Inicialización
# ---------------------------------------------------------------------------

def _ensure_json_file(path: Path, default: Any) -> None:
    if not path.exists():
        _atomic_write_json(path, default)


def _atomic_write_json(path: Path, data: Any) -> None:
    """
    Escribe primero en un archivo temporal y después lo reemplaza.

    Evita dejar un JSON incompleto si el proceso se interrumpe durante
    la escritura.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        prefix=f"{path.stem}_",
        suffix=".tmp",
        dir=str(path.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            json.dump(
                data,
                temporary_file,
                ensure_ascii=False,
                indent=2,
            )
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _load_json(path: Path, default: Any) -> Any:
    _ensure_json_file(path, default)

    with _LOCK:
        try:
            with path.open("r", encoding="utf-8") as file:
                return json.load(file)
        except (json.JSONDecodeError, OSError):
            backup = path.with_suffix(
                path.suffix + f".corrupt-{datetime.now():%Y%m%d-%H%M%S}"
            )

            try:
                path.replace(backup)
            except OSError:
                pass

            _atomic_write_json(path, default)
            return default


_ensure_json_file(DOCUMENTS_FILE, [])
_ensure_json_file(ACTIVITY_FILE, [])
_ensure_json_file(AUDIT_FILE, [])


# ---------------------------------------------------------------------------
# Documentos
# ---------------------------------------------------------------------------

def load_documents() -> list[dict]:
    data = _load_json(DOCUMENTS_FILE, [])
    return data if isinstance(data, list) else []


def save_documents(documents: list[dict]) -> None:
    if not isinstance(documents, list):
        raise TypeError("documents debe ser una lista")

    with _LOCK:
        _atomic_write_json(DOCUMENTS_FILE, documents)


def next_document_id(documents: list[dict] | None = None) -> int:
    """
    Mantiene IDs enteros para no romper el frontend actual, pero evita
    reutilizar len(documentos) + 1.
    """
    documents = documents if documents is not None else load_documents()

    valid_ids = [
        document.get("id")
        for document in documents
        if isinstance(document.get("id"), int)
    ]

    return max(valid_ids, default=0) + 1


def find_document(document_id: int) -> dict | None:
    return next(
        (
            document
            for document in load_documents()
            if document.get("id") == document_id
        ),
        None,
    )


# ---------------------------------------------------------------------------
# Archivos
# ---------------------------------------------------------------------------

def calculate_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def find_document_by_hash(file_hash: str) -> dict | None:
    if not file_hash:
        return None

    return next(
        (
            document
            for document in load_documents()
            if document.get("file_hash") == file_hash
        ),
        None,
    )


def safe_upload_name(original_name: str) -> str:
    """
    Elimina rutas incluidas en el nombre recibido y conserva únicamente
    el nombre final del archivo.
    """
    cleaned = Path(original_name or "documento").name.strip()

    if not cleaned:
        cleaned = "documento"

    return cleaned.replace("\x00", "")


# ---------------------------------------------------------------------------
# Actividad
# ---------------------------------------------------------------------------

def load_activity() -> list[dict]:
    data = _load_json(ACTIVITY_FILE, [])
    return data if isinstance(data, list) else []


def save_activity(activity: list[dict]) -> None:
    if not isinstance(activity, list):
        raise TypeError("activity debe ser una lista")

    with _LOCK:
        _atomic_write_json(ACTIVITY_FILE, activity)


def add_activity(time_label: str, message: str) -> None:
    activity = load_activity()

    activity.insert(
        0,
        {
            "time": time_label,
            "message": message,
            "created_at": clock.now().isoformat(),
        },
    )

    save_activity(activity[:100])


# ---------------------------------------------------------------------------
# Auditoría
# ---------------------------------------------------------------------------

def load_audit_log() -> list[dict]:
    data = _load_json(AUDIT_FILE, [])
    return data if isinstance(data, list) else []


def add_audit_event(
    action: str,
    entity_type: str,
    entity_id: int | str,
    actor: str = "system",
    metadata: dict | None = None,
) -> None:
    events = load_audit_log()

    events.insert(
        0,
        {
            "created_at": clock.now().isoformat(),
            "actor": actor,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "metadata": metadata or {},
        },
    )

    with _LOCK:
        _atomic_write_json(AUDIT_FILE, events[:5000])


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def reset_documents() -> None:
    save_documents([])
    save_activity([])

    add_audit_event(
        action="documents.reset",
        entity_type="document_collection",
        entity_id="all",
        actor="user",
    )