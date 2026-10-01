"""
Esquema de la base de datos con Alembic (PostgreSQL en producción, SQLite en local).

Al arrancar, `migrate()`:

    base vacía                        → alembic upgrade head
    base con alembic_version          → alembic upgrade head (aplica lo que falte)
    base anterior a Alembic           → se completa como antes (tablas y columnas que falten,
                                        datos de tablas sustituidas): queda como los modelos actuales,
                                        así que se marca en la última versión; desde ahí, normal

Para cambiar el esquema: modifica los modelos y genera la migración
(`alembic revision --autogenerate -m "…"` dentro de backend/), revísala y súbela.
`tests/integration/test_migrations.py` comprueba que las migraciones dejan exactamente
el esquema de los modelos, en SQLite y, si TEST_DATABASE_URL lo indica, en PostgreSQL.
"""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
logger = logging.getLogger(__name__)


def load_models() -> None:
    """Todos los modelos en Base.metadata (algunos viven fuera de app/models.py)."""
    from app import models  # noqa: F401
    from app import outlook_connector  # noqa: F401


def alembic_config(connection=None) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.attributes["skip_logging"] = True
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def migrate(engine: Engine | None = None) -> str:
    """Deja la base en la última versión. Devuelve qué hizo: «upgrade» o «legado→stamp»."""
    from app.database import Base
    from app.database import add_missing_columns
    from app.database import engine as default_engine
    from app.database import migrate_legacy_automation_settings

    engine = engine or default_engine
    load_models()
    with engine.connect() as connection:
        tables = set(inspect(connection).get_table_names())
    legacy = "alembic_version" not in tables and bool(tables & set(Base.metadata.tables))
    if legacy:
        # Base creada con create_all antes de Alembic: se completa hasta los modelos actuales y se marca en la última versión.
        Base.metadata.create_all(bind=engine)
        add_missing_columns()
        migrate_legacy_automation_settings()
        with engine.begin() as connection:
            command.stamp(alembic_config(connection), "head")
        logger.info("Base anterior a Alembic: completada y marcada en la última versión.")
    with engine.begin() as connection:
        command.upgrade(alembic_config(connection), "head")
    return "legado→stamp" if legacy else "upgrade"


def reset_database(engine: Engine | None = None) -> None:
    """Borra todo, incluida la marca de versión: la siguiente migración parte de cero (pruebas y evaluación)."""
    from sqlalchemy import text

    from app.database import Base
    from app.database import engine as default_engine

    engine = engine or default_engine
    load_models()
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as connection:
        for table in ("alembic_version", "automation_settings"):
            connection.execute(text(f'DROP TABLE IF EXISTS "{table}"'))
