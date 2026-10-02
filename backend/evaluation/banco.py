"""
Evaluación de la conciliación bancaria de punta a punta.

    python -m evaluation banco                          # dataset banco_sintetico
    python -m evaluation banco --dataset <carpeta>      # otra carpeta con el mismo formato (extractos + labels.json)

Recorre el mismo camino que producción: movimientos (extracto CSV) → normalización (POST /api/bank/import, que
detecta columnas, signos y formatos) → conciliación automática → estado (GET /api/bank/reconciliation, que no
escribe). Las facturas se registran ya leídas y aprobadas: aquí se mide la conciliación, no la lectura de PDF.

Mide: conciliados automáticamente, posibles coincidencias, no conciliados, duplicados, discrepancias de importe,
errores y, sobre todo, conciliaciones automáticas incorrectas (el error que no se ve). Al final reimporta los
mismos extractos: ningún movimiento debe entrar dos veces.
"""
from __future__ import annotations

import json
import shutil
import uuid
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
INVOICE_KINDS = {"VARIAS", "PARCIAL", "ANTICIPO"}  # propuestas que reparten el movimiento entre facturas
RESULTS = {
    "conciliado": "Conciliado", "propuesta": "Propuesta para confirmar", "importe_distinto": "Importe distinto",
    "duplicado": "Duplicado", "sin_factura": "Sin factura", "justificado": "Justificado sin factura",
}


def seed_invoices(facturas: list[dict[str, Any]]) -> dict[int, str]:
    """Registra las facturas como si ya se hubieran leído y aprobado. Devuelve id → número."""
    from app.database import SessionLocal
    from app.models import Document
    from app.models import ExtractionRun
    from app.models import Invoice

    numbers = {}
    with SessionLocal() as database:
        for item in facturas:
            document = Document(original_filename=f"{item['numero']}.pdf", stored_filename=f"{uuid.uuid4().hex}.pdf", sha256=uuid.uuid4().hex * 2,
                                extension=".pdf", size_bytes=1, status="APPROVED", extraction_status="COMPLETED", kind="INVOICE")
            database.add(document)
            database.flush()
            if item.get("iban"):  # el IBAN del proveedor aparece en el texto de su factura
                database.add(ExtractionRun(document_id=document.id, extractor_name="evaluacion", extractor_version="1", status="COMPLETED",
                                           raw_text=f"FACTURA {item['numero']}\n{item['nombre']}\nIBAN: {item['iban']}", result_json={}))
            total = Decimal(item["total"])
            subtotal = (total / Decimal("1.21")).quantize(Decimal("0.01"))
            issued = item["sentido"] == "EMITIDA"
            party = {"customer_name": item["nombre"], "customer_tax_id": item["nif"]} if issued else {"supplier_name": item["nombre"], "supplier_tax_id": item["nif"]}
            invoice = Invoice(document_id=document.id, direction="ISSUED" if issued else "RECEIVED", invoice_number=item["numero"],
                              invoice_date=date.fromisoformat(item["fecha"]), due_date=date.fromisoformat(item["vence"]) if item.get("vence") else None,
                              total=total, subtotal=subtotal, tax_total=total - subtotal, currency="EUR", confidence=95, field_confidences={},
                              validation_status="VALID", validation_messages=[], review_status="APPROVED", duplicate_status="NONE", **party)
            database.add(invoice)
            database.flush()
            numbers[invoice.id] = item["numero"]
        database.commit()
    return numbers


