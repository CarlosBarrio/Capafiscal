"""La demo «cero intervención» sigue funcionando de punta a punta."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "demo_cero_intervencion.py"


def test_zero_intervention_demo():
    result = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stderr[-2000:]
    output = result.stdout
    assert output.count("ESPERA TU REVISIÓN") == 2
    assert "buzón DEHú" in output and "Ruta «Requerimiento o acto administrativo»" in output
    assert "Ruta «Factura sospechosa»" in output and "6,0 veces lo habitual" in output
    assert "requieren atención" in output
