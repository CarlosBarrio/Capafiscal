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

    python -m evaluation generar-b                         # banco B de expedientes sintéticos (se versiona)
    python -m evaluation generar-c --semilla N             # banco C ciego (fuera de git; no se mira)
    python -m evaluation casos --dataset b_sintetico       # evalúa expedientes completos (reglas)
    python -m evaluation casos --dataset c_ciego --ciego   # una sola vez, al final (queda anotado)
    python -m evaluation comparar --dataset b_sintetico    # reglas vs Claude vs híbrido (ANTHROPIC_API_KEY)
    python -m evaluation politica --informe evaluation/informes/comparar_b_sintetico_<fecha>.json   # routing de Claude

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


def append_history(path: Path, report: dict, dataset: str, sets: list[str]) -> None:
    """Una línea por motor y ejecución: así se ve cómo evoluciona el sistema."""
    import csv

    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(["fecha", "commit", "dataset", "conjuntos", "motor", "documentos", "acierto_campos", "perfectos", "solo_reglas", "con_ia", "humano", "error_silencioso", "coste_usd", "errores_conocidos_pendientes"])
        for engine, data in report["engines"].items():
            if not data["available"]:
                continue
            pending = sum(1 for row in data["rows"] for item in row.get("known_errors") or [] if not row["checks"].get(item["campo"]))
            outcomes = data.get("outcomes") or {}
            writer.writerow([
                datetime.now().isoformat(timespec="seconds"), git_commit(), dataset, "+".join(sets), engine, data["docs"],
                data["field_accuracy"], data["perfect"], outcomes.get("solo_reglas", ""), outcomes.get("con_ia", ""),
                outcomes.get("humano", ""), outcomes.get("error_silencioso", ""), round(data["cost_per_doc_usd"] * data["docs"], 4), pending,
            ])


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

    append_history(out / "historial.csv", report, dataset.name, sorted(sets))

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


def generate(argv: list[str], which: str) -> int:
    from evaluation.banco_casos import build_b
    from evaluation.banco_casos import build_c

    parser = argparse.ArgumentParser(prog=f"python -m evaluation generar-{which}")
    if which == "c":
        parser.add_argument("--semilla", type=int, required=True, help="Elígela tú y no la compartas: decide las variantes de C")
    args = parser.parse_args(argv)
    index = build_b() if which == "b" else build_c(args.semilla)
    print(f"Banco {index['conjunto']}: {index['total_casos']} expedientes, {index['total_documentos']} documentos.")
    if which == "c":
        print(f"Sello: {index['sello']}. No abras los documentos ni ejecutes C hasta la evaluación final.")
    return 0


def cases(argv: list[str]) -> int:
    from evaluation import casos

    parser = argparse.ArgumentParser(prog="python -m evaluation casos")
    parser.add_argument("--dataset", default="b_sintetico")
    parser.add_argument("--motor", default="reglas", choices=["reglas", "hibrido"], help="«hibrido» usa Claude si hay ANTHROPIC_API_KEY")
    parser.add_argument("--ciego", action="store_true")
    args = parser.parse_args(argv)
    report = casos.run(args.dataset, engine=args.motor, blind=args.ciego)
    json_path, md_path = casos.save(report)
    summary = report["summary"]
    out = HERE / "informes"
    import csv

    history = out / "historial_casos.csv"
    new = not history.exists()
    with history.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if new:
            writer.writerow(["fecha", "commit", "banco", "motor", "expedientes", "expedientes_ok", "comprobaciones", "comprobaciones_ok", "error_silencioso"])
        writer.writerow([datetime.now().isoformat(timespec="seconds"), git_commit(), args.dataset, args.motor, summary["cases"], summary["cases_ok"], summary["checks"],
                         summary["checks_ok"], summary["outcomes"].get("error_silencioso", 0)])
    if args.ciego:
        with (out / "ciego.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} · casos {args.dataset} · commit {git_commit()} · {summary['checks_ok']}/{summary['checks']}\n")
    print(md_path.read_text(encoding="utf-8"))
    print(f"Informe: {md_path} · datos: {json_path}")
    return 0


def compare(argv: list[str]) -> int:
    from evaluation import comparar

    parser = argparse.ArgumentParser(prog="python -m evaluation comparar")
    parser.add_argument("--dataset", default="b_sintetico")
    parser.add_argument("--motores", default="reglas,claude,hibrido", help="reglas, claude (siempre) e hibrido (el producto)")
    parser.add_argument("--ciego", action="store_true")
    args = parser.parse_args(argv)
    engines = [item.strip() for item in args.motores.split(",") if item.strip()]
    unknown = [item for item in engines if item not in comparar.ENGINES]
    if unknown:
        print(f"Motores desconocidos: {', '.join(unknown)}")
        return 1
    report = comparar.run(args.dataset, engines=engines, blind=args.ciego)
    path = comparar.save(report)
    if args.ciego:
        out = HERE / "informes"
        with (out / "ciego.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now().isoformat(timespec='seconds')} · comparar {args.dataset} · commit {git_commit()} · {', '.join(engines)}\n")
    print(path.read_text(encoding="utf-8"))
    print(f"Informe: {path}")
    return 0


def policy(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m evaluation politica")
    parser.add_argument("--informe", required=True, help="JSON de `python -m evaluation comparar`")
    parser.add_argument("--motor", default="hibrido", choices=["hibrido", "claude"])
    parser.add_argument("--con-correcciones", action="store_true", help="Suma las correcciones humanas a campos que puso Claude (base de datos actual)")
    args = parser.parse_args(argv)
    from app.routing import claude_corrections
    from app.routing import derive_policy
    from app.routing import save_policy

    corrections = None
    if args.con_correcciones:
        from app.database import SessionLocal

        with SessionLocal() as database:
            corrections = claude_corrections(database)
    result = derive_policy(json.loads(Path(args.informe).read_text(encoding="utf-8")), engine=args.motor, corrections=corrections)
    path = save_policy(result)
    for key, item in sorted(result["reasons"].items()):
        print(f"{'Claude' if item['use_claude'] else 'reglas → persona':>17} · {key}: {item['why']}")
    print(f"Política guardada en {path}")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "politica":
        return policy(argv[1:])
    if argv and argv[0] == "comparar":
        return compare(argv[1:])
    if argv and argv[0] in {"generar-b", "generar-c"}:
        return generate(argv[1:], argv[0][-1])
    if argv and argv[0] == "casos":
        return cases(argv[1:])
    if argv and argv[0] == "preparar":
        return prepare(argv[1:])
    if argv and argv[0] == "incorporar":
        return incorporate(argv[1:])
    return evaluate(argv)


if __name__ == "__main__":
    raise SystemExit(main())
