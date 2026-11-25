"""v0.5.4 items 27 and 28: the morning and bedtime briefings, built here at good-morning time
and at bedtime, so a Home Assistant announcement can speak them.

The app sends its choices with every message (`briefing_settings`: which parts, the greeting,
the sign-off, how events are named) and what it knows about the morning with each plan
(`briefing`: the event, the forecast line, extras, heads-ups). The live parts (alarms, leave-by
and the drive, time to get ready, sleep, the house, the battery) come from here. The words match
the app's own briefing (BedtimeBridge/Core/Briefing.swift)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from . import timefmt

MORNING_PARTS = ("greeting", "today", "leave", "get_ready", "weather", "extras")
BEDTIME_PARTS = ("alarms", "tomorrow", "sleep", "house", "weather", "heads_ups", "sign_off")
NAMES_TITLE = "title"
NAMES_GENERIC = "generic"

DEFAULTS: dict[str, Any] = {
    "morning": True,
    "bedtime": True,
    "greeting": "Good morning.",
    "sign_off": "Good night.",
    # Parts turned off; a part added later starts on.
    "morning_off": [],
    "bedtime_off": [],
    "event_names": NAMES_TITLE,
}
# Facts from a plan older than this are about another morning.
FACTS_FRESH = timedelta(hours=20)
LOW_BATTERY = 30


def clean_settings(value: Any) -> dict[str, Any] | None:
    """The app's briefing settings, keeping only known keys with the right type."""
    if not isinstance(value, dict):
        return None
    kept: dict[str, Any] = {}
    for key in ("morning", "bedtime"):
        if isinstance(value.get(key), bool):
            kept[key] = value[key]
    for key in ("greeting", "sign_off"):
        if isinstance(value.get(key), str):
            kept[key] = value[key][:200]
    if isinstance(value.get("morning_off"), list):
        kept["morning_off"] = [p for p in value["morning_off"] if p in MORNING_PARTS]
    if isinstance(value.get("bedtime_off"), list):
        kept["bedtime_off"] = [p for p in value["bedtime_off"] if p in BEDTIME_PARTS]
    if value.get("event_names") in (NAMES_TITLE, NAMES_GENERIC):
        kept["event_names"] = value["event_names"]
    return kept


def settings(saved: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS, **(saved or {})}


def _time(value: Any) -> datetime | None:
    return dt_util.parse_datetime(value) if isinstance(value, str) else None


def _sentence(text: str | None) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    return text if text[-1] in ".!?" else text + "."


def spoken_minutes(minutes: int) -> str:
    """"48 minutes", "1 hour", "6 hours 10 minutes"."""
    minutes = max(0, int(minutes))
    hours, rest = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if rest or not hours:
        parts.append(f"{rest} minute{'s' if rest != 1 else ''}")
    return " ".join(parts)


def event_name(event: dict[str, Any], names: str) -> str:
    """The title, or "your 9:00 appointment" for shared rooms and speakers. A shift keeps its
    type ("Days"), which says nothing private."""
    start = _time(event.get("start"))
    if event.get("kind") == "shift" or names == NAMES_TITLE:
        return str(event.get("name") or "your event")
    return f"your {timefmt.short(start)} appointment" if start else "your appointment"


def today_line(event: dict[str, Any] | None, names: str, day_off: bool = False) -> str:
    """"You work Days at 6:30." / "Dentist at 9:00." / "No work today." (plus the first event)."""
    if not event or not event.get("start"):
        return "No work today."
    start = _time(event["start"])
    name = event_name(event, names)
    if event.get("kind") == "shift" and not day_off:
        return f"You work {name} at {timefmt.short(start)}."
    if names == NAMES_GENERIC and event.get("kind") != "shift":
        line = name[0].upper() + name[1:] + "."
    else:
        line = f"{name} at {timefmt.short(start)}."
    return ("No work today. " + line) if day_off else line


def leave_line(leave_by: datetime | None, drive_minutes: int | None, planned_minutes: int | None, train: str | None) -> str | None:
    """"Leave by 5:48. The drive is 32 minutes in current traffic, about 6 more than planned." For
    a train, its own line."""
    if train:
        return _sentence(train)
    if leave_by is None:
        return None
    line = f"Leave by {timefmt.short(leave_by)}."
    if drive_minutes:
        line += f" The drive is {spoken_minutes(drive_minutes)} in current traffic"
        if planned_minutes and abs(drive_minutes - planned_minutes) >= 3:
            diff = drive_minutes - planned_minutes
            line += f", about {abs(diff)} {'more' if diff > 0 else 'less'} than planned"
        line += "."
    return line


def get_ready_line(now: datetime, leave_by: datetime | None) -> str | None:
    if leave_by is None:
        return None
    minutes = round((leave_by - now).total_seconds() / 60)
    if minutes <= 0:
        return "It's time to leave."
    return f"You have {spoken_minutes(minutes)} to get ready."


def alarms_line(alarms: list[datetime], now: datetime) -> str:
    """"3 alarms tomorrow, the first at 4:20." / "No alarms tomorrow. Sleep in.\""""
    ahead = sorted(a for a in alarms if a > now)
    if not ahead:
        return "No alarms tomorrow. Sleep in."
    first = ahead[0]
    day = "this morning" if dt_util.as_local(first).date() == dt_util.as_local(now).date() else "tomorrow"
    if len(ahead) == 1:
        return f"1 alarm {day}, at {timefmt.short(first)}."
    return f"{len(ahead)} alarms {day}, the first at {timefmt.short(first)}."


