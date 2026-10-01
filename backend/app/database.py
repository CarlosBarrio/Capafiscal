from collections.abc import Generator

import logging

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


engine_options: dict = {
    "pool_pre_ping": True,
}

if settings.database_url.startswith("sqlite"):
    engine_options["connect_args"] = {
        "check_same_thread": False,
    }


engine = create_engine(
    settings.database_url,
    **engine_options,
)


if settings.database_url.startswith("sqlite"):
    from sqlalchemy import event

    # SQLite con varias peticiones a la vez: el driver pysqlite gestiona las
    # transacciones a su manera y, con SAVEPOINT y escrituras simultáneas,
    # puede deshacer trabajo en silencio. Receta de SQLAlchemy: que el BEGIN
    # lo emita SQLAlchemy, modo WAL (las lecturas no bloquean a las
    # escrituras) y espera ante bloqueos en lugar de fallar. No se usa
    # BEGIN IMMEDIATE: bloquearía a quien lee y abre otra sesión (p. ej. el
    # sincronizador de Outlook, que sube adjuntos por la API interna).
    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_connection, _record):
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 30000")
        if ":memory:" not in settings.database_url:
            cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _sqlite_begin(connection):
        # Las transacciones que van a escribir seguro (la entrada común) piden
        # el turno de escritura al empezar: esperan en vez de chocar.
        immediate = connection.get_execution_options().get("sqlite_immediate")
        connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")


SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db(request: Request) -> Generator[Session, None, None]:
    """Sesión de la petición, ya ligada al cliente que eligió el usuario (multiempresa)."""
    database = SessionLocal()
    database.info["tenant_id"] = getattr(request.state, "tenant_id", None)

    try:
        yield database
    finally:
        database.close()


def create_database_tables() -> None:
    """
    Creación inicial para desarrollo.

    Más adelante Alembic será el responsable de crear y modificar
    las tablas. Se mantiene esta función para poder arrancar el
    proyecto local sin ejecutar todavía una migración.
    """
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    add_missing_columns()
    migrate_legacy_automation_settings()


def migrate_legacy_automation_settings() -> int:
    """La tabla antigua «automation_settings» no tenía cliente: sus filas pasan al cliente 0 una sola vez."""
    with engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        if "automation_settings" not in tables:
            return 0
        if connection.execute(text('SELECT COUNT(*) FROM "automation_client_settings"')).scalar():
            return 0
        rows = connection.execute(text('SELECT code, enabled, last_run_at, last_status, last_summary FROM "automation_settings"')).mappings().all()
        for row in rows:
            connection.execute(text(
                'INSERT INTO "automation_client_settings" (tenant_id, code, enabled, last_run_at, last_status, last_summary) '
                "VALUES (0, :code, :enabled, :last_run_at, :last_status, :last_summary)"
            ), dict(row))
        return len(rows)


logger = logging.getLogger(__name__)


def add_missing_columns() -> list[str]:
    """
    Migración mínima para bases de datos ya creadas: añade las columnas
    nuevas (siempre opcionales) que falten en tablas existentes.
    create_all() crea tablas nuevas, pero nunca modifica las existentes.
    """
    added: list[str] = []

    # Inspección y ALTER en la misma conexión para ver un esquema coherente.
    with engine.begin() as connection:
        inspector = inspect(connection)
        existing_tables = set(inspector.get_table_names())

        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue

            existing_columns = {
                column["name"]
                for column in inspector.get_columns(table.name)
            }

            for column in table.columns:
                if column.name in existing_columns:
                    continue

                if not column.nullable and column.server_default is None:
                    logger.warning(
                        "No se puede añadir automáticamente la columna "
                        "obligatoria %s.%s.",
                        table.name,
                        column.name,
                    )
                    continue

                column_type = column.type.compile(
                    dialect=engine.dialect
                )

                default = ""
                if column.server_default is not None:
                    # p. ej. tenant_id NOT NULL DEFAULT 0: los datos existentes quedan en la empresa única.
                    default = f" NOT NULL DEFAULT {column.server_default.arg}"

                connection.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {column_type}{default}'
                    )
                )
                added.append(f"{table.name}.{column.name}")

    if added:
        logger.info("Columnas añadidas: %s", ", ".join(added))

    return added