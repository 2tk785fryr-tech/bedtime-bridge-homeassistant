"""v0.5.3 item 8: values from the app are checked on receipt; a bad one is refused (logged) and
never stored, so it can't stop setup on a later restart."""

from __future__ import annotations

import logging
import re
from typing import Any

_LOGGER = logging.getLogger(__name__)
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def clean(settings: dict[str, Any], defaults: dict[str, Any], what: str) -> dict[str, Any]:
    """The known keys whose value has the default's type (a bool for a bool, a whole number for
    a number, "HH:MM" for a clock time); anything else is dropped with a log line."""
    kept: dict[str, Any] = {}
    for key, value in settings.items():
        if key not in defaults:
            continue
        default = defaults[key]
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        elif isinstance(default, str) and _CLOCK.match(default):
            ok = isinstance(value, str) and bool(_CLOCK.match(value))
        elif isinstance(default, str):
            ok = isinstance(value, str)
        else:
            ok = True
        if ok:
            kept[key] = value
        else:
            _LOGGER.warning("Bedtime Bridge refused %s %s=%r: not a valid value", what, key, value)
    return kept


def weekly(settings: Any) -> dict[str, Any] | None:
    """The weekly summary's settings, or None when they aren't valid."""
    if not isinstance(settings, dict):
        return None
    enabled, weekday, time = settings.get("enabled"), settings.get("weekday"), settings.get("time")
    if not isinstance(enabled, bool) or not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 7 \
            or not isinstance(time, str) or not _CLOCK.match(time):
        _LOGGER.warning("Bedtime Bridge refused the weekly summary settings: %r", settings)
        return None
    return {"enabled": enabled, "weekday": weekday, "time": time}