def tomorrow_line(event: dict[str, Any] | None, names: str, leave_by: datetime | None) -> str | None:
    if not event or not event.get("start"):
        return None
    start = _time(event["start"])
    name = event_name(event, names)
    what = name[0].upper() + name[1:] if (names == NAMES_GENERIC and event.get("kind") != "shift") else f"{name} at {timefmt.short(start)}"
    line = f"Tomorrow: {what}."
    if leave_by is not None:
        line += f" Leave by {timefmt.short(leave_by)}."
    return line


def sleep_line(now: datetime, first_alarm: datetime | None, goal_minutes: int | None) -> str | None:
    """"If you sleep now, 6 hours 10 minutes until your first alarm, an hour short of your goal.\""""
    if first_alarm is None or first_alarm <= now:
        return None
    minutes = round((first_alarm - now).total_seconds() / 60)
    line = f"If you sleep now, {spoken_minutes(minutes)} until your first alarm"
    if goal_minutes:
        short = int(goal_minutes) - minutes
        if short >= 15:
            line += f", {'an hour' if short == 60 else spoken_minutes(short)} short of your goal"
        elif short > -15:
            line += ", about your goal"
    return line + "."


def house_line(problems: list[str] | None, away: bool) -> str | None:
    """Only problems: "At home, the back door is open." away, "Heads up: …" at home."""
    problems = [p for p in (problems or []) if p]
    if not problems:
        return None
    joined = problems[0] if len(problems) == 1 else ", ".join(problems[:-1]) + " and " + problems[-1]
    return ("At home, " if away else "Heads up: ") + joined + "."


def battery_line(level: float | None, charging: bool | None) -> str | None:
    if charging is None or charging:
        return None
    if level is not None and level < LOW_BATTERY:
        return f"Your phone is at {round(level)}% and not charging."
    return "Your phone isn't charging."


def morning_text(
    cfg: dict[str, Any],
    facts: dict[str, Any] | None,
    *,
    now: datetime,
    leave_by: datetime | None,
    drive_minutes: int | None,
    planned_minutes: int | None,
    away: bool,
) -> str:
    """The morning briefing, most important first."""
    facts = facts or {}
    off = set(cfg.get("morning_off") or [])
    names = cfg.get("event_names") or NAMES_TITLE
    day_off = bool(facts.get("day_off"))
    lines: list[str | None] = []
    if "greeting" not in off:
        lines.append(_sentence(cfg.get("greeting")))
    if away:
        lines.append(_sentence(facts.get("zone")))
    if "today" not in off:
        lines.append(today_line(facts.get("event"), names, day_off))
    if not day_off:
        if leave_by is not None and leave_by < now - timedelta(minutes=15):
            leave_by = None
        if "leave" not in off:
            lines.append(leave_line(leave_by, drive_minutes, planned_minutes, facts.get("train")))
        if "get_ready" not in off:
            lines.append(get_ready_line(now, leave_by))
    if "weather" not in off:
        lines.append(_sentence(facts.get("weather")))
    if "extras" not in off and not day_off:
        lines += [_sentence(x) for x in facts.get("extras") or []]
    return " ".join(line for line in lines if line)


def bedtime_text(
    cfg: dict[str, Any],
    facts: dict[str, Any] | None,
    *,
    now: datetime,
    alarms: list[datetime],
    leave_by: datetime | None,
    sleep_goal: int | None,
    house: list[str] | None,
    away: bool,
    battery: float | None,
    charging: bool | None,
    alarms_known: bool = True,
) -> str:
    """The bedtime briefing, in its set order. `alarms_known` False (no plan came tonight, v0.5.4
    item 60): the alarms and sleep lines are left out rather than say "Sleep in"."""
    facts = facts or {}
    off = set(cfg.get("bedtime_off") or [])
    names = cfg.get("event_names") or NAMES_TITLE
    ahead = sorted(a for a in alarms if a > now)
    lines: list[str | None] = []
    if away:
        lines.append(_sentence(facts.get("zone")))
    if "alarms" not in off and alarms_known:
        lines.append(alarms_line(ahead, now))
    if "tomorrow" not in off:
        lines.append(tomorrow_line(facts.get("event"), names, leave_by if leave_by and leave_by > now else None))
    if "sleep" not in off:
        lines.append(sleep_line(now, ahead[0] if ahead else None, sleep_goal))
    if "house" not in off:
        lines.append(house_line(house, away))
    if "weather" not in off:
        lines.append(_sentence(facts.get("tomorrow_weather")))
    if "heads_ups" not in off:
        lines.append(battery_line(battery, charging))
        lines += [_sentence(x) for x in facts.get("heads_ups") or []]
    if "sign_off" not in off:
        lines.append(_sentence(cfg.get("sign_off")))
    return " ".join(line for line in lines if line)


def fresh_facts(saved: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """The plan's facts, unless they're from another morning."""
    if not isinstance(saved, dict):
        return {}
    at = _time(saved.get("at"))
    if at is not None and now - at > FACTS_FRESH:
        return {}
    return saved
