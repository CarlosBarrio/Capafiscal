from collections.abc import Generator

from sqlalchemy import create_engine
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