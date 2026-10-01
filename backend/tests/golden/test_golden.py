"""
Golden set: cada fallo que destapó el banco B (v1) es ya una regresión permanente.

Los expedientes viven en evaluation/datasets/b_sintetico/<caso>/ con su
caso.json (la verdad, versionada antes de la primera ejecución). Aquí se
exige que sigan saliendo bien: si una regla nueva rompe uno, salta el test.
Las 5 facturas reales (conjunto A) no se tocan y siguen fuera de git.
"""
from __future__ import annotations

import pytest

from evaluation import casos

# caso → qué fallaba en B v1
GOLDEN = {
    "B01": "plazo sin leer la fecha de notificación; facturas y justificantes no reconocidos",
    "B02": "plazo; dos apartados pegados en una línea",
    "B03": "propuesta de liquidación tomada por liquidación (por el nombre del archivo); sin importe",
    "B05": "embargo: retenía todo el crédito (4.100) y no la deuda (2.850); deuda = principal",
    "B06": "embargo de pagos sucesivos sin mencionar los pagos futuros",
    "B07": "deuda = principal en vez del total pendiente",
    "B08": "embargo de salario sin importe",
    "B09": "plazo desde la fecha del documento",
    "B10": "reclamación de deuda TGSS tomada por providencia de apremio",
    "B11": "comunicación laboral leída como factura (CCC como NIF)",
    "B12": "certificado que abría un expediente",
    "B13": "justificante del 303 leído como factura; periodo no registrado",
    "B14": "notificación a otro NIF tratada como de la empresa",
    "B15": "facturas citadas por número no comprobadas; 303 presentado no señalado",
    "B16": "no pedía la factura que falta",
    "B18": "rectificativa con importes positivos (error silencioso)",
    "B19": "profesional persona física: la empresa tomada como emisora",
    "B23": "rectificativa en un correo: número mal leído e importes positivos",
    "B-ADV01": "embargo que enumera facturas: deuda = principal",
    "B-ADV10": "OCR malo: «550» como número de factura (error silencioso)",
    "B-PLAZOS01": "modelo 130 a una sociedad",
}


@pytest.mark.parametrize("case_id", sorted(GOLDEN))
def test_golden_case(client, case_id):
    result = casos.run_case(client, casos.DATASETS / "b_sintetico" / case_id)
    failures = [f"{entry['entrada']} · {item['check']}: esperado {item['expected']} · observado {item['observed']}"
                for entry in result["entries"] for item in entry["checks"] if not item["ok"]]
    assert not failures, f"{case_id} ({GOLDEN[case_id]}):\n" + "\n".join(failures)


def test_bank_scan_does_not_crash(client):
    """B-BANCO01 rompía el barrido con un error 500; la factura sin pago aún no ha vencido."""
    result = casos.run_case(client, casos.DATASETS / "b_sintetico" / "B-BANCO01")
    assert not [entry for entry in result["entries"] if entry["outcome"] == "error_sistema"]
