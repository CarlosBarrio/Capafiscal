"""El reloj: «hoy» es el día en Madrid y nada en la aplicación lee la hora del sistema por su cuenta."""
from __future__ import annotations

import re
from datetime import date
from datetime import datetime
from datetime import timezone
from pathlib import Path

from app import clock

APP = Path(__file__).resolve().parents[1] / "app"
DIRECT = re.compile(r"\bdate\.today\(\)|datetime\.now\((timezone\.utc|UTC|tz=)")


def test_today_is_the_day_in_madrid_not_in_utc():
    with clock.frozen(datetime(2026, 9, 30, 22, 30, tzinfo=timezone.utc)):  # 00:30 del 1 de octubre en Madrid
        assert clock.today() == date(2026, 10, 1)
        assert clock.now().tzinfo is not None
    with clock.frozen(datetime(2026, 9, 30, 21, 30, tzinfo=timezone.utc)):
        assert clock.today() == date(2026, 9, 30)


def test_frozen_restores_the_previous_clock():
    before = clock.now()
    with clock.frozen(datetime(2030, 1, 1)):
        assert clock.today() == date(2030, 1, 1)
    assert clock.now() == before


def test_the_application_only_reads_time_through_the_clock():
    offenders = [f"{path.relative_to(APP)}:{number}" for path in APP.rglob("*.py") if path.name != "clock.py"
                 for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if DIRECT.search(line)]
    assert not offenders, f"Usa app.clock en lugar de la hora del sistema: {offenders}"
