"""Las migraciones dejan exactamente el esquema de los modelos, y una base anterior a Alembic se adopta sin perder datos.

Se ejecuta contra la base de las pruebas: SQLite por defecto y PostgreSQL con TEST_DATABASE_URL.
"""
from __future__ import annotations

from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy import text

from tests.conftest import drop_everything


def schema_differences():
    from app.database import Base
    from app.database import engine
    from app.migrate import load_models

    load_models()
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        return compare_metadata(context, Base.metadata)


def current_revision():
    from app.database import engine

    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def head_revision():
    from alembic.script import ScriptDirectory

    from app.migrate import alembic_config

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def test_fresh_database_is_built_by_migrations_and_matches_models():
    from app.migrate import migrate

    drop_everything()
    assert migrate() == "upgrade"
    assert current_revision() == head_revision()
    assert schema_differences() == []
    assert migrate() == "upgrade"  # idempotente


def test_database_created_before_alembic_is_adopted_without_losing_data():
    from app.database import Base
    from app.database import engine
    from app.migrate import load_models
    from app.migrate import migrate

    drop_everything()
    load_models()
    Base.metadata.create_all(bind=engine)  # como se creaban antes las bases
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE automation_settings (code VARCHAR(40) PRIMARY KEY, enabled BOOLEAN NOT NULL, '
                                'last_run_at TIMESTAMP, last_status VARCHAR(20), last_summary TEXT)'))
        connection.execute(text("INSERT INTO automation_settings (code, enabled) VALUES ('daily_summary', false)"))
    from datetime import date

    from app.database import SessionLocal
    from tests.workflows.test_scenarios import add_invoice

    with SessionLocal() as database:
        add_invoice(database, supplier="ANTIGUA S.L.", tax_id="B00000017", number="A-1", total=100, when=date(2026, 9, 1))
        database.commit()
    assert "alembic_version" not in inspect(engine).get_table_names()

    assert migrate() == "legado→stamp"
    assert current_revision() == head_revision()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT supplier_name FROM invoices")).scalar() == "ANTIGUA S.L."
        moved = connection.execute(text("SELECT tenant_id, enabled FROM automation_client_settings WHERE code = 'daily_summary'")).one()
    assert moved.tenant_id == 0 and not moved.enabled
