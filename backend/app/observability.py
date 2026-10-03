"""
Saber qué pasa en producción sin mirar la base de datos.

    registros   una línea por petición (método, ruta, estado, milisegundos, usuario, cliente, id de
                petición) y los avisos de seguridad (capafiscal.security). Nunca el contenido: ni
                parámetros de búsqueda, ni cuerpos, ni contraseñas, ni enlaces. LOG_FORMAT=json para
                un recolector (Loki, CloudWatch, Datadog…); LOG_LEVEL para el nivel.
    salud       GET /api/health, pública y sin datos de negocio: 200 si todo está bien, 503 si algo
                falla (base de datos, esquema, planificador, disco). Es lo que vigila el monitor
                externo (UptimeRobot, Better Stack, un healthcheck de Docker…).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
import uuid
from typing import Any

from app import clock

REQUEST_LOG = logging.getLogger("capafiscal.request")
MIN_FREE_BYTES = 1024 ** 3  # menos de 1 GB libre en la carpeta de datos: aviso
SCHEDULER_STALE_SECONDS = 5 * 60  # el planificador da una vuelta por minuto


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {"time": clock.now().isoformat(timespec="milliseconds"), "level": record.levelname,
                                 "logger": record.name, "message": record.getMessage()}
        entry.update(getattr(record, "fields", {}))
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Los registros de CapaFiscal a la salida estándar (donde los recoge Docker o systemd). Idempotente."""
    root = logging.getLogger("capafiscal")
    app_root = logging.getLogger("app")
    if getattr(root, "_configured", False):
        return
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get("LOG_FORMAT", "text").lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s · %(message)s"))
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    for logger in (root, app_root):
        logger.addHandler(handler)
        logger.setLevel(level)
    root._configured = True  # type: ignore[attr-defined]


async def log_requests(request, call_next):
    """Una línea por petición a la API. La ruta va sin parámetros (pueden llevar nombres o NIF buscados)."""
    path = request.url.path
    if not path.startswith("/api/"):
        return await call_next(request)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        user = getattr(request.state, "user", None)
        fields = {"request_id": request_id, "method": request.method, "path": path, "status": status,
                  "ms": round((time.perf_counter() - start) * 1000, 1), "user": getattr(user, "id", None),
                  "tenant": getattr(request.state, "tenant_id", None)}
        level = logging.ERROR if status >= 500 else logging.WARNING if status in (401, 403, 409, 429) else logging.INFO
        REQUEST_LOG.log(level, "%s %s → %s (%s ms)", request.method, path, status, fields["ms"], extra={"fields": fields})


def health() -> tuple[bool, dict[str, Any]]:
    """Comprobaciones para el monitor. Solo dice qué falla, nunca datos de negocio."""
    from sqlalchemy import text

    from app.automation_service import SCHEDULER
    from app.config import settings
    from app.database import engine

    checks: dict[str, dict[str, Any]] = {}
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as error:  # noqa: BLE001
        checks["database"] = {"ok": False, "detail": type(error).__name__}
    if checks["database"]["ok"]:
        try:
            from alembic.runtime.migration import MigrationContext
            from alembic.script import ScriptDirectory

            from app.migrate import alembic_config

            with engine.connect() as connection:
                current = MigrationContext.configure(connection).get_current_revision()
            head = ScriptDirectory.from_config(alembic_config()).get_current_head()
            checks["schema"] = {"ok": current == head, "version": current}
        except Exception as error:  # noqa: BLE001
            checks["schema"] = {"ok": False, "detail": type(error).__name__}
    if settings.enable_scheduler:
        tick = SCHEDULER.last_tick
        age = (clock.now() - tick).total_seconds() if tick else None
        # Recién arrancado aún no ha dado la primera vuelta: no es un fallo.
        checks["scheduler"] = {"ok": SCHEDULER.state()["running"] and (age is None or age < SCHEDULER_STALE_SECONDS),
                               "seconds_since_tick": round(age) if age is not None else None}
    try:
        free = shutil.disk_usage(settings.data_dir).free
        checks["disk"] = {"ok": free >= MIN_FREE_BYTES, "free_gb": round(free / 1024 ** 3, 1)}
    except OSError as error:
        checks["disk"] = {"ok": False, "detail": type(error).__name__}
    ok = all(check["ok"] for check in checks.values())
    return ok, checks
