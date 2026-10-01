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
from pathlib import Path
from typing import Any
from typing import Protocol

from sqlalchemy.orm import Session

from app.connectors.dehu.adapter import DehuItem
from app.connectors.dehu.adapter import ingest_dehu


class DehuTransport(Protocol):
    def pending(self) -> list[DehuItem]: ...


class FolderTransport:
    """Carpeta con pares <id>.json (metadatos) + <id>.pdf (opcional)."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def pending(self) -> list[DehuItem]:
        items = []
        for meta in sorted(self.folder.glob("*.json")):
            data: dict[str, Any] = json.loads(meta.read_text(encoding="utf-8"))
            item = DehuItem.from_payload(data)
            pdf = meta.with_suffix(".pdf")
            if pdf.exists() and item.pdf is None:
                item.pdf, item.filename = pdf.read_bytes(), pdf.name
            items.append(item)
        return items


def poll(database: Session, transport: DehuTransport) -> dict[str, Any]:
    """Procesa lo pendiente. Idempotente: lo ya entregado se cuenta como duplicado."""
    results = [ingest_dehu(database, item) for item in transport.pending()]
    return {
        "received": len(results),
        "new": sum(1 for item in results if not item["duplicate"]),
        "duplicates": sum(1 for item in results if item["duplicate"]),
        "cases": [item["case_code"] for item in results if item["case_code"]],
        "items": results,
    }
