"""
Copias de seguridad y recuperación.

Una copia es UN archivo .zip con todo lo necesario para volver a tener CapaFiscal como estaba:

    manifest.json        versión del esquema (Alembic), fecha, filas por tabla y huella SHA-256 de cada fichero
    tables/<tabla>.jsonl todas las filas de todas las tablas (todos los clientes), en formato portable:
                         sirve igual para SQLite que para PostgreSQL (y para pasar de uno a otro)
    files/uploads/…      documentos subidos
    files/data/…         adjuntos de expedientes, buzón, etc.

Si BACKUP_PASSPHRASE está definida, el archivo se cifra (Fernet, clave derivada con PBKDF2) y
termina en .zip.enc: sin la frase no se puede leer. Las copias contienen datos fiscales y
personales: guárdalas fuera del servidor y cifradas.

    python -m app.backup create [carpeta]     crea la copia (por defecto en BACKUP_DIR)
    python -m app.backup verify <archivo>     comprueba huellas y que se puede leer
    python -m app.backup restore <archivo>    restaura en una base VACÍA (DATABASE_URL); nunca pisa datos

La restauración se ensaya en los tests (copia → base nueva → mismos datos).
"""
from __future__ import annotations

from app import clock
import base64
import hashlib
import io
import json
import os
import zipfile
from datetime import date
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import inspect
from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.engine import Engine

FORMAT = 1
MAGIC = b"CFBK1"
KEEP_DEFAULT = 14


class BackupError(RuntimeError):
    pass


def encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$d": value.isoformat()}
    if isinstance(value, Decimal):
        return {"$dec": str(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode()}
    return value


def decode(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        (key, raw), = value.items()
        if key == "$dt":
            return datetime.fromisoformat(raw)
        if key == "$d":
            return date.fromisoformat(raw)
        if key == "$dec":
            return Decimal(raw)
        if key == "$b":
            return base64.b64decode(raw)
    return value


def key_for(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return base64.urlsafe_b64encode(PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=390_000).derive(passphrase.encode()))


def seal(content: bytes, passphrase: str) -> bytes:
    from cryptography.fernet import Fernet

    salt = os.urandom(16)
    return MAGIC + salt + Fernet(key_for(passphrase, salt)).encrypt(content)


def unseal(content: bytes, passphrase: str | None) -> bytes:
    from cryptography.fernet import Fernet
    from cryptography.fernet import InvalidToken

    if not content.startswith(MAGIC):
        return content
    if not passphrase:
        raise BackupError("La copia está cifrada: define BACKUP_PASSPHRASE con la frase con la que se creó.")
    try:
        return Fernet(key_for(passphrase, content[5:21])).decrypt(content[21:])
    except InvalidToken as error:
        raise BackupError("La frase de la copia no es correcta (o el archivo está dañado).") from error


def tables_in_order() -> list:
    from app.database import Base
    from app.migrate import load_models

    load_models()
    return list(Base.metadata.sorted_tables)  # padres antes que hijos


def schema_version(engine: Engine) -> str | None:
    with engine.connect() as connection:
        if "alembic_version" not in inspect(connection).get_table_names():
            return None
        return connection.execute(text("SELECT version_num FROM alembic_version")).scalar()


def walk(folder: Path) -> list[Path]:
    return sorted(path for path in folder.rglob("*") if path.is_file()) if folder.exists() else []


def create(*, engine: Engine | None = None, folders: dict[str, Path] | None = None, passphrase: str | None = None) -> bytes:
    """Copia completa en memoria (bytes del .zip, cifrado si hay frase). Solo lee la base."""
    from app.config import settings
    from app.database import engine as default_engine

    engine = engine or default_engine
    folders = folders if folders is not None else {"uploads": Path(settings.upload_dir), "data": Path(settings.data_dir)}
    manifest: dict[str, Any] = {"format": FORMAT, "created_at": clock.now().isoformat(), "schema": schema_version(engine),
                                "tables": {}, "files": {}}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        with engine.connect() as connection:
            existing = set(inspect(connection).get_table_names())
            for table in tables_in_order():
                if table.name not in existing:
                    continue
                lines = [json.dumps({column: encode(value) for column, value in row._mapping.items()}, ensure_ascii=False)
                         for row in connection.execute(select(table).order_by(*table.primary_key.columns))]
                archive.writestr(f"tables/{table.name}.jsonl", "\n".join(lines))
                manifest["tables"][table.name] = len(lines)
        uploads_root = folders.get("uploads")
        for name, folder in folders.items():
            for path in walk(folder):
                if name == "data" and uploads_root and uploads_root in path.parents:
                    continue  # uploads dentro de data: no duplicar
                if path.suffix in (".db", ".db-wal", ".db-shm", ".sqlite"):
                    continue  # la base va en tables/, no como fichero (podría estar a medio escribir)
                relative = f"files/{name}/{path.relative_to(folder).as_posix()}"
                content = path.read_bytes()
                archive.writestr(relative, content)
                manifest["files"][relative] = hashlib.sha256(content).hexdigest()
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    content = buffer.getvalue()
    return seal(content, passphrase) if passphrase else content


def read(content: bytes, passphrase: str | None = None) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(unseal(content, passphrase)))
    except zipfile.BadZipFile as error:
        raise BackupError("El archivo no es una copia de CapaFiscal o está dañado.") from error


def verify(content: bytes, passphrase: str | None = None) -> dict[str, Any]:
    """Comprueba que la copia se puede restaurar: huellas de ficheros y filas legibles."""
    archive = read(content, passphrase)
    try:
        manifest = json.loads(archive.read("manifest.json"))
    except KeyError as error:
        raise BackupError("La copia no tiene manifest.json.") from error
    problems = []
    for name, digest in manifest["files"].items():
        if hashlib.sha256(archive.read(name)).hexdigest() != digest:
            problems.append(f"Huella distinta: {name}")
    for table, rows in manifest["tables"].items():
        body = archive.read(f"tables/{table}.jsonl").decode()
        if len([line for line in body.split("\n") if line]) != rows:
            problems.append(f"Filas distintas en {table}")
    return {"ok": not problems, "problems": problems, "schema": manifest["schema"], "created_at": manifest["created_at"],
            "tables": len(manifest["tables"]), "rows": sum(manifest["tables"].values()), "files": len(manifest["files"])}


def restore(content: bytes, *, engine: Engine | None = None, folders: dict[str, Path] | None = None, passphrase: str | None = None) -> dict[str, Any]:
    """Restaura en una base VACÍA (la crea con las migraciones). Nunca pisa datos existentes."""
    from app.config import settings
    from app.database import engine as default_engine
    from app.migrate import migrate

    engine = engine or default_engine
    folders = folders if folders is not None else {"uploads": Path(settings.upload_dir), "data": Path(settings.data_dir)}
    check = verify(content, passphrase)
    if not check["ok"]:
        raise BackupError("La copia no supera la verificación: " + "; ".join(check["problems"][:5]))
    archive = read(content, passphrase)
    manifest = json.loads(archive.read("manifest.json"))

    tables = tables_in_order()
    with engine.connect() as connection:
        existing = set(inspect(connection).get_table_names())
        for table in tables:
            if table.name in existing and connection.execute(select(table).limit(1)).first() is not None:
                raise BackupError(f"La base de destino no está vacía (tabla {table.name}). Restaura en una base nueva.")
    migrate(engine)
    if manifest["schema"] and schema_version(engine) != manifest["schema"]:
        raise BackupError(f"La copia es del esquema {manifest['schema']} y esta instalación está en {schema_version(engine)}: "
                          "restaura con la misma versión de CapaFiscal y actualiza después.")
    restored = 0
    with engine.begin() as connection:
        for table in tables:
            name = f"tables/{table.name}.jsonl"
            if name not in archive.namelist():
                continue
            rows = [{key: decode(value) for key, value in json.loads(line).items()} for line in archive.read(name).decode().split("\n") if line]
            columns = set(table.columns.keys())
            rows = [{key: value for key, value in row.items() if key in columns} for row in rows]
            if table.name == "alembic_version":
                continue
            for start in range(0, len(rows), 500):
                connection.execute(table.insert(), rows[start:start + 500])
            restored += len(rows)
        if engine.dialect.name == "postgresql":  # los contadores de id siguen después del último restaurado
            for table in tables:
                key = [column for column in table.primary_key.columns]
                if len(key) == 1 and key[0].autoincrement and str(key[0].type).startswith("INTEGER"):
                    connection.execute(text(f"SELECT setval(pg_get_serial_sequence('\"{table.name}\"', '{key[0].name}'), "
                                            f"COALESCE((SELECT MAX(\"{key[0].name}\") FROM \"{table.name}\"), 0) + 1, false)"))
    files = 0
    for name in archive.namelist():
        if not name.startswith("files/"):
            continue
        _, group, relative = name.split("/", 2)
        target = folders[group] / relative
        if target.exists():
            continue  # nunca se pisa un fichero existente
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(name))
        files += 1
    return {"rows": restored, "files": files, "schema": manifest["schema"], "created_at": manifest["created_at"]}


