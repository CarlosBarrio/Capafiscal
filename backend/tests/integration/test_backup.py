"""Copias de seguridad: crear (cifrada), verificar, restaurar en una base vacía y obtener los mismos datos.

Ensayo de recuperación: si este test pasa, una copia de producción se puede restaurar. Datos sintéticos.
"""
from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy import func
from sqlalchemy import select

from tests.agents.test_closing import setup_month

PASSPHRASE = "frase-de-prueba-larga"


def counts(engine):
    from app.backup import tables_in_order

    with engine.connect() as connection:
        return {table.name: connection.execute(select(func.count()).select_from(table)).scalar() for table in tables_in_order()}


@pytest.fixture()
def seeded(client, tmp_path):
    from app.config import settings

    ids = setup_month(client)
    client.post(f"/api/close/2026-09/run")
    attachment = settings.data_dir / "expedientes" / "prueba.txt"
    attachment.parent.mkdir(parents=True, exist_ok=True)
    attachment.write_text("Adjunto de prueba (SIMULACIÓN — NO OFICIAL)", encoding="utf-8")
    (settings.upload_dir / "factura_prueba.pdf").write_bytes(b"%PDF-1.4 simulado")
    return ids


def test_backup_roundtrip_into_an_empty_database(seeded, tmp_path):
    from app.backup import create
    from app.backup import restore
    from app.backup import verify
    from app.database import engine
    from app.models import Invoice

    content = create(passphrase=PASSPHRASE)
    assert content.startswith(b"CFBK1") and b"LS-0903" not in content  # cifrada: no se lee nada sin la frase
    check = verify(content, PASSPHRASE)
    assert check["ok"] and check["rows"] > 10 and check["files"] >= 2 and check["schema"]

    target = create_engine(f"sqlite:///{tmp_path / 'restaurada.db'}")
    folders = {"uploads": tmp_path / "u", "data": tmp_path / "d"}
    result = restore(content, engine=target, folders=folders, passphrase=PASSPHRASE)
    assert result["rows"] == check["rows"]  # la marca de versión no viaja: la pone la migración
    assert {name: rows for name, rows in counts(target).items()} == {name: rows for name, rows in counts(engine).items()}
    with target.connect() as connection:
        numbers = set(connection.execute(select(Invoice.__table__.c.invoice_number)).scalars())
    assert {"FAC-0901", "T-0902", "LS-0903"} <= numbers
    assert (folders["uploads"] / "factura_prueba.pdf").read_bytes() == b"%PDF-1.4 simulado"
    assert "SIMULACIÓN" in (folders["data"] / "expedientes" / "prueba.txt").read_text(encoding="utf-8")

    # Restaurar encima de datos: nunca.
    from app.backup import BackupError

    with pytest.raises(BackupError, match="no está vacía"):
        restore(content, engine=target, folders=folders, passphrase=PASSPHRASE)


def test_tampered_or_wrong_passphrase_is_refused(seeded):
    from app.backup import BackupError
    from app.backup import create
    from app.backup import verify

    with pytest.raises(BackupError, match="frase"):
        verify(create(passphrase=PASSPHRASE), "otra-frase")
    plain = create()
    source = zipfile.ZipFile(io.BytesIO(plain))
    tampered = io.BytesIO()
    with zipfile.ZipFile(tampered, "w") as archive:
        for name in source.namelist():
            data = source.read(name)
            archive.writestr(name, data + b"x" if name.startswith("files/uploads/") else data)
    result = verify(tampered.getvalue())
    assert not result["ok"] and "Huella distinta" in result["problems"][0]


def test_cli_keeps_only_the_latest_copies(seeded, tmp_path, monkeypatch, capsys):
    from app.backup import main

    monkeypatch.setenv("BACKUP_PASSPHRASE", PASSPHRASE)
    monkeypatch.setenv("BACKUP_KEEP", "2")
    from datetime import timedelta

    from app import clock

    start = clock.now()
    for hour in range(3):  # el nombre lleva la hora: se adelanta el reloj en vez de esperar
        with clock.frozen(start + timedelta(hours=hour)):
            assert main(["create", str(tmp_path / "copias")]) == 0
    copies = sorted((tmp_path / "copias").glob("capafiscal-*.zip.enc"))
    assert len(copies) == 2 and "✓ Copia creada y verificada" in capsys.readouterr().out
    assert main(["verify", str(copies[-1])]) == 0
