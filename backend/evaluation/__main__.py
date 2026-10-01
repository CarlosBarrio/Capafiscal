"""
Uso (desde backend/):

    python -m evaluation                                   # réplicas sintéticas, reglas
    python -m evaluation --dataset reales                  # tus documentos (carpeta local, fuera de git)
    python -m evaluation --dataset reales --engines reglas,claude,hibrido   # necesita ANTHROPIC_API_KEY
    python -m evaluation --dataset reales --conjuntos B    # solo el conjunto de evaluación
    python -m evaluation --dataset reales --ciego          # incluye el conjunto ciego C (queda anotado)
    python -m evaluation --model claude-sonnet-5-5         # comparar otro modelo

    python -m evaluation preparar reales [--conjunto B] [--prerrellenar]   # etiquetas para documentos nuevos
    python -m evaluation incorporar reales                 # pasa a labels.json los casos revisados

Conjuntos: A = desarrollo (se puede mirar y ajustar reglas con ellos),
B = evaluación (no se usan para cambiar reglas), C = ciego (no se tocan
hasta el final; cada uso queda anotado en informes/ciego.log).

Deja el informe en evaluation/informes/ (Markdown y JSON).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from evaluation.core import ENGINES  # noqa: E402
from evaluation.core import draft_labels  # noqa: E402
from evaluation.core import load_dataset  # noqa: E402
from evaluation.core import merge_reviewed  # noqa: E402
from evaluation.core import run  # noqa: E402
from evaluation.core import to_markdown  # noqa: E402


def dataset_folder(name: str) -> Path:
    folder = Path(name)
    return folder if folder.exists() else HERE / "datasets" / name


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=HERE, check=False).stdout.strip() or "?"
    except OSError:
        return "?"


def prepare(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation preparar")
    parser.add_argument("dataset")
    parser.add_argument("--conjunto", default="B", choices=["A", "B", "C"])
    parser.add_argument("--prerrellenar", action="store_true", help="Rellena con lo que leen las reglas (sesga la etiqueta: revisa cada valor)")
    args = parser.parse_args(argv)
    folder = dataset_folder(args.dataset)
    folder.mkdir(parents=True, exist_ok=True)
    out = draft_labels(folder, prefill=args.prerrellenar, set_name=args.conjunto)
    count = len(json.loads(out.read_text(encoding="utf-8"))["casos"])
    print(f"{count} documento(s) sin etiquetar → {out}")
    print("Rellena cada «expected» mirando el documento, marca «revisado»: true y ejecuta: python -m evaluation incorporar " + args.dataset)
    if args.prerrellenar:
        print("Aviso: los valores vienen de las reglas. Si no los revisas uno a uno, la evaluación medirá a las reglas contra sí mismas.")
    return 0


def incorporate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation incorporar")
    parser.add_argument("dataset")
    args = parser.parse_args(argv)
    moved, left = merge_reviewed(dataset_folder(args.dataset))
    print(f"{moved} caso(s) incorporados a labels.json; {left} siguen pendientes de revisar.")
    return 0


def evaluate(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Evalúa la lectura de facturas: reglas, Claude e híbrido.")
    parser.add_argument("--dataset", default="sinteticas", help="Carpeta dentro de evaluation/datasets o ruta")
    parser.add_argument("--engines", default="reglas", help=f"Motores separados por comas: {', '.join(ENGINES)}")
    parser.add_argument("--conjuntos", default="A,B", help="Conjuntos a evaluar (A desarrollo, B evaluación)")
    parser.add_argument("--ciego", action="store_true", help="Incluye el conjunto ciego C (queda anotado)")
    parser.add_argument("--model", default=None, help="Modelo de Claude (por defecto el de AGENT_MODEL)")
    args = parser.parse_args(argv)

    folder = dataset_folder(args.dataset)
    if not (folder / "labels.json").exists():
        print(f"No encuentro {folder / 'labels.json'}")
        return 1
    engines = [item.strip() for item in args.engines.split(",") if item.strip()]
    unknown = [item for item in engines if item not in ENGINES]
    if unknown:
        print(f"Motores desconocidos: {', '.join(unknown)}")
        return 1
    sets = {item.strip().upper() for item in args.conjuntos.split(",") if item.strip()}
    if "C" in sets and not args.ciego:
        print("El conjunto ciego C solo se evalúa con --ciego (y queda anotado).")
        return 1
    if args.ciego:
        sets.add("C")

    dataset = load_dataset(folder)
    report = run(dataset, engines, model=args.model, sets=sets)
    markdown = to_markdown(report)
    out = HERE / "informes"
    out.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (out / f"{dataset.name}-{stamp}.md").write_text(markdown, encoding="utf-8")
    (out / f"{dataset.name}-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    if args.ciego:
        log = out / "ciego.log"
        previous = log.read_text(encoding="utf-8").count("\n") if log.exists() else 0
        summary = ", ".join(f"{engine} {data['field_accuracy']:.0%}" for engine, data in report["engines"].items() if data["field_accuracy"] is not None)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} · {dataset.name} · commit {git_commit()} · {summary}\n")
        if previous:
            print(f"Aviso: el conjunto ciego ya se había usado {previous} vez/veces. Si has cambiado reglas entre medias, ya no es ciego.")

    print(markdown)
    print(f"Informe guardado en {out}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "preparar":
        return prepare(argv[1:])
    if argv and argv[0] == "incorporar":
        return incorporate(argv[1:])
    return evaluate(argv)


if __name__ == "__main__":
    raise SystemExit(main())