def write(target_dir: Path, content: bytes, *, encrypted: bool, keep: int = KEEP_DEFAULT) -> Path:
    """Guarda la copia con fecha y deja solo las `keep` más recientes."""
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = clock.now().strftime("%Y%m%d-%H%M%S")
    path = target_dir / f"capafiscal-{stamp}.zip{'.enc' if encrypted else ''}"
    path.write_bytes(content)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    copies = sorted(target_dir.glob("capafiscal-*.zip*"))
    for old in copies[:-keep] if keep > 0 else []:
        old.unlink()
    return path


def main(argv: list[str]) -> int:
    from app.config import settings

    passphrase = os.environ.get("BACKUP_PASSPHRASE") or None
    if len(argv) < 1 or argv[0] not in ("create", "verify", "restore"):
        print(__doc__)
        return 2
    try:
        if argv[0] == "create":
            folder = Path(argv[1]) if len(argv) > 1 else Path(os.environ.get("BACKUP_DIR") or Path(settings.data_dir) / "copias")
            path = write(folder, create(passphrase=passphrase), encrypted=bool(passphrase), keep=int(os.environ.get("BACKUP_KEEP", KEEP_DEFAULT)))
            result = verify(path.read_bytes(), passphrase)
            print(f"✓ Copia creada y verificada: {path} ({result['rows']} filas, {result['files']} ficheros, esquema {result['schema']})"
                  + ("" if passphrase else "\n! Sin cifrar: define BACKUP_PASSPHRASE para cifrar las copias."))
        elif argv[0] == "verify":
            result = verify(Path(argv[1]).read_bytes(), passphrase)
            print(("✓ Copia correcta" if result["ok"] else "✕ Copia con problemas") + f": {result['rows']} filas, {result['files']} ficheros, "
                  f"esquema {result['schema']}, creada {result['created_at']}")
            for problem in result["problems"]:
                print(f"  - {problem}")
            return 0 if result["ok"] else 1
        else:
            result = restore(Path(argv[1]).read_bytes(), passphrase=passphrase)
            print(f"✓ Restaurada: {result['rows']} filas y {result['files']} ficheros (copia del {result['created_at']}).")
    except BackupError as error:
        print(f"✕ {error}")
        return 1
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
