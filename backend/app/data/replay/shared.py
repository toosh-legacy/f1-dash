"""The few things every other replay module needs.

Kept deliberately small: a module this widely imported is one that must not
grow opinions of its own.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger("app.data.replay")

#: Called by a build to report what it is doing, stage by stage.
Progress = Callable[[str, dict[str, Any] | None], None]


class ReplayUnavailable(RuntimeError):
    """The session has no usable position feed."""


# --------------------------------------------------------------------------
def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _round(value: Any, places: int = 3) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, places) if math.isfinite(number) else None
