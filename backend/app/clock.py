"""
El reloj de CapaFiscal: un único sitio del que sale «hoy» y «ahora».

    now()      instante actual, con zona (UTC)
    today()    el día de hoy en España (Europe/Madrid, o APP_TIMEZONE): un plazo vence
               a medianoche en Madrid, no en UTC
    frozen()   para las pruebas: fija el reloj y todo CapaFiscal ve la misma fecha,
               así un test no pasa ayer y falla hoy (ni al cruzar la medianoche)

Nada en la aplicación debe llamar a date.today() ni a datetime.now(...) directamente.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Iterator
from zoneinfo import ZoneInfo

ZONE = ZoneInfo(os.environ.get("APP_TIMEZONE", "Europe/Madrid"))
_frozen: datetime | None = None


def now() -> datetime:
    """Instante actual en UTC (o el fijado en las pruebas)."""
    return _frozen if _frozen is not None else datetime.now(timezone.utc)


def today() -> date:
    """El día de hoy en la zona horaria de la empresa."""
    return now().astimezone(ZONE).date()


def freeze(moment: datetime | None) -> None:
    """Fija el reloj (None lo devuelve a la hora real)."""
    global _frozen
    if moment is not None and moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    _frozen = moment


@contextmanager
def frozen(moment: datetime) -> Iterator[None]:
    previous = _frozen
    freeze(moment)
    try:
        yield
    finally:
        freeze(previous)
