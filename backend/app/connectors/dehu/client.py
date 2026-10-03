"""
Transporte DEHú: de dónde salen las notificaciones.

El adaptador (adapter.py) no sabe de dónde vienen. Un transporte solo tiene
que dar la lista de notificaciones pendientes (metadatos y, si se ha
accedido, el PDF):

    class DehuTransport(Protocol):
        def pending(self) -> list[DehuItem]: ...

Hoy existen dos:

    FolderTransport   una carpeta con lo descargado de la DEHú (PDF + .json
                      de metadatos con el mismo nombre). Sirve para empezar a
                      usarlo sin integración: la gestoría descarga y CapaFiscal
                      lo procesa.
    (en pruebas)      un transporte en memoria.

La conexión directa con el servicio de la DEHú necesita alta, certificado
electrónico de la empresa y, para una gestoría, el apoderamiento de cada
cliente. Cuando exista, será otro transporte con el mismo `pending()`: el
resto no cambia.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from typing import Protocol

from sqlalchemy.orm import Session

from app.connectors.dehu.adapter import DehuItem
from app.connectors.dehu.adapter import ingest_dehu

logger = logging.getLogger(__name__)


class DehuUnavailable(RuntimeError):
    """No se puede llegar al origen de la DEHú: no es lo mismo que «no hay notificaciones nuevas»."""


class DehuTransport(Protocol):
    def pending(self) -> list[DehuItem]: ...


class FolderTransport:
    """Carpeta con pares <id>.json (metadatos) + <id>.pdf (opcional)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.skipped: list[dict[str, str]] = []

    def pending(self) -> list[DehuItem]:
        """Un .json que no es una notificación (o está dañado) se salta y se avisa: no bloquea a los demás."""
        items = []
        self.skipped = []
        if not self.folder.is_dir():  # carpeta de red sin montar, ruta mal escrita, sin permiso…
            raise DehuUnavailable(f"No se puede leer la carpeta de la DEHú ({self.folder}): comprueba que existe y que CapaFiscal tiene acceso.")
        for meta in sorted(self.folder.glob("*.json")):
            try:
                data: dict[str, Any] = json.loads(meta.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not data.get("identifier"):
                    raise ValueError("no es una notificación de la DEHú (falta «identifier»)")
                item = DehuItem.from_payload(data)
            except (OSError, ValueError, TypeError) as error:  # json.JSONDecodeError es un ValueError
                logger.warning("DEHú: se salta %s: %s", meta.name, error)
                self.skipped.append({"file": meta.name, "reason": str(error)[:200]})
                continue
            pdf = meta.with_suffix(".pdf")
            if pdf.exists() and item.pdf is None:
                item.pdf, item.filename = pdf.read_bytes(), pdf.name
            items.append(item)
        return items


def poll(database: Session, transport: DehuTransport) -> dict[str, Any]:
    """Procesa lo pendiente. Idempotente: lo ya entregado se cuenta como duplicado."""
    results = [ingest_dehu(database, item) for item in transport.pending()]
    skipped = getattr(transport, "skipped", [])
    return {
        "skipped": skipped,
        "received": len(results),
        "new": sum(1 for item in results if not item["duplicate"]),
        "duplicates": sum(1 for item in results if item["duplicate"]),
        "cases": [item["case_code"] for item in results if item["case_code"]],
        "items": results,
    }