def observed(row: dict[str, Any], numbers: dict[int, str]) -> dict[str, Any]:
    """El resultado de CapaFiscal para un movimiento, en los mismos términos que la verdad.

    Lo que ya está conciliado o aplicado lo hizo la conciliación automática: en la evaluación no actúa ninguna persona."""
    parts = list(row.get("allocations") or []) or list((row.get("proposal") or {}).get("parts", []))
    invoice_ids = {part["invoice_id"] for part in parts if part.get("invoice_id")}
    if row.get("invoice_id"):
        invoice_ids.add(row["invoice_id"])
    if row.get("level") == "CONFLICTO" and row["state"] == "POSIBLE":
        invoice_ids |= {item["invoice_id"] for item in row.get("candidates", [])}
    kind = next((part["kind"] for part in parts if not part.get("invoice_id")), None)
    if kind and not invoice_ids:
        result = "justificado"
    else:
        result = {"CONCILIADO": "conciliado", "POSIBLE": "propuesta", "IMPORTE_DISTINTO": "importe_distinto", "DUPLICADO": "duplicado",
                  "SIN_FACTURA": "sin_factura", "IGNORADO": "ignorado"}.get(row["state"], row["state"])
    return {"resultado": result, "estado": row["state"], "nivel": row.get("level"), "tipo": kind,
            "auto": row["state"] == "CONCILIADO" or bool(row.get("allocations")),
            "facturas": sorted(numbers.get(item, f"#{item}") for item in invoice_ids), "decision": row.get("decision")}


def compare(expected: dict[str, Any], got: dict[str, Any]) -> dict[str, bool]:
    allowed = expected["resultado"] if isinstance(expected["resultado"], list) else [expected["resultado"]]
    checks = {"resultado": got["resultado"] in allowed, "facturas": sorted(expected["facturas"]) == got["facturas"]}
    if expected.get("tipo"):
        checks["tipo"] = got["tipo"] == expected["tipo"]
    if expected.get("auto") is not None:
        checks["auto"] = got["auto"] == expected["auto"]
    return checks


def primary(expected: dict[str, Any]) -> str:
    """El resultado principal (si la verdad admite varios, el primero)."""
    return expected["resultado"][0] if isinstance(expected["resultado"], list) else expected["resultado"]


def unpaid_ok(expected: list[Any], got: list[str]) -> bool:
    """Cada entrada esperada es un número o una lista de alternativas («una de estas queda sin pago»)."""
    remaining = list(got)
    for entry in expected:
        options = entry if isinstance(entry, list) else [entry]
        hits = [number for number in remaining if number in options]
        if len(hits) != 1:
            return False
        remaining.remove(hits[0])
    return not remaining


def wrong_auto(expected: dict[str, Any], got: dict[str, Any]) -> bool:
    """Conciliado solo cuando no debía, o con otra factura: el error que nadie ve."""
    if not got["auto"]:
        return False
    if expected.get("auto") is False:
        return True
    return "justificado" not in expected["resultado"] and sorted(expected["facturas"]) != got["facturas"]


def run(folder: Path) -> dict[str, Any]:
    from evaluation.casos import prepare_environment
    from evaluation.casos import reset_database

    labels = json.loads((folder / "labels.json").read_text(encoding="utf-8"))
    work = prepare_environment("reglas")
    from fastapi.testclient import TestClient

    from app.main import app

    rows, imports, reimports, errors = [], [], [], []
    try:
        with TestClient(app) as client:
            reset_database()
            company = labels.get("empresa", {})
            client.put("/api/company", json={"name": company.get("name", "Empresa"), "tax_id": company.get("tax_id"), "legal_form": "SOCIEDAD"}).raise_for_status()
            numbers = seed_invoices(labels["facturas"])

            def upload(statement: dict[str, str]) -> dict[str, Any] | None:
                path = folder / statement["fichero"]
                response = client.post("/api/bank/import", data={"account_label": statement["cuenta"]},
                                       files={"uploaded_file": (path.name, path.read_bytes(), "text/csv")})
                if response.status_code >= 400:
                    errors.append(f"{path.name}: HTTP {response.status_code} {response.text[:200]}")
                    return None
                return response.json()

            imports = [upload(statement) for statement in labels["extractos"]]
            report = client.get("/api/bank/reconciliation").json()
            movements = report["movements"]
            for truth in labels["movimientos"]:
                amount = float(Decimal(truth["importe"]))
                match = next((row for row in movements if row["date"] == truth["fecha"] and row["description"] == truth["concepto"]
                              and abs(row["amount"] - amount) < 0.005 and not row.get("_used")), None)
                if match is None:
                    errors.append(f"No se importó: {truth['fecha']} {truth['concepto']} {truth['importe']}")
                    rows.append({"movement": truth, "expected": truth["expected"], "got": None, "checks": {"importado": False}, "perfect": False, "wrong_auto": False})
                    continue
                match["_used"] = True
                got = observed(match, numbers)
                checks = compare(truth["expected"], got)
                rows.append({"movement": truth, "expected": truth["expected"], "got": got, "checks": checks, "perfect": all(checks.values()),
                             "wrong_auto": wrong_auto(truth["expected"], got)})
            extra = [row for row in movements if not row.get("_used")]
            unpaid = sorted(item["invoice_label"].split(" · ")[0] for item in report["unpaid_invoices"])
            reimports = [upload(statement) for statement in labels["extractos"]]  # idempotencia
    finally:
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)

    got_results = Counter(row["got"]["resultado"] for row in rows if row["got"])
    expected_results = Counter(primary(row["expected"]) for row in rows)
    per_result = {name: {"expected": expected_results.get(name, 0), "ok": sum(1 for row in rows if primary(row["expected"]) == name and row["perfect"])}
                  for name in RESULTS}
    return {
        "dataset": folder.name, "movements": len(rows), "imported": sum((item or {}).get("imported", 0) for item in imports),
        "perfect": sum(row["perfect"] for row in rows),
        "auto_matched": sum(1 for row in rows if row["got"] and row["got"]["auto"]),
        "wrong_auto": sum(row["wrong_auto"] for row in rows),
        "observed": dict(got_results), "per_result": per_result,
        "unpaid_expected": labels.get("facturas_sin_pago", []), "unpaid_got": unpaid,
        "unpaid_ok": unpaid_ok(labels.get("facturas_sin_pago", []), unpaid),
        "reimport": {"imported": sum((item or {}).get("imported", 0) for item in reimports), "duplicated": sum((item or {}).get("duplicated", 0) for item in reimports)},
        "extra_movements": len(extra), "errors": errors, "rows": rows,
    }


