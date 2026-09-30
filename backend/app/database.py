from collections.abc import Generator

import logging

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


SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    database = SessionLocal()

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

                if not column.nullable:
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

                connection.execute(
                    text(
                        f'ALTER TABLE "{table.name}" '
                        f'ADD COLUMN "{column.name}" {column_type}'
                    )
                )
                added.append(f"{table.name}.{column.name}")

    if added:
        logger.info("Columnas añadidas: %s", ", ".join(added))

    return added