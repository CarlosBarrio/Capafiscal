"""
Uso (desde backend/):

    python -m evaluation                          # dataset sintético, motor de reglas
    python -m evaluation --dataset reales         # tus facturas reales (carpeta local, fuera de git)
    python -m evaluation --engines reglas,claude,hibrido   # necesita ANTHROPIC_API_KEY
    python -m evaluation --model claude-sonnet-5-5         # comparar otro modelo

Deja el informe en evaluation/informes/ (Markdown y JSON).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from evaluation.core import ENGINES  # noqa: E402
from evaluation.core import load_dataset  # noqa: E402
from evaluation.core import run  # noqa: E402
from evaluation.core import to_markdown  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Evalúa la lectura de facturas: reglas, Claude e híbrido.")
    parser.add_argument("--dataset", default="sinteticas", help="Carpeta dentro de evaluation/datasets o ruta")
    parser.add_argument("--engines", default="reglas", help=f"Motores separados por comas: {', '.join(ENGINES)}")
    parser.add_argument("--model", default=None, help="Modelo de Claude (por defecto el de AGENT_MODEL)")
    args = parser.parse_args()

    folder = Path(args.dataset)
    if not folder.exists():
        folder = HERE / "datasets" / args.dataset
    if not (folder / "labels.json").exists():
        print(f"No encuentro {folder / 'labels.json'}")
        return 1
    engines = [item.strip() for item in args.engines.split(",") if item.strip()]
    unknown = [item for item in engines if item not in ENGINES]
    if unknown:
        print(f"Motores desconocidos: {', '.join(unknown)}")
        return 1

    dataset = load_dataset(folder)
    report = run(dataset, engines, model=args.model)
    markdown = to_markdown(report)
    out = HERE / "informes"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{dataset.name}-{stamp}.md").write_text(markdown, encoding="utf-8")
    (out / f"{dataset.name}-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(markdown)
    print(f"Informe guardado en {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