def to_markdown(report: dict[str, Any]) -> str:
    observed_counts = report["observed"]
    lines = [
        f"# Evaluación de conciliación · {report['dataset']}", "",
        f"- Movimientos: {report['movements']} · importados {report['imported']} · correctos **{report['perfect']}/{report['movements']}**",
        f"- Conciliados automáticamente: {report['auto_matched']} · **conciliaciones automáticas incorrectas: {report['wrong_auto']}**",
        f"- Posibles coincidencias (para confirmar): {observed_counts.get('propuesta', 0)} · no conciliados (sin factura): {observed_counts.get('sin_factura', 0)}"
        f" · justificados sin factura: {observed_counts.get('justificado', 0)}",
        f"- Duplicados detectados: {observed_counts.get('duplicado', 0)} · discrepancias de importe: {observed_counts.get('importe_distinto', 0)}",
        f"- Facturas vencidas sin pago: esperadas {report['unpaid_expected']}, detectadas {report['unpaid_got']}"
        + ("" if report["unpaid_ok"] else " **(no coincide)**"),
        f"- Reimportar los mismos extractos: {report['reimport']['imported']} nuevos, {report['reimport']['duplicated']} ya existían (idempotencia)",
        f"- Errores: {len(report['errors'])}" + "".join(f"\n  - {error}" for error in report["errors"]), "",
        "| Resultado esperado | Movimientos | Correctos |", "|---|---:|---:|",
    ]
    for name, entry in report["per_result"].items():
        if entry["expected"]:
            lines.append(f"| {RESULTS[name]} | {entry['expected']} | {entry['ok']} |")
    lines += ["", "## Fallos", ""]
    failures = [row for row in report["rows"] if not row["perfect"]]
    for row in failures:
        movement, got = row["movement"], row["got"] or {}
        wrong = [f"{name}: esperado `{row['expected'].get(name)}`, obtenido `{got.get(name)}`" for name, ok in row["checks"].items() if not ok]
        lines.append(f"- {movement['fecha']} «{movement['concepto']}» {movement['importe']}: " + "; ".join(wrong)
                     + (" **(conciliación automática incorrecta)**" if row["wrong_auto"] else "") + (f" — {got.get('decision')}" if got.get("decision") else ""))
    if not failures:
        lines.append("Ninguno.")
    return "\n".join(lines) + "\n"
