from __future__ import annotations

import re
from typing import Annotated

from fastapi import Depends
from fastapi import Header
from sqlalchemy.orm import Session

from app.database import get_db


SAFE_ACTOR_PATTERN = re.compile(
    r"[^a-zA-Z0-9@._\-\s]"
)


DatabaseDependency = Annotated[
    Session,
    Depends(get_db),
]

ActorHeader = Annotated[
    str | None,
    Header(alias="X-Actor"),
]


def normalize_actor(
    actor: str | None,
) -> str:
    if not actor:
        return "usuario-local"

    cleaned = SAFE_ACTOR_PATTERN.sub(
        "",
        actor,
    ).strip()

    return cleaned[:100] or "usuario-local"
