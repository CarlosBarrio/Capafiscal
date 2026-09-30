"""
Calendario administrativo español.

Solo incluye festivos nacionales comunes: los autonómicos y locales
también son inhábiles, así que los plazos calculados son orientativos y
se muestran siempre como revisables.
"""
from __future__ import annotations

import calendar
from datetime import date
from datetime import timedelta
from functools import lru_cache


def easter_sunday(year: int) -> date:
    # Algoritmo de Meeus/Jones/Butcher (calendario gregoriano).
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)

    return date(year, month, day + 1)


@lru_cache(maxsize=64)
def national_holidays(year: int) -> frozenset[date]:
    fixed = {
        (1, 1),    # Año Nuevo
        (1, 6),    # Epifanía
        (5, 1),    # Fiesta del Trabajo
        (8, 15),   # Asunción
        (10, 12),  # Fiesta Nacional
        (11, 1),   # Todos los Santos
        (12, 6),   # Constitución
        (12, 8),   # Inmaculada Concepción
        (12, 25),  # Navidad
    }

    holidays = {date(year, month, day) for month, day in fixed}
    holidays.add(easter_sunday(year) - timedelta(days=2))  # Viernes Santo

    return frozenset(holidays)


def is_business_day(value: date) -> bool:
    # Ley 39/2015, art. 30: sábados, domingos y festivos son inhábiles.
    return value.weekday() < 5 and value not in national_holidays(value.year)


def next_business_day(value: date) -> date:
    """El propio día si es hábil; si no, el siguiente hábil."""
    current = value

    while not is_business_day(current):
        current += timedelta(days=1)

    return current


def add_business_days(start: date, days: int) -> date:
    """
    Plazo en días hábiles contado desde el día siguiente a `start`.
    """
    current = start
    remaining = days

    while remaining > 0:
        current += timedelta(days=1)

        if is_business_day(current):
            remaining -= 1

    return current


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])

    return date(year, month, day)


def last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def days_until(target: date, today: date | None = None) -> int:
    return (target - (today or date.today())).days
