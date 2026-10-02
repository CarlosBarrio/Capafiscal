"""
Configuración común: cada sesión de tests usa una carpeta temporal
propia (base de datos SQLite, subidas y datos) para no tocar nunca los
datos reales. Las variables se fijan antes de importar la aplicación.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_ROOT = Path(tempfile.mkdtemp(prefix="capafiscal-tests-"))

os.environ["DATA_DIR"] = str(TEST_ROOT / "data")
os.environ["UPLOAD_DIR"] = str(TEST_ROOT / "uploads")
# TEST_DATABASE_URL=postgresql+psycopg://… ejecuta la batería contra PostgreSQL.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{(TEST_ROOT / 'test.db').as_posix()}"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ.pop("COMPANY_TAX_ID", None)
os.environ.pop("COMPANY_TAX_IDS", None)
os.environ.pop("COMPANY_NAME", None)

sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.database import create_database_tables  # noqa: E402
from app.database import engine  # noqa: E402
from app.main import app  # noqa: E402
from gen_facturas import generate_all  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def sample_pdfs() -> dict[str, Path]:
    pdfs = generate_all(TEST_ROOT / "pdfs")
    return {pdf.stem: pdf for pdf in pdfs}


def drop_everything() -> None:
    """Cada prueba parte de cero: tablas, versión de Alembic y tablas antiguas."""
    from app.migrate import reset_database

    reset_database()


@pytest.fixture()
def client():
    from app.auth_routes import _login_failures

    _login_failures.clear()
    drop_everything()
    shutil.rmtree(settings.upload_dir, ignore_errors=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    create_database_tables()

    with TestClient(app) as test_client:
        yield test_client


def upload(client: TestClient, path: Path) -> dict:
    with path.open("rb") as handle:
        response = client.post(
            "/api/upload",
            files={"uploaded_file": (path.name, handle.read())},
        )

    assert response.status_code == 201, response.text
    return response.json()


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(TEST_ROOT, ignore_errors=True)
