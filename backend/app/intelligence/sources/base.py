"""Cómo se obtienen los datos de una fuente: por HTTP o desde archivos ya descargados."""
from __future__ import annotations

from pathlib import Path


class SourceUnavailable(RuntimeError):
    """La fuente no responde o no se puede alcanzar (red, permisos, caída)."""


class NotPublished(RuntimeError):
    """La fuente responde, pero ese día no hay publicación (domingos en el BOE, por ejemplo)."""


class HttpTransport:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

    def get(self, url: str, *, accept: str) -> bytes:
        import httpx

        try:
            response = httpx.get(url, headers={"Accept": accept, "User-Agent": "CapaFiscal/1.0 (+reutilizacion datos abiertos)"},
                                 timeout=self.timeout, follow_redirects=True)
        except httpx.HTTPError as error:
            raise SourceUnavailable(f"No se pudo conectar con {url.split('/')[2]}: {error.__class__.__name__}") from error
        if response.status_code == 404:
            raise NotPublished(url)
        if response.status_code >= 400:
            raise SourceUnavailable(f"{url.split('/')[2]} respondió {response.status_code}")
        return response.content


class FolderTransport:
    """Lee lo que alguien descargó a mano (o los datos de prueba): <carpeta>/<nombre>."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def get(self, url: str, *, accept: str) -> bytes:
        name = url.rstrip("/").split("/")[-1].split("=")[-1]
        for candidate in (name, f"{name}.json", f"{name}.xml"):
            path = self.folder / candidate
            if path.exists():
                return path.read_bytes()
        raise NotPublished(url)
