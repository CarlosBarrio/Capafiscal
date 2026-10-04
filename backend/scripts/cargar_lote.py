"""
Carga en una base de datos NUEVA y aparte solo los documentos de una carpeta o de un .zip.

Al abrir CapaFiscal con esa base se ven únicamente esos documentos, leídos por el circuito normal
(subida → lectura → factura, albarán, presupuesto, nómina o notificación). Tu base de siempre no se toca.

    python scripts/cargar_lote.py RUTA [--empresa-nif B00000000 --empresa-nombre "Mi empresa S.L."] [--reemplazar]

RUTA es una carpeta (se recorre entera) o un .zip: PDF, texto, Word (.doc, .docx), .odt, RTF, HTML y XML. La base se crea en backend/data/solo-lote/ (fuera de git).
Sin --empresa-nif se usa la empresa de evaluation/datasets/reales/labels.json si existe: hace falta para saber
qué facturas son emitidas y cuáles recibidas. Al terminar dice cómo arrancar la aplicación con esa base.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
DESTINO = BACKEND / "data" / "solo-lote"
LABELS = BACKEND / "evaluation" / "datasets" / "reales" / "labels.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ruta", type=Path, help="Carpeta o .zip con los documentos")
    parser.add_argument("--empresa-nif", help="NIF de tu empresa (para distinguir emitidas y recibidas)")
    parser.add_argument("--empresa-nombre", help="Nombre de tu empresa")
    parser.add_argument("--destino", type=Path, default=DESTINO, help=f"Carpeta de la base nueva (por defecto {DESTINO})")
    parser.add_argument("--reemplazar", action="store_true", help="Borra antes la base del destino si ya existe")
    args = parser.parse_args()

    if not args.ruta.exists():
        print(f"No existe: {args.ruta}")
        return 1

    destino = args.destino.resolve()
    if destino.exists() and any(destino.iterdir()):
        if not args.reemplazar:
            print(f"Ya hay una base en {destino}. Usa --reemplazar para empezar de cero (solo borra esa carpeta).")
            return 1
        shutil.rmtree(destino)
    destino.mkdir(parents=True, exist_ok=True)

    empresa = {"tax_id": args.empresa_nif, "name": args.empresa_nombre}
    if not empresa["tax_id"] and LABELS.is_file():
        empresa = {**(json.loads(LABELS.read_text(encoding="utf-8")).get("empresa") or {}), **{k: v for k, v in empresa.items() if v}}

    # La configuración se fija antes de importar la aplicación: todo va a la base nueva, con todos los formatos
    # que CapaFiscal sabe leer aunque un .env antiguo diga solo «.pdf,.txt».
    os.environ.update({
        "ALLOWED_EXTENSIONS": ".pdf,.txt,.doc,.docx,.odt,.rtf,.htm,.html,.xml",
        "DATA_DIR": str(destino / "data"),
        "UPLOAD_DIR": str(destino / "uploads"),
        "DATABASE_URL": f"sqlite:///{(destino / 'capafiscal.db').as_posix()}",
        "ENABLE_SCHEDULER": "false",
    })
    sys.path.insert(0, str(BACKEND))

    from fastapi.testclient import TestClient

    from app.config import settings
    from app.database import create_database_tables
    from app.main import app

    allowed = {extension.strip().lower() for extension in settings.allowed_extensions.split(",") if extension.strip()}

    with tempfile.TemporaryDirectory(prefix="capafiscal-lote-") as temporary:
        carpeta = args.ruta
        if args.ruta.is_file() and args.ruta.suffix.lower() == ".zip":
            with zipfile.ZipFile(args.ruta) as archive:
                archive.extractall(temporary)
            carpeta = Path(temporary)

        archivos = sorted(path for path in carpeta.rglob("*") if path.is_file() and not path.name.startswith("."))
        cargables = [path for path in archivos if path.suffix.lower() in allowed]
        fuera = [path for path in archivos if path.suffix.lower() not in allowed]

        create_database_tables()
        resumen: dict[str, int] = {}
        errores: list[str] = []
        with TestClient(app) as client:
            if empresa.get("tax_id"):
                client.put("/api/company", json={key: value for key, value in empresa.items() if key in ("name", "tax_id") and value})
            else:
                print("Aviso: sin NIF de la empresa no se distingue bien qué facturas son emitidas.")
            for numero, path in enumerate(cargables, start=1):
                response = client.post("/api/upload", files={"uploaded_file": (path.name, path.read_bytes())})
                if response.status_code != 201:
                    errores.append(f"{path.name}: {response.json().get('detail')}")
                    continue
                document = response.json()["document"]
                tipo = document.get("kind") or ("FACTURA" if document.get("invoice") else "SIN IDENTIFICAR")
                resumen[tipo] = resumen.get(tipo, 0) + 1
                print(f"  {numero}/{len(cargables)} {path.name} → {tipo.lower()}")

    print(f"\nCargados {sum(resumen.values())} de {len(archivos)} archivos en {destino}")
    for tipo, cantidad in sorted(resumen.items()):
        print(f"  {tipo.lower()}: {cantidad}")
    if fuera:
        print(f"Sin cargar (formato no admitido: {', '.join(sorted(allowed))}): {len(fuera)}")
        for path in fuera:
            print(f"  {path.name}")
    for error in errores:
        print(f"Error: {error}")

    base = (destino / "capafiscal.db").as_posix()
    print("\nPara abrir CapaFiscal solo con estos documentos (desde la carpeta backend):")
    print("  PowerShell:")
    print(f'    $env:DATABASE_URL="sqlite:///{base}"; $env:UPLOAD_DIR="{(destino / "uploads").as_posix()}"; '
          f'$env:DATA_DIR="{(destino / "data").as_posix()}"; uvicorn app.main:app --reload')
    print("  bash:")
    print(f'    DATABASE_URL="sqlite:///{base}" UPLOAD_DIR="{(destino / "uploads").as_posix()}" '
          f'DATA_DIR="{(destino / "data").as_posix()}" uvicorn app.main:app --reload')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
