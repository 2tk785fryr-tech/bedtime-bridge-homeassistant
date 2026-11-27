"""State kept for the phone, and handling of the app's webhook messages."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import validate
from . import drivetime as dtm
from . import helpers as made
from . import ladder as ldr
from . import briefing as brf
from . import reports, timefmt, weekly
from .awake import NoAlarmMorning
from .clears import OwedClears
from .liveactivity import LiveActivities
from .pending import BedtimePending
from .winddown import WindDown
from .const import (
    APP_VERSION_BEFORE_CHECK,
    INTEGRATION_VERSION,
    ISSUE_APP_OUTDATED,
    ISSUE_INTEGRATION_OUTDATED,
    MIN_APP_VERSION,
    BED_EMPTY_SECONDS,
    CMD_ARM,
    CMD_BACK_TO_BED,
    CMD_NAP_END,
    CMD_PLAN_PREVIEW,
    CMD_MORNING_UPDATE,
    CMD_NAP_START,
    CMD_LEAVE_CHECK,
    CMD_BEDTIME,
    CMD_DISARM,
    CMD_MORNING,
    CMD_QUIET_MORNING,
    COMMAND_TAG,
    COMMAND_NAMES,
    COMMANDS,
    LADDER_SETTINGS,
    LEAVE_CHECK_TAIL_MINUTES,
    CONF_AWAKE_HELPER,
    CONF_BEDROOM_DOOR,
    CONF_NAP_HELPER,
    CONF_WIND_DOWN_HELPER,
    CONF_PHONE_BATTERY,
    CONF_PHONE_BATTERY_STATE,
    CONF_DRIVE_SOURCE,
    CONF_GOOGLE_KEY,
    CONF_GOOGLE_BUDGET,
    CONF_MAPS_KEY,
    CONF_MAPS_KEY_ID,
    CONF_TOMTOM_KEY,
    CONF_BED_SENSOR,
    CONF_BEDTIME_HELPER,
    CONF_CURTAIN_OFFSET,
    CONF_EXIT_LATCH,
    CONF_HOME_CHECKS,
    CONF_HOME_ONLY,
    CONF_NOTIFY,
    CONF_PHONE_COMMANDS,
    CONF_PRESENCE,
    CONF_REARM_MINUTES,
    CONF_REPLAN_ALERT,
    CONF_RERUN_MINUTES,
    CONF_CALENDAR,
    CONF_COMPLETE_MINUTES,
    CONF_HOUSE_BACKSTOP,
    CONF_OLD_RUNGS,
    CONF_EARLY_HELPER,
    CONF_RESTART_AUTOMATIONS,
    CONF_LADDER_SHIFT_TYPES,
    CONF_SHIFT_OVERRIDE,
    CONF_SHIFT_SENSOR,
    CONF_SHIFT_TYPES,
    CONF_SLEEP_IN,
    CONF_SNOOZE_HELPER,
    CONF_STOP_HELPER,
    CONF_WAKE_ACTION,
    CONF_WEATHER,
    DEFAULT_CURTAIN_OFFSET,
    DEFAULT_REARM_MINUTES,
    DEFAULT_RERUN_MINUTES,
    DEFAULT_COMPLETE_MINUTES,
    HOUSE_OLD,
    DEFAULT_LADDER_SHIFT_TYPES,
    ladder_shift_types,
    CONF_CLEAR_TAGS,
    RESTART_NOTIFICATION_TAGS,
    clear_tags,
    DEFAULT_WORK_BUFFER,
    DOMAIN,
    EVENT,
    EVENT_PLAN_OUTDATED,
    EVENT_ALARMS_MISSING,
    EVENT_CHANGE_REFUSED,
    EVENT_COMMAND_INCOMPLETE,
    EVENT_REARMED,
    EVENT_BACKSTOP_FIRED,
    EVENT_RERUN,
    HOME_STATES,
    MSG_BEDTIME,
    MSG_BEDTIME_UNDONE,
    MSG_COMMAND_RAN,
    MSG_MORNING_CHANGED,
    MSG_NAP_ENDED,
    MSG_PLAN_CURRENT,
    MSG_PREVIEW,
    MSG_RESET,
    MSG_LIVE_ACTIVITY,
    MSG_AWAKE,
    MSG_GOOD_NIGHT,
    MSG_HEALTH,
    MSG_REPORT,
    MSG_WIND_DOWN,
    MSG_NAP_STARTED,
    MSG_CANCELLED,
    MSG_CONTEXT,
    MSG_FORECAST,
    MSG_HOME_CHECK,
    MSG_LEAVE_BY,
    MSG_PLAN_FAILED,
    MSG_PLANNED,
    MSG_SKIPPED,
    MSG_SNOOZED,
    MSG_SET_SETTING,
    MSG_STOPPED,
    MSG_TEMP_SNOOZED,
    MSG_TEMP_STOPPED,
    MSG_UNSKIPPED,
    EVENT_BEDTIME_BRIEFING,
    EVENT_MORNING_BRIEFING,
    MORNING_COMMANDS,
    MSG_UP_CHECK,
    REARM_GIVE_UP_HOURS,
    REPLAN_URL,
    SETTINGS,
    SHIFT_LEAD_PREFIX,
    UNKNOWN_STATES,
    WAKE_BOTH,
    WAKE_EVENTS,
    WAKE_HELPERS,
    shift_types,
    signal_update,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Plan fields copied from the app's "planned" message into the state.
PLAN_FIELDS = (
    "alarm_cause",
    "alarm_time",
    "wake_time",
    "alarm_times",
    "label",
    "reason",
    "leave_by",
    "drive_minutes",
    "weather_extra_minutes",
    "warnings",
    "alarm_style",
    # v0.4.11 item 5: a remote work day, with no trip.
    "remote",
    # v0.4.11 item 75: you sleep at work tonight (a duty night): no home routines.
    "duty_night",
)


def empty_state() -> dict[str, Any]:
    """State with no alarm planned."""
    return {
        "alarm_cause": "noalarm",
        "alarm_time": None,
        "wake_time": None,
        "alarm_times": [],
        "label": None,
        "reason": None,
        "leave_by": None,
        "drive_minutes": None,
        "weather_extra_minutes": 0,
        "warnings": [],
        "alarm_style": None,
        "remote": False,
        "duty_night": False,
        "departed": None,
        "snoozed_until": None,
        "snooze_count": 0,
        # v0.6.0 item 38: a Stop that wasn't the last of the wake-up (Waiting on you stays on).
        "wake_up_open": None,
        # The wake routine (stop helper, Good Morning) already ran for this series (item 9).
        "routine_ran": False,
        "last_event": None,
        "last_event_time": None,
        "skipped_away": False,
        "plan_outdated": False,
        "outdated_reason": None,
        # v0.4.11 item 26: this morning's backstops that escalated: rung, level and time.
        "backstops_fired": [],
    }


def plan_record() -> dict[str, Any]:
    """Kept across new plans and cancels: when the phone last planned, why it failed, and the
    last finished morning's snooze total."""
    return {"last_plan": None, "plan_error": None, "undone_at": None, "snoozes_last_morning": None}


def pending_record() -> dict[str, Any]:
    """Kept across new plans too, and across restarts: a re-arm waiting for you to get back
    into bed, a shift override to put back after a skipped morning, and when the regular
    alarms began (so a trip out of the bedroom before them isn't read as being up)."""
    return {
        "rearm_since": None,
        "rearm_until": None,
        "rerun_since": None,
        "rerun_until": None,
        "skip_restore_to": None,
        "skip_restore_at": None,
        "series_start": None,
    }


# v0.5.4 item 28: a plan this soon after bedtime started is tonight's (the bedtime briefing waits
# for it); a plan this recent when bedtime starts already is.
BRIEFING_AFTER_BEDTIME = timedelta(hours=1)
BRIEFING_PLAN_FRESH = timedelta(minutes=30)
# v0.5.4 item 60: no plan this long after bedtime started (your own alarms kept, say): the bedtime
# briefing goes out with what's known. A plan this recent still says what tomorrow's alarms are.
BRIEFING_FALLBACK = timedelta(minutes=2)
BRIEFING_PLAN_KNOWN = timedelta(hours=12)

# Pushes whose shortcut should report it ran within 10 minutes (v0.4.8 item 11).
WATCHED_COMMANDS = (CMD_BEDTIME, CMD_MORNING, CMD_LEAVE_CHECK, CMD_QUIET_MORNING)
COMMAND_RAN_WAIT = timedelta(minutes=10)
# A bedtime shortcut run this soon after RUN_BEDTIME was sent counts as run by the push.
BEDTIME_BY_PUSH = timedelta(minutes=15)
# v0.5.2 item 3: an alarm with no snooze or stop this long after it rang wasn't answered.
UNANSWERED_AFTER = timedelta(minutes=10)
# v0.5.4 item 56: the made exit latch only latches from the first alarm until this much later, so
# a night-time trip out of the bedroom doesn't read as "left this morning".
EXIT_LATCH_WINDOW = timedelta(hours=12)
# v0.5.3 item 4: how many replies are kept to answer a retried message.
REPLY_MEMORY = 50
# v0.5.3 item 35: Good Morning is sent again this long after, while it hasn't reported it ran.
GOOD_MORNING_RESENDS = (timedelta(minutes=2), timedelta(minutes=5))
# Event fired when one wasn't answered.
EVENT_ALARM_UNANSWERED = "alarm_unanswered"


def _command_tag(message: str) -> str:
    return f"{COMMAND_TAG}_{message.lower()}"


def _time(value: Any) -> datetime | None:
    """A time with its offset; v0.5.3 item 8: one without (or unreadable) counts as none."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt_util.parse_datetime(value)
    except ValueError:
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


# v0.5.4 item 71: a night runs from noon to noon (local), so a bedtime after midnight is the night
# before's. The bedtime briefing is announced once per night by this date.
NIGHT_STARTS_AFTER = timedelta(hours=12)


def _night_of(value: Any) -> str | None:
    """The local date of the night a bedtime stamp belongs to."""
    when = _time(value)
    return (dt_util.as_local(when) - NIGHT_STARTS_AFTER).date().isoformat() if when else None


# v0.5.5 item 33: leave days. The titles until the app sends its own, and how long a day's
# answer from the work calendar is kept.
DEFAULT_LEAVE_TITLES = ("Leave",)
LEAVE_CACHE = timedelta(minutes=10)


def leave_covers(event: dict[str, Any], day: Any) -> bool:
    """An all-day event (dates, end exclusive) or a timed one that covers `day` (a local date)."""
    start, end = str(event.get("start") or ""), str(event.get("end") or "")
    if len(start) == 10 and len(end) == 10:
        return start <= day.isoformat() < end
    first, last = _time(start), _time(end)
    if first is None or last is None:
        return False
    day_start = dt_util.start_of_local_day(day)
    return first < day_start + timedelta(days=1) and last > day_start


def version_less(a: str, b: str) -> bool:
    """Dotted versions compared part by part ("0.5.1" < "0.5.2" < "0.5.10")."""
    def parts(v: str) -> list[int]:
        return [int(p) if p.isdigit() else 0 for p in v.split(".")]
    x, y = parts(a), parts(b)
    width = max(len(x), len(y))
    return x + [0] * (width - len(x)) < y + [0] * (width - len(y))


class BedtimeBridge:
    """One paired phone: its latest plan, alarm events and the settings it reads back."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.state: dict[str, Any] = {**empty_state(), **plan_record(), **pending_record()}
        # v0.6.0 item 36: backstop recalculations run one at a time (they await the calendar).
        self._ladder_lock = asyncio.Lock()
        # None means "not set yet"; filled from the app's own settings on its first plan.
        self.settings: dict[str, int | None] = {}
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._bed_unsubs: dict[str, list[Callable[[], None]]] = {}
        # When you got back into bed, per kind, carried across the sensor's short dropouts.
        self._in_bed_since: dict[str, datetime] = {}
        # Command pushes still showing on the phone, cleared once the shortcut has run.
        self._pending_commands: set[str] = set()
        self._restore_unsub: Callable[[], None] | None = None
        self._charge_unsub: Callable[[], None] | None = None
        self._ladder_unsub: Callable[[], None] | None = None
        # v0.6.0 item 19 (Fable review): when an app-kept schedule's idle status should be looked at
        # again (the shift that made it has started), with no rungs to roll over from.
        self._app_recheck: datetime | None = None
        # v0.5.5 item 33: leave days read from the work calendar, {day: (leave, read at)}.
        self._leave_cache: dict[str, tuple[bool, datetime]] = {}
        # Phone shortcuts that reported Started and are expected to report Completed.
        self._command_unsubs: dict[str, Callable[[], None]] = {}
        # The leave checks still to send (item 17).
        self._leave_unsubs: list[Callable[[], None]] = []
        # Tonight's wind-down (v0.4.3 item 5).
        self.wind_down = WindDown(self)
        # Live Activities by Apple push (0.4.4).
        self.live = LiveActivities(self)
        # Mornings with no alarms (v0.4.6 item 2).
        self.awake = NoAlarmMorning(self)
        # Bedtime pending and Good night (v0.4.8 items 1 and 5).
        self.pending = BedtimePending(self)
        # Command pushes waiting for their shortcut to report it ran (v0.4.8 item 11).
        self._command_check_unsubs: dict[str, Callable[[], None]] = {}
        # Apple Maps' client keeps its access token between checks.
        self._maps: dtm.AppleMaps | None = None
        # RUN_MORNING_UPDATE timers for the morning Live Activity (v0.4.3 item 14).
        self._morning_unsubs: list[Callable[[], None]] = []
        # The weekly summary's timer (v0.4.3 item 15).
        self._weekly_unsub: Callable[[], None] | None = None
        # v0.5.2 item 3: a check 10 minutes after each alarm, for an alarm nobody answered.
        self._unanswered_unsubs: list[Callable[[], None]] = []
        # v0.5.3 item 4: replies to the last messages, by the app's message ID.
        self._replies: dict[str, dict[str, Any]] = {}
        # v0.5.3 item 35: Good Morning's resends, while it hasn't reported it ran.
        self._resend_unsubs: list[Callable[[], None]] = []
        # v0.5.4 item 60: the bedtime briefing's fallback, while tonight's plan hasn't come.
        self._briefing_unsub: Callable[[], None] | None = None
        self._resent = 0
        # v0.5.4 item 77: clears owed until the phone is reachable.
        self.clears = OwedClears(self)

    @property
    def raw_options(self) -> dict[str, Any]:
        """The options as stored (a made helper's own option keeps whatever was picked)."""
        return {**self.entry.data, **self.entry.options}

    @property
    def options(self) -> dict[str, Any]:
        """The options, with each helper Bedtime Bridge makes resolved to its own entity
        (v0.5.4 item 35)."""
        return made.resolve(self.hass, self.entry, self.raw_options)

    def is_made(self, option: str) -> bool:
        return made.source(self.raw_options, option) == made.SOURCE_MADE

    async def _set_helper(self, option: str, on: bool, blocking: bool = False) -> bool:
        """Turns a helper on or off by what it means: Awake For Day mirrored from an "Asleep"
        helper is turned off for awake. A binary sensor is never written."""
        entity_id = self.options.get(option)
        if not entity_id:
            return False
        if made.inverted(self.raw_options, option):
            on = not on
        if on:
            return await self._turn_on(entity_id, blocking=blocking)
        await self._turn_off(entity_id)
        return True

    async def _turn_off_made(self, *options: str) -> None:
        """Bedtime Bridge's own switches it resets (yours are left to your automations)."""
        for option in options:
            if self.is_made(option) and self._state_of(option) == "on":
                await self._turn_off(self.options[option])

    @callback
    def _door_opened(self, event: Event[EventStateChangedData]) -> None:
        """The made exit latch turns on when the bedroom door opens in the morning window (off
        again at bedtime). v0.5.4 item 56: a door opening in the night (before the first alarm,
        or long after yesterday's) doesn't latch, so the backstop still sees you in bed."""
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if new is None or new.state != "on" or (old is not None and old.state == "on"):
            return
        latch = self.options.get(CONF_EXIT_LATCH)
        if latch and self._state_of(CONF_EXIT_LATCH) != "on" and self._in_latch_window(dt_util.utcnow()):
            self.hass.async_create_task(self._turn_on(latch))

    def _in_latch_window(self, at: datetime) -> bool:
        """The morning window `_latched_this_morning` reads: from the first alarm (series_start)
        on, for EXIT_LATCH_WINDOW. With no alarms known, any time (as before)."""
        start = _time(self.state.get("series_start"))
        return start is None or start <= at < start + EXIT_LATCH_WINDOW

    def setting_keys(self) -> list[str]:
        """Built-in settings plus one lead time per configured shift type."""
        shifts = shift_types(self.options.get(CONF_SHIFT_TYPES))
        return list(SETTINGS) + list(LADDER_SETTINGS) + [f"{SHIFT_LEAD_PREFIX}{name}" for name in shifts]

    async def async_load(self) -> None:
        stored = await self._store.async_load() or {}
        self.state = {**empty_state(), **plan_record(), **pending_record(), **stored.get("state", {})}
        self.settings = stored.get("settings", {})
        timefmt.set_24h(bool(self.state.get("clock_24h")))

    @callback
    def async_start(self) -> None:
        """Watches the shift override and shift sensor for changes after a plan."""
        if CONF_LADDER_SHIFT_TYPES in self.options and not ladder_shift_types(self.options):
            # v0.6.0 item 36 (Fable review): cleared now means none, where it used to mean Days.
            _LOGGER.warning("Bedtime Bridge: no shift types are chosen for the backstops (Configure → Backstops), "
                            "so calendar shifts get none")
        watched = [e for e in (self.options.get(CONF_SHIFT_OVERRIDE), self.options.get(CONF_SHIFT_SENSOR)) if e]
        if watched:
            self.entry.async_on_unload(async_track_state_change_event(self.hass, watched, self._shift_changed))
        door = self.options.get(CONF_BEDROOM_DOOR)
        if door and self.is_made(CONF_EXIT_LATCH):
            self.entry.async_on_unload(async_track_state_change_event(self.hass, [door], self._door_opened))
        self.entry.async_on_unload(self._cancel_timers)
        # Picks up a re-arm or an override restore that was waiting when HA restarted.
        self._watch_bed("rearm")
        self._watch_bed("rerun")
        self._schedule_skip_restore()
        self._schedule_leave_checks()
        self._schedule_unanswered(catch_up=True)
        self._resume_briefing_fallback()
        # v0.5.3 item 7: a morning's hourly updates that were running carry on after a restart.
        if (until := _time(self.state.get("morning_updates_until"))) and until > dt_util.utcnow():
            self._schedule_morning_updates()
        for command in list(self.state.get("command_started") or {}):
            self._watch_command(command)
        # The ladder: recompute once loaded (keeping a stored one inside its lock), then check the
        # forecast every 15 minutes before leave-by.
        self.hass.async_create_task(self._recompute_ladder())
        self.entry.async_on_unload(async_track_time_interval(self.hass, self._weather_tick, timedelta(minutes=15)))
        self.wind_down.async_start()
        self.awake.async_start()
        self.pending.async_start()
        self.clears.async_start()
        presence = self.options.get(CONF_PRESENCE) or []
        if isinstance(presence, str):
            presence = [presence]
        if presence:
            self.entry.async_on_unload(async_track_state_change_event(self.hass, presence, self._morning_presence))
        self.entry.async_on_unload(self._cancel_morning_updates)
        self._schedule_weekly()
        self.entry.async_on_unload(lambda: self._weekly_unsub() if self._weekly_unsub else None)
        self.entry.async_on_unload(self.hass.bus.async_listen(EVENT_BACKSTOP_FIRED, self._backstop_fired))
    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"state": self.state, "settings": self.settings}, 1)

    async def async_flush(self) -> None:
        """v0.5.3 item 25: writes the state at once (on unload)."""
        await self._store.async_save({"state": self.state, "settings": self.settings})

    def _changed(self) -> None:
        self._save()
        async_dispatcher_send(self.hass, signal_update(self.entry.entry_id))

    def set_setting(self, key: str, value: int) -> None:
        self.settings[key] = value
        self._changed()
        if key in LADDER_SETTINGS:
            self.hass.async_create_task(self._recompute_ladder())

    def setting_value(self, key: str) -> int | None:
        """A setting's value, or the ladder setting's default."""
        value = self.settings.get(key)
        if value is None and key in LADDER_SETTINGS:
            return LADDER_SETTINGS[key][4]
        return value

    def settings_for_app(self) -> dict[str, Any]:
        """Settings with a value, shift leads grouped as {"Days": 60}."""
        flat: dict[str, Any] = {}
        leads: dict[str, int] = {}
        for key, value in self.settings.items():
            if value is None:
                continue
            if key.startswith(SHIFT_LEAD_PREFIX):
                leads[key.removeprefix(SHIFT_LEAD_PREFIX)] = int(value)
            elif key in SETTINGS:
                flat[key] = int(value)
        for key in LADDER_SETTINGS:
            flat[key] = int(self.setting_value(key))
        if leads:
            flat["shift_lead_minutes"] = leads
        return flat

    def _adopt_app_settings(self, app_settings: dict[str, Any]) -> None:
        """First plan seeds the HA numbers from the phone; after that HA wins."""
        for key in SETTINGS:
            if self.settings.get(key) is None and isinstance(app_settings.get(key), int):
                self.settings[key] = app_settings[key]
        for name, minutes in (app_settings.get("shift_lead_minutes") or {}).items():
            key = f"{SHIFT_LEAD_PREFIX}{name}"
            if self.settings.get(key) is None and isinstance(minutes, int):
                self.settings[key] = minutes

    # MARK: Messages from the app

    async def async_handle(self, message: dict[str, Any]) -> dict[str, Any]:
        """Handle one webhook message; the returned dict is sent back as JSON."""
        kind = message.get("type")
        data = message.get("data") or {}
        handler = {
            MSG_CONTEXT: self._context,
            MSG_FORECAST: self._forecast,
            MSG_PLANNED: self._planned,
            MSG_SNOOZED: self._snoozed,
            MSG_STOPPED: self._stopped,
            MSG_CANCELLED: self._cancelled,
            MSG_LEAVE_BY: self._leave_by,
            MSG_BEDTIME: self._bedtime,
            MSG_PLAN_FAILED: self._plan_failed,
            MSG_UP_CHECK: self._up_check,
            MSG_SKIPPED: self._skipped,
            MSG_UNSKIPPED: self._unskipped,
            MSG_TEMP_SNOOZED: self._temporary_snoozed,
            MSG_TEMP_STOPPED: self._temporary_stopped,
            MSG_HOME_CHECK: self._home_check,
            MSG_BEDTIME_UNDONE: self._bedtime_undone,
            MSG_COMMAND_RAN: self._command_ran,
            MSG_MORNING_CHANGED: self._morning_changed,
            MSG_NAP_STARTED: self._nap_started,
            MSG_NAP_ENDED: self._nap_ended,
            MSG_PREVIEW: self._preview,
            MSG_WIND_DOWN: self._wind_down_action,
            MSG_RESET: self._reset,
            MSG_LIVE_ACTIVITY: self._live_activity,
            MSG_AWAKE: self._awake,
            MSG_GOOD_NIGHT: self._good_night,
            MSG_REPORT: self._report,
            MSG_HEALTH: self._health,
            MSG_SET_SETTING: self._set_setting_from_app,
            MSG_PLAN_CURRENT: self._plan_current,
        }.get(kind)
        # v0.5.2 item 11: the app's version, checked on every message; every reply says ours.
        self._check_app_version(data)
        # v0.5.4 item 77: a message from the phone; owed clears go again (before this one's own).
        await self.clears.resend("app")
        versions = {"integration_version": INTEGRATION_VERSION, "min_app": MIN_APP_VERSION}
        if handler is None:
            return {"error": f"unknown message type: {kind}", **versions}
        # v0.5.3 item 4: a retried message (same ID, from a retry or the second URL) is applied
        # once; the repeat gets the first reply.
        message_id = data.get("message_id")
        if isinstance(message_id, str) and message_id:
            if message_id in self._replies:
                return {**self._replies[message_id], "duplicate": True, **versions}
        # v0.5.3 item 39: alarms that rang and weren't answered stay watched, whatever list
        # comes next, until answered or the morning is ended on purpose.
        watched_before = self._watched_alarms()
        # Every message may carry the app's wind-down settings (v0.4.3 item 5) and when this
        # install of the app stops opening (item 9).
        self.wind_down.adopt_settings(data.get("wind_down_settings"))
        # v0.5.5 item 16: fixed-time shifts' wake-ups, for the calendar-only fallback.
        self.wind_down.adopt_shift_wake(data.get("shift_wake"))
        # v0.5.5 item 33: the app's leave titles, for leave beating shifts.
        self.adopt_leave_titles(data.get("leave_titles"))
        self.awake.adopt_settings(data.get("awake_settings"))
        if isinstance(data.get("clock_24h"), bool) and data["clock_24h"] != self.state.get("clock_24h"):
            self.state["clock_24h"] = data["clock_24h"]
            timefmt.set_24h(data["clock_24h"])
            self._changed()
        if isinstance(data.get("notification_levels"), dict):
            self.state["notification_levels"] = {
                k: v for k, v in data["notification_levels"].items() if v in ("normal", "time_sensitive", "critical")
            }
        if isinstance(data.get("charge_warning"), dict):
            self.state["charge_warning"] = data["charge_warning"]
        # v0.5.3 item 8: refused unless valid.
        if (weekly_settings := validate.weekly(data.get("weekly_summary"))) and weekly_settings != self.state.get("weekly_settings"):
            self.state["weekly_settings"] = weekly_settings
            self._schedule_weekly()
        # v0.5.4 items 27 and 28: the briefings' parts and phrases, as Settings → Briefings has them.
        if (briefing_settings := brf.clean_settings(data.get("briefing_settings"))) is not None:
            self.state["briefing_settings"] = briefing_settings
        if isinstance(data.get("app_expires"), str) and data["app_expires"] != self.state.get("app_expires"):
            self.state["app_expires"] = data["app_expires"]
            self._changed()
        result = await handler(data) or {}
        if kind in (MSG_PLANNED, MSG_CANCELLED, MSG_SKIPPED, MSG_UNSKIPPED, MSG_BEDTIME_UNDONE):
            await self._recompute_ladder()
        if kind == MSG_PLANNED:
            self._schedule_leave_checks()
        elif kind in (MSG_CANCELLED, MSG_SKIPPED, MSG_BEDTIME_UNDONE):
            # The morning went: no more leave checks (item 17). A morning change keeps them
            # (v0.4.2 item 1, in _morning_changed).
            self.state["leave_reminders"] = []
            self._cancel_leave_checks()
        if kind == MSG_STOPPED and data.get("last", not self.state.get("alarm_times")):
            # The morning Live Activity after the last alarm of the wake-up (v0.4.11 item 6); the
            # phone says when it was the last, as tomorrow's alarms may already be set.
            self._schedule_morning_updates()
            if self.live.configured and self.state.get("morning_updates_until") and self.is_home():
                await self.live.morning()
        elif kind in (MSG_CANCELLED, MSG_SKIPPED, MSG_RESET) or (kind == MSG_MORNING_CHANGED and not data.get("alarm_times")):
            self._cancel_morning_updates()
            if self.live.configured:
                await self.live.end_morning()
        elif kind == MSG_LEAVE_BY and self.live.configured and self.state.get("morning_updates_until"):
            # A new leave-by from a drive re-check: the countdown follows.
            await self.live.morning()
        # v0.5.2 item 3: a snooze or stop answers the alarms rung so far; the checks follow the times.
        if kind in (MSG_SNOOZED, MSG_STOPPED) and not data.get("test") and not data.get("moved"):
            self._note_answer(kind, data)
        if kind == MSG_PLANNED and isinstance(data.get("alarm_times"), list):
            self.state["planned_alarm_times"] = [t for t in data["alarm_times"] if isinstance(t, str)]
        self._keep_unanswered(kind, data, watched_before)
        self._schedule_unanswered()
        if kind in (MSG_PLANNED, MSG_CANCELLED, MSG_SKIPPED, MSG_UNSKIPPED, MSG_MORNING_CHANGED):
            changed = kind == MSG_MORNING_CHANGED
            self.wind_down.refresh(
                moved=True if changed else bool(data.get("first_alarm_moved")),
                moved_reason=data.get("reason") if changed else data.get("moved_reason"),
            )
        reply = {
            "settings": self.settings_for_app(), "wind_down": self.wind_down.summary(),
            # Whether I'm awake is offered now (v0.4.6 item 2).
            "no_alarm_morning": self.awake.summary(),
            # The app skips its own leave reminders while home when the checks happen here.
            "drive_checks_here": bool(self._drive_services()),
            # v0.6.0 item 0.5.6-14: Home Assistant asks Google itself, so the phone's background
            # checks don't ask Google for the same morning.
            "drive_asks_google": self.asks_google(),
            # v0.4.8 item 2: the bedtime shortcut is running because of a push, so Plan Wake-Up
            # Alarms doesn't ask anything.
            "bedtime_by_push": self._bedtime_by_push(),
            **result,
            **versions,
        }
        if isinstance(message_id, str) and message_id:
            self._replies[message_id] = reply
            while len(self._replies) > REPLY_MEMORY:
                self._replies.pop(next(iter(self._replies)))
        return reply

    # MARK: v0.5.3 items 39-41: answers per alarm

    def _note_answer(self, kind: str, data: dict[str, Any]) -> None:
        """A snooze or stop. With an app that says which alarms it answered (`answered`, the set
        times, folded ones included) only those count; an older app's answer counts for every
        alarm rung so far, as before."""
        now = dt_util.utcnow()
        self.state["last_answer"] = now.isoformat()
        answered = data.get("answered")
        # Audit 3: decided per message, so a rolled-back app (no "answered") works as before.
        self.state["per_alarm"] = isinstance(answered, list)
        if isinstance(answered, list):
            answers = {k: v for k, v in (self.state.get("answers") or {}).items()
                       if (_time(k) or now) > now - timedelta(days=1)}
            for value in answered:
                if _time(value):
                    answers[value] = now.isoformat()
            self.state["answers"] = answers
        # Audit 1: a stop answers the snoozes still to ring (the app reports any snooze that's
        # still going in snoozed_until); only rings already past stay watched.
        if kind == MSG_STOPPED:
            self.state["snooze_rings"] = [r for r in (self.state.get("snooze_rings") or []) if (t := _time(r)) and t <= now]
        # v0.5.3 item 40: every snooze's ring-again is watched, not only the latest.
        if kind == MSG_SNOOZED and (rings := _time(data.get("rings_again"))):
            rings_list = [r for r in (self.state.get("snooze_rings") or []) if (_time(r) or now) > now - timedelta(hours=4)]
            self.state["snooze_rings"] = sorted({*rings_list, rings.isoformat()})

    def _answered(self, alarm: datetime) -> bool:
        """Whether a snooze or stop has answered this alarm since it rang."""
        last = _time(self.state.get("last_answer"))
        if not self.state.get("per_alarm"):
            return last is not None and last >= alarm
        for key, value in (self.state.get("answers") or {}).items():
            set_for, at = _time(key), _time(value)
            if set_for and at and abs((set_for - alarm).total_seconds()) < 60 and at >= alarm:
                return True
        # Not one of the alarms as planned (a snooze's ring, a snooze alarm): any answer after it.
        planned = [t for t in (_time(v) for v in self.state.get("planned_alarm_times") or []) if t]
        if not any(abs((t - alarm).total_seconds()) < 60 for t in planned):
            return last is not None and last >= alarm
        return False

    def _keep_unanswered(self, kind: str, data: dict[str, Any], watched_before: list[datetime]) -> None:
        """v0.5.3 item 39: an alarm that rang and wasn't answered stays watched when a message
        drops it from the list (one alarm ended while another still waits). Only ending the
        morning on purpose (the hold, cancelling, skipping, Undo bedtime, reset) lets it go."""
        # Audit 10: changing the morning (Wake up later, Back to bed, No alarms) is you answering.
        if kind in (MSG_CANCELLED, MSG_SKIPPED, MSG_RESET, MSG_BEDTIME_UNDONE, MSG_MORNING_CHANGED) or (kind == MSG_STOPPED and data.get("all")):
            self.state["pending_unanswered"] = []
            self.state["snooze_rings"] = []
            if kind == MSG_MORNING_CHANGED:
                self.state["last_answer"] = dt_util.utcnow().isoformat()
            return
        now = dt_util.utcnow()
        pending = {v for v in self.state.get("pending_unanswered") or []
                   if (t := _time(v)) and t > now - timedelta(hours=4) and not self._answered(t)}
        for alarm in watched_before:
            if alarm <= now and not self._answered(alarm):
                pending.add(alarm.isoformat())
        self.state["pending_unanswered"] = sorted(pending)

    async def _set_setting_from_app(self, data: dict[str, Any]) -> dict[str, Any]:
        """v0.5.3 item 48: the phone changed a setting this integration owns: it's set here, and
        the reply carries the settings as they now are."""
        key, value = data.get("key"), data.get("value")
        if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, (int, float)):
            return {"setting_error": "A setting needs a name and a number"}
        if key in SETTINGS:
            _, low, high, _step = SETTINGS[key]
        elif key in LADDER_SETTINGS:
            _, low, high, _step, _default = LADDER_SETTINGS[key]
        elif key.startswith(SHIFT_LEAD_PREFIX) and key in self.settings:
            low, high = 0, 600
        else:
            return {"setting_error": f"{key} isn't set in Home Assistant"}
        if not low <= value <= high:
            return {"setting_error": f"{key} must be between {low} and {high}"}
        self.set_setting(key, int(value))
        return {"setting_set": key}

    def _check_app_version(self, data: dict[str, Any]) -> None:
        """v0.5.2 item 11: an app older than this integration needs raises a repair issue ("Update
        Bedtime Bridge on your iPhone"); it goes once the app is new enough. An app that sends
        no version is from before the check (0.5.1 or older)."""
        app = data.get("app_version") if isinstance(data.get("app_version"), str) else APP_VERSION_BEFORE_CHECK
        if app != self.state.get("app_version") or data.get("min_integration") != self.state.get("app_min_integration"):
            self.state["app_version"] = app
            self.state["app_min_integration"] = data.get("min_integration")
            self._changed()
        if version_less(app, MIN_APP_VERSION):
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_APP_OUTDATED, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_APP_OUTDATED,
                translation_placeholders={
                    "device": str(data.get("device_name") or "your iPhone"), "app_version": app,
                    "min_app": MIN_APP_VERSION, "integration_version": INTEGRATION_VERSION,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_APP_OUTDATED)
        # v0.6.0 item 36: the app says which integration it needs.
        needs = data.get("min_integration")
        if isinstance(needs, str) and needs and version_less(INTEGRATION_VERSION, needs):
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_INTEGRATION_OUTDATED, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_INTEGRATION_OUTDATED,
                translation_placeholders={
                    "device": str(data.get("device_name") or "your iPhone"), "app_version": app,
                    "min_integration": needs, "integration_version": INTEGRATION_VERSION,
                },
            )
        elif isinstance(needs, str):
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_INTEGRATION_OUTDATED)

    async def _awake(self, data: dict[str, Any]) -> dict[str, Any]:
        """I'm awake from the app, Siri or a control: good morning now (v0.4.6 item 2)."""
        return await self.awake.good_morning(by_hand=True)

    def _bedtime_by_push(self) -> bool:
        pushed = _time(self.state.get("bedtime_push_at"))
        return pushed is not None and dt_util.utcnow() - pushed < BEDTIME_BY_PUSH

    async def _good_night(self, data: dict[str, Any]) -> dict[str, Any]:
        """Good night from the app's bar or Siri: RUN_BEDTIME (v0.4.8 item 5)."""
        return await self.pending.good_night()

    async def _report(self, data: dict[str, Any]) -> dict[str, Any]:
        """A testing report from the app (v0.4.8 items 9 and 11), with what Home Assistant
        knows added. Or an edit or delete of one you sent (v0.4.10 item 13)."""
        if "edits" in data or "deletes" in data:
            return await reports.change(self, data)
        return await reports.save(self, {**data, "source": data.get("source") or "app", "home_assistant": reports.snapshot(self)})

    async def _health(self, data: dict[str, Any]) -> dict[str, Any]:
        """For the app's bedtime health check (v0.4.8 item 14)."""
        notify = (self.options.get(CONF_NOTIFY) or "").strip()
        return {
            "alarm_times": self.state.get("alarm_times") or [],
            "ladder": (self.state.get("ladder") or {}).get("status"),
            "push_token": bool(((self.state.get("live_activity") or {}).get("device") or {}).get("token")),
            "phone_commands": bool(notify and self.options.get(CONF_PHONE_COMMANDS)),
            "bedtime_pending": self.pending.is_on,
        }

    # MARK: Plug in your phone (v0.4.3 item 10)

    def _battery(self) -> tuple[float | None, bool | None]:
        """The companion app's battery level and whether it's charging (Full counts)."""
        level_state = self.hass.states.get(self.options.get(CONF_PHONE_BATTERY) or "")
        charge_state = self.hass.states.get(self.options.get(CONF_PHONE_BATTERY_STATE) or "")
        try:
            level = float(level_state.state) if level_state else None
        except ValueError:
            level = None
        charging = None
        if charge_state is not None and charge_state.state not in UNKNOWN_STATES:
            charging = charge_state.state.lower() in ("charging", "full")
        return level, charging

    async def charge_check(self, level: Any = None, charging: Any = None, *, again: bool = True) -> None:
        """At bedtime (with the phone's own reading) and at go to bed: under the threshold and
        not charging → "Plug in your phone · alarms at 4:20", once more after 10 min if still
        not charging."""
        cfg = self.state.get("charge_warning") or {}
        if not cfg.get("enabled", True):
            return
        sensor_level, sensor_charging = self._battery()
        level = level if isinstance(level, (int, float)) else sensor_level
        charging = charging if isinstance(charging, bool) else sensor_charging
        if level is None or charging is None or charging or level >= int(cfg.get("threshold", 30)):
            return
        times = sorted(t for t in (_time(v) for v in self.state.get("alarm_times") or []) if t and t > dt_util.utcnow())
        alarms = f" · alarms at {timefmt.short(times[0])}" if times else ""
        await self.wind_down._notify("Plug in your phone", f"Plug in your phone{alarms}", tag="charge", time_sensitive=True)
        if again:
            # v0.6.0 item 36: kept, so unloading cancels it.
            if self._charge_unsub:
                self._charge_unsub()
            self._charge_unsub = async_call_later(self.hass, timedelta(minutes=10), callback(
                lambda _now: self._charge_again()))

    @callback
    def _charge_again(self) -> None:
        self._charge_unsub = None
        self.hass.async_create_task(self.charge_check(again=False))

    # MARK: Morning Live Activity (v0.4.3 item 14)

    @callback
    def _schedule_morning_updates(self) -> None:
        """After the last alarm's stop on a leave-by morning: RUN_MORNING_UPDATE every hour until 15 min
        after leave-by, while home."""
        until = _time(self.state.get("morning_updates_until"))
        if self._morning_unsubs and until is not None and until > dt_util.utcnow():
            return
        # Last morning's timers have all fired: start over.
        self._cancel_morning_updates()
        leave_by = _time(self.state.get("leave_by"))
        now = dt_util.utcnow()
        end = leave_by + timedelta(minutes=LEAVE_CHECK_TAIL_MINUTES) if leave_by else None
        if end is None or end <= now:
            return
        self.state["morning_updates_until"] = end.isoformat()
        when = now + timedelta(hours=1)
        while when < end:
            self._morning_unsubs.append(async_track_point_in_utc_time(self.hass, self._morning_update_due, when))
            when += timedelta(hours=1)
        # The last one ends it.
        self._morning_unsubs.append(async_track_point_in_utc_time(self.hass, self._morning_update_due, end))

    @callback
    def _cancel_morning_updates(self) -> None:
        for unsub in self._morning_unsubs:
            unsub()
        self._morning_unsubs = []
        self.state["morning_updates_until"] = None

    @callback
    def _morning_update_due(self, _now: datetime) -> None:
        if not self.is_home():
            return
        if self.live.configured:
            # By push: refreshed (or ended, 15 min after leave-by) without the app.
            self.hass.async_create_task(self.live.morning())
        else:
            self.hass.async_create_task(self._command(CMD_MORNING_UPDATE))

    @callback
    def _morning_presence(self, event: Event[EventStateChangedData]) -> None:
        """Left home during the morning: "On your way" until the event starts (v0.4.11 item 20),
        and no more leave reminders. Only a change of state counts, not a location update."""
        new, old = event.data.get("new_state"), event.data.get("old_state")
        if new is None or old is None or new.state == old.state:
            return
        now = dt_util.utcnow()
        if new.state in HOME_STATES and old.state not in UNKNOWN_STATES:
            self._back_home(now)
            return
        if old.state not in HOME_STATES or new.state in UNKNOWN_STATES or not self._leaving_window(now):
            return
        self.state["departed"] = now.isoformat()
        self.state["leave_reminders"] = []
        self._cancel_leave_checks()
        self._cancel_morning_updates()
        event_start = self._event_start()
        if self.live.configured and event_start is not None:
            self.hass.async_create_task(self.live.on_the_way(event_start))
        elif self.live.configured:
            self.hass.async_create_task(self.live.end_morning())
        else:
            self.hass.async_create_task(self._command(CMD_MORNING_UPDATE))
        self._changed()

    def _leaving_window(self, now: datetime) -> bool:
        """Between the first alarm and the event's start, on a morning with a trip."""
        leave_by = _time(self.state.get("leave_by"))
        if leave_by is None or self.state.get("remote"):
            return False
        first = _time(self.state.get("series_start")) or min(
            (t for t in (_time(v) for v in self.state.get("alarm_times") or []) if t), default=None)
        start = self._event_start() or leave_by + timedelta(minutes=LEAVE_CHECK_TAIL_MINUTES)
        return (first is None or first - timedelta(minutes=1) <= now) and now < start and now > leave_by - timedelta(hours=4)

    def _event_start(self) -> datetime | None:
        ladder = self.state.get("ladder") or {}
        return _time(ladder.get("shift_start")) or _time(ladder.get("appointment_start"))

    @callback
    def _back_home(self, now: datetime) -> None:
        """Back before the event (forgot something): Leave in or Past leave time again."""
        departed = _time(self.state.get("departed"))
        start = self._event_start()
        if departed is None or (start is not None and now >= start):
            return
        self.state["departed"] = None
        self._changed()
        if self.live.configured:
            self.hass.async_create_task(self.live.morning())

    # MARK: Weekly summary (v0.4.3 item 15)

    @callback
    def _schedule_weekly(self) -> None:
        if self._weekly_unsub:
            self._weekly_unsub()
            self._weekly_unsub = None
        when = weekly.next_run(self.state.get("weekly_settings") or {}, dt_util.utcnow())
        if when is not None:
            self._weekly_unsub = async_track_point_in_utc_time(
                self.hass, callback(lambda _now: self.hass.async_create_task(self.send_weekly_summary())), when)

    async def send_weekly_summary(self) -> dict[str, Any]:
        """The week, as a sensor and a notification that opens the app's summary screen."""
        # The week that just ended, cut at the setting's day and time (v0.4.6 item 5).
        now = dt_util.utcnow()
        cutoff = weekly.last_cutoff(self.state.get("weekly_settings") or {}, now)
        summary = weekly.summarize(self.state.get("history") or [], self.state.get("wind_down_history") or [], now,
                                   since=weekly.week_start(cutoff), until=cutoff)
        summary["vacation"] = weekly.vacation_text(self.state.get("vacations"), weekly.week_start(cutoff), cutoff)
        self.state["weekly_summary"] = summary
        self._changed()
        self._schedule_weekly()
        # On vacation: the summary is there, without the notification (v0.4.7 item 6f).
        if weekly.on_vacation(self.state.get("vacations"), now):
            return summary
        await self.wind_down._notify("Your week", summary["text"] or "Nothing tracked this week.", tag="weekly",
                                     url="bedtimebridge://summary")
        return summary

    async def bedtime_started(self, data: dict[str, Any]) -> None:
        """Bedtime ran (the bedtime shortcut's Report Shortcut Ran RUN_BEDTIME, or Start Bedtime
        Routine): wind-down ends (v0.4.3 item 5), the expiry warning (item 9) and Plug in your
        phone (item 10), each once a night. Bedtime is no longer pending (v0.4.8 item 1)."""
        self.pending.bedtime_started()
        self._changed()
        # v0.5.4 item 77: Backstops' Notification tags to clear (e.g. the Nudge's RUN_BEDTIME).
        # Item 81: not wake_backstop, which may be this morning's (the backstop restart clears it).
        await self._clear_tags(bedtime=True)
        # v0.5.4 item 35: Bedtime Bridge's own switches start the night off (yours are yours).
        await self._turn_off_made(CONF_SNOOZE_HELPER, CONF_STOP_HELPER, CONF_EARLY_HELPER, CONF_AWAKE_HELPER, CONF_EXIT_LATCH)
        await self.wind_down.bedtime_ran()
        await self._warn_app_expiry()
        # v0.5.4 item 28: built and announced now when tonight's plan is already in, else when it
        # comes (in _planned). v0.5.4 item 60: not built from last morning's data before then; with
        # no plan in 2 minutes, it goes out with what's known.
        planned = _time(self.state.get("last_plan"))
        if planned is not None and dt_util.utcnow() - planned < BRIEFING_PLAN_FRESH:
            await self.bedtime_briefing()
        else:
            self._schedule_briefing_fallback()
        today = dt_util.as_local(dt_util.utcnow()).date().isoformat()
        if self.state.get("charge_checked_night") != today:
            self.state["charge_checked_night"] = today
            await self.charge_check(data.get("battery_level"), data.get("battery_charging"))

    async def _warn_app_expiry(self) -> None:
        """At bedtime on each of the last 2 days before the app stops opening (v0.4.3 item 9)."""
        expires = _time(self.state.get("app_expires"))
        now = dt_util.utcnow()
        today = dt_util.as_local(now).date().isoformat()
        if expires is None or not now < expires <= now + timedelta(days=2) or self.state.get("app_expiry_warned") == today:
            return
        self.state["app_expiry_warned"] = today
        when = timefmt.day_long(expires)
        await self.wind_down._notify("Reinstall Bedtime Bridge", f"The app stops opening {when}. Reinstall it from Xcode (⌘R); settings are kept.",
                                     tag="app_expiry")

    async def _reset(self, data: dict[str, Any]) -> dict[str, Any]:
        """Reset alarms and backstops to nothing set (v0.4.3 item 7a): clears the alarm state, the
        ladder's delay and weather moves, pending leave checks and phone commands, turns the
        napping and winding-down helpers off, then recalculates the ladder from the calendar
        alone (armed again if a covered shift is on it). Settings, the override, Sleep In and
        Awake For Day are left as they are."""
        before = dict(self.state.get("ladder") or {})
        self.state.update(empty_state())
        self.pending.cleared()
        self.state["last_morning_change"] = None
        self.state["ladder"] = {}
        self.state["ladder_plan"] = self._app_ladder_plan()
        self.state["ladder_delay"] = None
        self.state["nap_alarm_times"] = []
        self.state["leave_reminders"] = []
        self._cancel_leave_checks()
        for command in list(self._pending_commands):
            await self._clear_command(command)
        for command in list(self._command_unsubs):
            self._unwatch_command(command)
        self.state["command_started"] = {}
        for option in (CONF_NAP_HELPER, CONF_WIND_DOWN_HELPER):
            helper = self.options.get(option)
            state = self.hass.states.get(helper or "")
            if helper and state is not None and state.state == "on":
                await self._turn_off(helper)
        self.wind_down.cancel_timers()
        self.state["wind_down"] = {"status": "off"}
        await self._recompute_ladder(passed_before=before)
        self.wind_down.async_reschedule()
        self._event(MSG_RESET)
        return {}

    async def _live_activity(self, data: dict[str, Any]) -> dict[str, Any]:
        return self.live.handle(data)

    async def _preview(self, data: dict[str, Any]) -> dict[str, Any]:
        """The phone's Preview tomorrow (RUN_PLAN_PREVIEW): sets nothing, gives the wind-down
        tomorrow's first alarm."""
        await self._clear_command(CMD_PLAN_PREVIEW)
        self.wind_down.preview_answered(data)
        return {}

    async def _plan_current(self, data: dict[str, Any]) -> dict[str, Any]:
        """v0.5.5 item 16: the check before wind-down found nothing changed since the evening plan."""
        self.wind_down.plan_current(data)
        return {}

    async def _wind_down_action(self, data: dict[str, Any]) -> dict[str, Any]:
        """From the app, a control or Siri: start, cancel, later, stop_reminders or bed_later."""
        action = data.get("action")
        if action == "start":
            return {"started": await self.wind_down.start_now()}
        if action == "cancel":
            await self.wind_down.cancel()
        elif action == "later":
            await self.wind_down.later()
        elif action == "stop_reminders":
            self.wind_down.stop_reminders()
        elif action == "bed_later":
            return await self.wind_down.bed_later(int(data.get("minutes") or 15))
        return {}

    async def _context(self, data: dict[str, Any]) -> dict[str, Any]:
        """Shift override and HA's next shift, read before the app plans."""
        override = self.hass.states.get(self.options.get(CONF_SHIFT_OVERRIDE) or "")
        sensor = self.hass.states.get(self.options.get(CONF_SHIFT_SENSOR) or "")
        return {
            "shift_override": override.state if override else None,
            "next_shift_start": sensor.attributes.get("next_shift_start") if sensor else None,
            "next_shift_type": sensor.attributes.get("next_shift_type") if sensor else None,
            "at_home": self.is_home(),
            # For item 3's confirm screen: the backstops now, and when the curtains open on the
            # morning of the first new alarm (Sleep In's rule).
            "ladder": self._ladder_summary(),
            # The override or HA's next shift changed after the plan (v0.4.1 item 10).
            "plan_outdated": bool(self.state.get("plan_outdated")),
            "outdated_reason": self.state.get("outdated_reason"),
            # v0.4.11 item 33: Sleep In, for the app's in-bed card (None when not set up).
            "sleep_in": (lambda s: None if s in (None, "unknown", "unavailable") else s == "on")(self._state_of(CONF_SLEEP_IN)),
            # v0.4.11 item 26: the backstops that went off this morning (the app tags them).
            "backstops_fired": list(self.state.get("backstops_fired") or []),
            "wind_down": self.wind_down.summary(),
            "weekly_summary": self.state.get("weekly_summary"),
            # This week so far and up to 8 earlier weeks, newest first (v0.4.6 item 5).
            "weekly_summaries": weekly.weeks(self.state.get("history") or [], self.state.get("wind_down_history") or [],
                                             self.state.get("weekly_settings") or {}, dt_util.utcnow(), self.state.get("vacations")),
            "curtains_open_at": self._curtains_open(_time(data.get("first_alarm")) or dt_util.utcnow()),
        }

    def _ladder_summary(self) -> dict[str, Any]:
        ladder = self.state.get("ladder") or {}
        summary = {key: ladder.get(key) for key in (
            "status", "reason", "kind", "times", "shift_start", "appointment", "delay_minutes", "leave_by", "first_day_back",
        )}
        summary["house_backstop"] = self.house_backstop()
        return summary

    def house_backstop(self) -> dict[str, Any]:
        """What the house backstop fires on (item 8): your own rung sensors (setups from before
        the ladder), or this ladder."""
        source = self.options.get(CONF_HOUSE_BACKSTOP) or HOUSE_OLD
        if source != HOUSE_OLD:
            return {"source": source, "times": (self.state.get("ladder") or {}).get("times") or {}}
        times: dict[str, str] = {}
        for rung, entity_id in zip(("early", "deadline", "late"), self.options.get(CONF_OLD_RUNGS) or []):
            state = self.hass.states.get(entity_id)
            when = _time(state.state) if state else None
            if when:
                times[rung] = when.isoformat()
        return {"source": HOUSE_OLD, "times": times}

    def _curtains_open(self, on: datetime) -> str | None:
        """Sunrise + the curtain offset on the local day of `on`."""
        sun = self.hass.states.get("sun.sun")
        rising = _time(sun.attributes.get("next_rising")) if sun else None
        if rising is None:
            return None
        # Moved by days in local time, so a clock change between doesn't put it an hour out
        # (v0.4.9 item 7).
        local = dt_util.as_local(rising)
        rising = dt_util.as_utc(datetime.combine(dt_util.as_local(on).date(), local.time(), local.tzinfo))
        offset = timedelta(minutes=int(self.options.get(CONF_CURTAIN_OFFSET) or DEFAULT_CURTAIN_OFFSET))
        return (rising + offset).isoformat()

    async def _forecast(self, data: dict[str, Any]) -> dict[str, Any]:
        weather = self.options.get(CONF_WEATHER)
        state = self.hass.states.get(weather or "")
        if state is None:
            return {"forecast": [], "temperature_unit": None}
        response = await self.hass.services.async_call(
            "weather",
            "get_forecasts",
            {"entity_id": weather, "type": "hourly"},
            blocking=True,
            return_response=True,
        )
        forecast = (response or {}).get(weather, {}).get("forecast", [])
        return {
            "forecast": [
                {
                    "datetime": str(hour.get("datetime")),
                    "condition": hour.get("condition"),
                    "temperature": hour.get("temperature"),
                }
                for hour in forecast
            ],
            "temperature_unit": state.attributes.get("temperature_unit"),
        }

    async def _planned(self, data: dict[str, Any]) -> None:
        self._adopt_app_settings(data.get("app_settings") or {})
        if self.state.get("snooze_count"):
            # A morning that never got its last stop: the next plan closes it (item 5).
            self._end_morning()
        self.state.update(empty_state())
        self.state.update({key: data.get(key) for key in PLAN_FIELDS if key in data})
        self.state["last_plan"] = dt_util.utcnow().isoformat()
        self.state["plan_error"] = None
        self.state["ladder_plan"] = data.get("ladder") or {}
        if isinstance(data.get("duty_nights"), list):
            # v0.4.11 item 75: kept across plans and cancels, for nights the phone can't reach us.
            self.state["duty_nights"] = [str(d) for d in data["duty_nights"]]
        self.state["leave_reminders"] = [int(m) for m in data.get("leave_reminders") or []]
        self.state["departed"] = None
        # Where and when the morning's drive arrives, for drive checks here (0.4.4).
        self.state["trip"] = data.get("trip") if isinstance(data.get("trip"), dict) else None
        # v0.5.4 items 27 and 28: what the app knows about the morning, for the briefings.
        if isinstance(data.get("briefing"), dict):
            self.state["briefing_facts"] = data["briefing"]
        # v0.5.4 item 28: the plan made at bedtime (the bedtime shortcut reports Started before it
        # plans): the bedtime briefing with these alarms.
        started = _time(self.state.get("bedtime_started_at"))
        if started is not None and dt_util.utcnow() - started < BRIEFING_AFTER_BEDTIME:
            self._cancel_briefing_fallback()
            await self.bedtime_briefing()
        self._check_alarms_set(data)
        if (data.get("ladder") or {}).get("work_buffer_minutes") is not None:
            self.state["work_buffer_minutes"] = int(data["ladder"]["work_buffer_minutes"])
        # Bedtime ran again (by hand, or the nudge): no need to wait for you to get into bed.
        self._end_bed("rerun")
        self.state["planned_override"] = self._state_of(CONF_SHIFT_OVERRIDE)
        self.state["planned_shift_start"] = self._next_shift_start()
        times = sorted(t for t in (_time(v) for v in self.state.get("alarm_times") or []) if t)
        self.state["series_start"] = times[0].isoformat() if times else None
        await self._sync_sleep_in()
        # v0.4.7 item 6: Sleep In on for a vacation morning with no alarms; the vacations for the
        # weekly summary.
        if data.get("vacation") and not times:
            await self._set_sleep_in(True)
        if isinstance(data.get("vacations"), list):
            self.state["vacations"] = data["vacations"]
        # The phone reports a plan only once its alarms are scheduled, so with app alarms this
        # means they're really set; in Clock app mode the shortcut still has to make them.
        has_alarm = data.get("has_alarm", bool(data.get("alarm_times")))
        if not has_alarm:
            outcome = "no_alarm"
        elif data.get("alarm_style") == "clock":
            outcome = "planned_for_clock"
        else:
            outcome = "alarms_set"
        # v0.4.8 item 1: alarms set, so getting into bed is bedtime until bedtime runs.
        if outcome == "alarms_set":
            self.pending.alarms_set()
        else:
            self.pending.cleared()
        self._event(MSG_PLANNED, last_event=outcome, alarms_set=outcome == "alarms_set")

    async def _up_check(self, data: dict[str, Any]) -> dict[str, Any]:
        """Are you really up? The phone asks on Stop before cancelling the rest of the alarms."""
        return {"up": self.is_up()}

    def is_up(self) -> bool | None:
        """True: the bedroom door was opened (exit latch on) or the bed has been empty for over
        a minute. False: still in bed (or the bed reads unavailable). None: neither is set up.
        Stopping the first alarm and falling back asleep is exactly what this guards against,
        so anything uncertain counts as not up."""
        latch = self.hass.states.get(self.options.get(CONF_EXIT_LATCH) or "")
        bed = self.hass.states.get(self.options.get(CONF_BED_SENSOR) or "")
        if latch is None and bed is None:
            return None
        if latch is not None and latch.state == "on" and self._latched_this_morning(latch.last_changed):
            return True
        if bed is not None and bed.state == "off":
            return (dt_util.utcnow() - bed.last_changed).total_seconds() >= BED_EMPTY_SECONDS
        return False

    def _latched_this_morning(self, latched_at: datetime) -> bool:
        """The door only proves you're up if it opened once the alarms began: a trip out for
        the trash (a temporary alarm, before them) leaves the latch on but you back in bed."""
        start = _time(self.state.get("series_start"))
        return start is None or latched_at >= start

    async def _plan_failed(self, data: dict[str, Any]) -> None:
        """The phone couldn't plan. The previous plan's alarms may still be set."""
        self.state["plan_error"] = data.get("error") or "unknown error"
        self._event(MSG_PLAN_FAILED, error=self.state["plan_error"])

    # MARK: Leave-by backstop ladder (item 6)

    def _ladder_offsets(self) -> ldr.Offsets:
        """The settings, or the last valid ladder when they cross (early > deadline > late)."""
        offsets = ldr.Offsets(
            early=int(self.setting_value("rung_early_minutes")),
            deadline=int(self.setting_value("rung_deadline_minutes")),
            late=int(self.setting_value("rung_late_minutes")),
            first_day_back=int(self.setting_value("first_day_back_minutes")),
        )
        if offsets.valid:
            self.state["ladder_offsets"] = offsets.as_dict()
            return offsets
        _LOGGER.warning(
            "Bedtime Bridge ladder settings cross (early %s, deadline %s, late %s); keeping the last valid ladder",
            offsets.early, offsets.deadline, offsets.late,
        )
        return ldr.Offsets.from_dict(self.state.get("ladder_offsets"))

    @staticmethod
    def _ladder_locked(ladder: dict[str, Any], now: datetime) -> bool:
        """From the early rung until 60 minutes after the late rung, nothing recomputes it."""
        if ladder.get("status") != ldr.STATUS_ARMED:
            return False
        times = [t for t in (_time(v) for v in (ladder.get("times") or {}).values()) if t]
        # The first and last rung (your own alarm's preview can leave one out, item 18).
        return bool(times) and min(times) <= now < max(times) + ldr.LADDER_TAIL

    async def _recompute_ladder(self, passed_before: dict[str, Any] | None = None) -> None:
        """Works out the ladder for the next morning with a leave-by. Inside the morning lock it
        stays as it is; item 3 and worse weather or traffic move it through their own paths.
        v0.6.0 item 36: one at a time (a calendar that doesn't answer in a minute leaves the
        ladder as it was). `passed_before` (Reset): the ladder before it; a rung that had already
        passed for the same morning stays passed instead of coming back at now + 60 s.
        """
        async with self._ladder_lock:
            try:
                async with asyncio.timeout(60):
                    await self._recompute_ladder_now(passed_before)
            except TimeoutError:
                _LOGGER.warning("Bedtime Bridge: the backstops weren't recalculated (the calendar didn't answer); kept as they were")

    async def _recompute_ladder_now(self, passed_before: dict[str, Any] | None) -> None:
        now = dt_util.utcnow()
        ladder = self.state.get("ladder") or {}
        if self._ladder_locked(ladder, now):
            self._schedule_ladder_rollover()
            return
        offsets = self._ladder_offsets()
        morning, status, reason = await self._ladder_morning(now, offsets)
        if morning is None:
            if status == "keep":
                if ladder:
                    self._schedule_ladder_rollover()
                    return
                status, reason = ldr.STATUS_FAULT, reason
            self._set_ladder({"status": status, "reason": reason, "morning": None, "times": {}}, None)
            return
        delay = self._ladder_delay_for(morning.key)
        earlier = ladder.get("earlier_minutes", 0) if ladder.get("morning") == morning.key else 0
        if morning.fixed_times:
            shift_by = timedelta(minutes=delay.get("minutes", 0) - earlier)
            base = {rung: when + shift_by for rung, when in morning.fixed_times.items()}
        else:
            base = ldr.rung_times(morning.leave_by, offsets, morning.first_day_back, delay.get("minutes", 0) - earlier)
        floor = self.state.get("ladder_early_floor") or {}
        if floor.get("morning") == morning.key and _time(floor.get("at")):
            base["early"] = max(base["early"], _time(floor["at"]))
        times = ldr.not_in_past(base, now)
        if passed_before and passed_before.get("status") == ldr.STATUS_ARMED and passed_before.get("morning") == morning.key:
            # Fable review: only a rung that had passed before Reset stays passed; one the
            # recalculation moved earlier still comes (now + 60 s), as before.
            for rung, old in (passed_before.get("times") or {}).items():
                if (was := _time(old)) and was <= now and rung in base:
                    times[rung] = min(base[rung], was)
        self._set_ladder({
            "status": ldr.STATUS_ARMED,
            "reason": None,
            "morning": morning.key,
            "kind": morning.kind,
            "times": {rung: when.isoformat() for rung, when in times.items()},
            "leave_by": morning.leave_by.isoformat(),
            "leave_by_source": morning.leave_by_source,
            "first_day_back": morning.first_day_back,
            "shift_start": morning.shift_start.isoformat() if morning.shift_start else None,
            "appointment": morning.appointment,
            "appointment_start": morning.appointment_start.isoformat() if morning.appointment_start else None,
            "replaces_shift": morning.replaces_shift,
            "firing": morning.firing,
            "delay_minutes": delay.get("minutes", 0),
            "changed_by": delay.get("changed_by"),
            "change_reason": delay.get("reason"),
            "weather_extra_minutes": morning.weather_extra_minutes + (ladder.get("weather_minutes", 0) if earlier else 0),
            "condition": (ladder.get("condition") if earlier else None) or morning.condition,
            "earlier_minutes": earlier,
            "weather_minutes": ladder.get("weather_minutes", 0) if earlier else 0,
            "traffic_minutes": ladder.get("traffic_minutes", 0) if earlier else 0,
        }, morning.key)

    def _set_ladder(self, ladder: dict[str, Any], morning: str | None) -> None:
        """Stores the ladder; saves and updates the entities only when it changed."""
        changed = ladder != self.state.get("ladder")
        if (ladder.get("status") == ldr.STATUS_ARMED
                and ladder.get("morning") != (self.state.get("ladder") or {}).get("morning")):
            # v0.5.4 item 81: the backstops armed for a new morning; their tags may come again.
            self.clears.drop_backstop_tags()
        self.state["ladder"] = ladder
        self._ladder_cleanup(morning)
        self._schedule_ladder_rollover()
        if changed:
            self._changed()

    async def _ladder_morning(self, now: datetime, offsets: ldr.Offsets) -> tuple[ldr.Morning | None, str | None, str | None]:
        """The morning the ladder is for, or why there isn't one ("keep": hold the stored ladder)."""
        plan = self.state.get("ladder_plan") or {}
        shift_morning: ldr.Morning | None = None
        status, reason = ldr.STATUS_NO_SHIFT, "No shift on the calendar"

        def finished(morning: ldr.Morning) -> bool:
            delay = self._ladder_delay_for(morning.key).get("minutes", 0)
            if morning.fixed_times:
                late = max(morning.fixed_times.values()) + timedelta(minutes=delay)
            else:
                late = ldr.rung_times(morning.leave_by, offsets, morning.first_day_back, delay)["late"]
            return late + ldr.LADDER_TAIL <= now

        sensor = self.hass.states.get(self.options.get(CONF_SHIFT_SENSOR) or "")
        if plan.get("schedule") == "app":
            # v0.6.0 item 19: the schedule is kept in the app (My week, a rotation): its workdays
            # come with the plan, with days off and leave already left out.
            shift_morning, status, reason = await self._app_schedule_morning(now, plan, finished)
        elif sensor is None or sensor.state in UNKNOWN_STATES:
            status, reason = "keep", "Shift sensor unavailable"
        else:
            next_start = _time(sensor.attributes.get("next_shift_start"))
            shift_type = sensor.attributes.get("effective_shift") or sensor.attributes.get("next_shift_type")
            covered = self._ladder_types()
            if self._state_of(CONF_SHIFT_OVERRIDE) == "Off":
                status, reason = ldr.STATUS_OVERRIDE_OFF, "Shift override is Off"
            elif sensor.attributes.get("deadline_source") == "leave":
                status, reason = ldr.STATUS_LEAVE, "Leave day"
            elif next_start is None:
                status, reason = ldr.STATUS_NO_SHIFT, "No shift on the calendar"
            elif await self._on_leave(next_start):
                # v0.5.5 item 33: leave beats every shift on its days, duty included, whatever the
                # shift sensor's deadline source says. Wake-up events and flights keep theirs.
                status, reason = ldr.STATUS_LEAVE, "Leave day"
            elif str(shift_type or "").lower() not in covered:
                status, reason = ldr.STATUS_UNCOVERED, f"{shift_type or 'This shift'} mornings have no backstops"
            elif (plan.get("duty_night") and ldr.same_morning(_time(plan.get("shift_start")), next_start)) or self.duty_morning(next_start):
                # v0.4.11 item 75: the morning after duty you're already at work.
                status, reason = ldr.STATUS_DUTY, "No backstops: the morning after duty, at work"
            else:
                shift_morning = await self._shift_morning(next_start, plan)
                if shift_morning.leave_by_source == "remote" and not shift_morning.fixed_times:
                    shift_morning = None
                    status, reason = ldr.STATUS_REMOTE, "No backstops: the remote shift starts at its last alarm"
        own: ldr.Morning | None = None
        own_off = False
        last_alarm = _time(plan.get("last_alarm"))
        if plan.get("type") == "own" and last_alarm:
            # Your own alarm on a day off or leave (item 14): off unless switched on.
            if plan.get("on"):
                own = ldr.Morning(
                    "own", last_alarm, "plan", False,
                    fixed_times=ldr.own_times(last_alarm, plan.get("offsets"), plan.get("times")),
                    firing=plan.get("firing") or "gentle",
                )
            elif last_alarm + timedelta(minutes=int((plan.get("offsets") or {}).get("late") or 45)) + ldr.LADDER_TAIL > now:
                own_off = True
        appointment: ldr.Morning | None = None
        if plan.get("type") == "appointment" and _time(plan.get("leave_by")):
            appointment = ldr.Morning(
                "appointment", _time(plan["leave_by"]), "plan", False,
                appointment=plan.get("appointment"), appointment_start=_time(plan.get("appointment_start")),
                weather_extra_minutes=int(plan.get("weather_extra_minutes") or 0), condition=plan.get("condition"),
            )

        if appointment and shift_morning and plan.get("replaces_shift") and ldr.same_morning(
            appointment.appointment_start or appointment.leave_by, shift_morning.shift_start
        ):
            # A "wake up" appointment replaces the shift for that morning (v0.4.1 item 11).
            appointment.replaces_shift = plan.get("replaces_shift")
            replaced = shift_morning.shift_start
            shift_morning = None
            if plan.get("schedule") == "app":
                # v0.6.0 item 19 (Fable review): the next workday after it, so nothing reads
                # "armed" with no morning once the appointment is over.
                shift_morning, status, reason = await self._app_schedule_morning(now, plan, finished, skip=replaced)
        if shift_morning and finished(shift_morning):
            # current_shift still shows the shift that just passed (it polls): read the next one
            # from the work calendar, so the status goes straight to the next state (item 19).
            shift_morning, status, reason = await self._next_shift_morning(shift_morning.shift_start, plan, finished)
        open_mornings = [m for m in (shift_morning, appointment, own) if m and not finished(m)]
        if open_mornings:
            return min(open_mornings, key=lambda m: m.leave_by), None, None
        if own_off:
            return None, ldr.STATUS_OWN_OFF, "Backstops are off for your own alarm"
        # A finished appointment morning falls through to the shift branch's own status: never a
        # standing "done" (item 19), and never "armed" with no morning.
        if status == ldr.STATUS_ARMED:
            status, reason = ldr.STATUS_NO_SHIFT, "No shift on the calendar"
        return None, status, reason

    def _ladder_types(self) -> list[str]:
        """The shift types the backstops cover, lowercased. v0.6.0 item 36: cleared stays cleared;
        only never set falls back to the default."""
        return [t.lower() for t in ladder_shift_types(self.options)]

    def _app_covered(self, shift_type: str | None, start: datetime) -> bool:
        """v0.6.0 item 19: whether an app-kept shift gets backstops. One of the shift types chosen
        for the ladder does. Fable review: a name that isn't one of the integration's own shift
        types (My week's "Work", a rotation's own names) does when it starts before noon, as the
        options are saved on every install, so "unset" can't be relied on."""
        covered = self._ladder_types()
        name = str(shift_type or "").lower()
        if name in covered:
            return True
        known = [t.lower() for t in shift_types(self.options.get(CONF_SHIFT_TYPES))]
        return name not in known and dt_util.as_local(start).hour < 12

    def _app_ladder_plan(self) -> dict[str, Any]:
        """v0.6.0 item 19 (Fable review): an emptied ladder plan keeps an app-kept schedule, so the
        next workday still arms after an own alarm is cancelled or skipped."""
        plan = self.state.get("ladder_plan") or {}
        return {key: plan[key] for key in ("schedule", "next_shifts") if key in plan}

    async def _app_schedule_morning(
        self, now: datetime, plan: dict[str, Any], finished: Callable[[ldr.Morning], bool], skip: datetime | None = None
    ) -> tuple[ldr.Morning | None, str, str]:
        """v0.6.0 item 19: the next app-kept workday's morning, from the plan's `next_shifts` (and
        the planned shift, if the app planned one that isn't in it). `skip`: a start an appointment
        replaced."""
        self._app_recheck = None
        if self._state_of(CONF_SHIFT_OVERRIDE) == "Off":
            return None, ldr.STATUS_OVERRIDE_OFF, "Shift override is Off"
        found: dict[datetime, str | None] = {}
        for item in plan.get("next_shifts") or []:
            if isinstance(item, dict) and (start := _time(item.get("start"))):
                found[start] = item.get("type")
        planned = _time(plan.get("shift_start")) if plan.get("type") == "shift" else None
        if planned and not any(ldr.same_morning(planned, start) for start in found):
            found[planned] = plan.get("shift_type")
        shifts = sorted(found.items(), key=lambda pair: pair[0])
        days = {dt_util.as_local(start).date() for start, _ in shifts}
        for start, shift_type in shifts:
            # Fable review: a start that's passed (or was replaced) never blocks a later one.
            if start <= now or (skip and ldr.same_morning(skip, start)):
                continue
            if not self._app_covered(shift_type, start):
                self._app_recheck = start
                return None, ldr.STATUS_UNCOVERED, f"{shift_type or 'This shift'} mornings have no backstops"
            if (plan.get("duty_night") and ldr.same_morning(_time(plan.get("shift_start")), start)) or self.duty_morning(start):
                self._app_recheck = start
                return None, ldr.STATUS_DUTY, "No backstops: the morning after duty, at work"
            # Fable review: no calendar to ask; a first day back when the day before isn't a workday.
            first_back = (dt_util.as_local(start).date() - timedelta(days=1)) not in days
            morning = await self._shift_morning(start, plan, first_day_back=first_back)
            if finished(morning):
                continue
            if morning.leave_by_source == "remote" and not morning.fixed_times:
                self._app_recheck = start
                return None, ldr.STATUS_REMOTE, "No backstops: the remote shift starts at its last alarm"
            return morning, ldr.STATUS_ARMED, ""
        if shifts:
            # Fable review: workdays sent, all gone by: the phone hasn't planned since.
            return None, ldr.STATUS_FAULT, "No new plan from the phone since its last workdays"
        return None, ldr.STATUS_NO_SHIFT, "No work in your schedule"

    async def _shift_morning(self, start: datetime, plan: dict[str, Any], first_day_back: bool | None = None) -> ldr.Morning:
        """A covered shift's morning: the plan's leave-by when it's for that morning, else the
        calendar's."""
        buffer = int(self.state.get("work_buffer_minutes") or DEFAULT_WORK_BUFFER)
        calendar = ldr.calendar_leave_by(start, int(self.setting_value("fallback_drive_minutes")), buffer)
        planned = plan.get("type") == "shift" and ldr.same_morning(_time(plan.get("shift_start")), start)
        if planned and plan.get("remote"):
            # v0.4.11 item 10: a remote day's backstops are the plan's times after its last alarm.
            remote = plan.get("remote_times") or {}
            times = {rung: t for rung in ("early", "deadline", "late") if (t := _time(remote.get(rung)))}
            return ldr.Morning("shift", start, "remote", False, shift_start=start, fixed_times=times)
        leave_by, source, warning = ldr.pick_leave_by(_time(plan.get("leave_by")) if planned else None, calendar)
        if warning:
            _LOGGER.warning("Bedtime Bridge ladder: %s", warning)
        if planned and plan.get("first_day_back") is not None:
            first_day_back = bool(plan["first_day_back"])
        elif first_day_back is None:
            first_day_back = await self._first_day_back(start)
        return ldr.Morning(
            "shift", leave_by, source, first_day_back, shift_start=start,
            weather_extra_minutes=int(plan.get("weather_extra_minutes") or 0) if planned else 0,
            condition=plan.get("condition") if planned else None,
        )

    async def _next_shift_morning(
        self, after: datetime | None, plan: dict[str, Any], finished: Callable[[ldr.Morning], bool]
    ) -> tuple[ldr.Morning | None, str, str]:
        """The next shift after one that's over, from the work calendar."""
        calendar = self.options.get(CONF_CALENDAR)
        if after is None or not calendar or self.hass.states.get(calendar) is None:
            return None, ldr.STATUS_NO_SHIFT, "No shift on the calendar yet"
        try:
            response = await self.hass.services.async_call(
                "calendar", "get_events",
                {"entity_id": calendar, "start_date_time": (after + timedelta(minutes=1)).isoformat(),
                 "end_date_time": (after + timedelta(days=8)).isoformat()},
                blocking=True, return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - reported as a fault, not a silent "done"
            return None, ldr.STATUS_FAULT, f"Couldn't read the next shift from {calendar}: {err}"
        names = shift_types(self.options.get(CONF_SHIFT_TYPES))
        covered = self._ladder_types()
        events = ((response or {}).get(calendar) or {}).get("events") or []
        for event in sorted(events, key=lambda e: str(e.get("start"))):
            title = str(event.get("summary") or "").lower()
            name = next((n for n in names if n.lower() in title), None)
            start = _time(event.get("start"))
            if name is None or start is None or start <= after:
                continue
            if await self._on_leave(start):
                # v0.5.5 item 33: a shift on a leave day is skipped; the next one counts.
                continue
            if name.lower() not in covered:
                return None, ldr.STATUS_UNCOVERED, f"{name} mornings have no backstops"
            morning = await self._shift_morning(start, plan)
            if not finished(morning):
                return morning, ldr.STATUS_ARMED, ""
        return None, ldr.STATUS_NO_SHIFT, "No shift on the calendar"

    def adopt_leave_titles(self, titles: Any) -> None:
        """v0.5.5 item 33: the app's leave titles (all-day events that make a leave day)."""
        if not isinstance(titles, list):
            return
        clean = [t.strip() for t in titles if isinstance(t, str) and t.strip()]
        if not clean or clean == self.state.get("leave_titles"):
            return
        self.state["leave_titles"] = clean
        self._leave_cache.clear()
        self._changed()
        self.hass.async_create_task(self._recompute_ladder())

    async def _on_leave(self, shift_start: datetime) -> bool:
        """v0.5.5 item 33: the shift's day is covered by an all-day leave event on the work
        calendar (end date exclusive, so leave ending on the 1st doesn't cover the 1st). Read
        failures count as not leave: the backstops stay rather than go quiet by mistake."""
        day = dt_util.as_local(shift_start).date()
        now = dt_util.utcnow()
        cached = self._leave_cache.get(day.isoformat())
        if cached is not None and now - cached[1] < LEAVE_CACHE:
            return cached[0]
        calendar = self.options.get(CONF_CALENDAR)
        if not calendar or self.hass.states.get(calendar) is None:
            return False
        start = dt_util.start_of_local_day(day)
        try:
            response = await self.hass.services.async_call(
                "calendar", "get_events",
                {"entity_id": calendar, "start_date_time": start.isoformat(), "end_date_time": (start + timedelta(days=1)).isoformat()},
                blocking=True, return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - not leave: keep the backstops
            _LOGGER.warning("Bedtime Bridge couldn't read %s for leave: %s", calendar, err)
            return False
        titles = [t.casefold() for t in (self.state.get("leave_titles") or DEFAULT_LEAVE_TITLES)]
        events = ((response or {}).get(calendar) or {}).get("events") or []
        leave = any(str(e.get("summary") or "").strip().casefold() in titles and leave_covers(e, day) for e in events)
        self._leave_cache[day.isoformat()] = (leave, now)
        return leave

    async def _first_day_back(self, shift_start: datetime) -> bool:
        """No shift the day before (a day off, or leave). Read from the work calendar when the app
        didn't say; if that fails, assume a first day back (fail early, not late)."""
        day = dt_util.as_local(shift_start).date()
        cache: dict[str, bool] = self.state.setdefault("ladder_first_day_back", {})
        if day.isoformat() in cache:
            return cache[day.isoformat()]
        calendar = self.options.get(CONF_CALENDAR)
        if not calendar or self.hass.states.get(calendar) is None:
            return True
        start = dt_util.start_of_local_day(day) - timedelta(days=1)
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {"entity_id": calendar, "start_date_time": start.isoformat(), "end_date_time": (start + timedelta(days=1)).isoformat()},
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - any failure means "assume first day back"
            _LOGGER.warning("Bedtime Bridge couldn't read %s for first day back: %s", calendar, err)
            return True
        events = ((response or {}).get(calendar) or {}).get("events") or []
        names = [n.lower() for n in shift_types(self.options.get(CONF_SHIFT_TYPES))]
        worked = any(any(n in str(e.get("summary") or "").lower() for n in names) for e in events)
        cache.clear()
        cache[day.isoformat()] = not worked
        return not worked

    def _ladder_delay_for(self, morning: str) -> dict[str, Any]:
        delay = self.state.get("ladder_delay") or {}
        return delay if delay.get("morning") == morning else {}

    def _ladder_cleanup(self, morning: str | None) -> None:
        """A delay or early floor belongs to its morning; once another morning's ladder is up, it goes."""
        if (self.state.get("ladder_delay") or {}).get("morning") not in (None, morning):
            self.state["ladder_delay"] = None
        if (self.state.get("ladder_early_floor") or {}).get("morning") not in (None, morning):
            self.state["ladder_early_floor"] = None

    @callback
    def _schedule_ladder_rollover(self) -> None:
        """Once the late rung is 60 minutes past, this morning is over: roll to the next one."""
        if self._ladder_unsub:
            self._ladder_unsub()
            self._ladder_unsub = None
        times = [t for t in (_time(v) for v in ((self.state.get("ladder") or {}).get("times") or {}).values()) if t]
        if not times:
            # v0.6.0 item 19 (Fable review): an app-kept schedule's idle status is looked at again
            # once the shift that made it has started (no sensor change will come).
            if self._app_recheck and self._app_recheck > dt_util.utcnow():
                self._ladder_unsub = async_track_point_in_utc_time(self.hass, self._ladder_rollover, self._app_recheck + timedelta(minutes=1))
            return
        late = max(times)
        due = late + ldr.LADDER_TAIL
        if due <= dt_util.utcnow():
            return
        self._ladder_unsub = async_track_point_in_utc_time(self.hass, self._ladder_rollover, due)

    @callback
    def _ladder_rollover(self, _now: datetime) -> None:
        self._ladder_unsub = None
        self.state["ladder_delay"] = None
        self.state["ladder_early_floor"] = None
        # Straight to the next state (armed for the next morning, or why not); no "done" (item 19).
        self.hass.async_create_task(self._recompute_ladder())

    def _move_ladder(self, minutes: int, *, rungs_ahead_only: bool) -> None:
        """Shifts the stored rungs (negative = earlier). Earlier moves touch only rungs still
        ahead; anything landing in the past is published as now + 60 s."""
        ladder = self.state.get("ladder") or {}
        if ladder.get("status") != ldr.STATUS_ARMED or not minutes:
            return
        now = dt_util.utcnow()
        times = {}
        for rung, value in (ladder.get("times") or {}).items():
            when = _time(value)
            if when is None or (rungs_ahead_only and when <= now):
                times[rung] = value
                continue
            times[rung] = max(when + timedelta(minutes=minutes), now + ldr.PAST_RUNG_LEAD).isoformat()
        ladder["times"] = times
        self._schedule_ladder_rollover()

    def change_morning_ladder(self, delay_minutes: int, changed_by: str | None, reason: str | None) -> None:
        """Item 3: the backstops move by the same amount as the wake-up, later or earlier. The
        delay persists through recomputes that morning and is cleared when its ladder ends."""
        ladder = self.state.get("ladder") or {}
        if ladder.get("status") != ldr.STATUS_ARMED or not delay_minutes:
            return
        current = self._ladder_delay_for(ladder["morning"])
        total = int(current.get("minutes", 0)) + int(delay_minutes)
        self.state["ladder_delay"] = {"morning": ladder["morning"], "minutes": total, "changed_by": changed_by, "reason": reason}
        self._move_ladder(int(delay_minutes), rungs_ahead_only=False)
        ladder.update(delay_minutes=total, changed_by=changed_by, change_reason=reason)
        self._changed()

    def ladder_early_floor(self, floor: datetime, changed_by: str | None, reason: str | None) -> None:
        """Shift alarms only (item 7): the early rung may not come before the new last alarm
        plus the gap. Kept for that morning through recomputes, cleared when it ends."""
        ladder = self.state.get("ladder") or {}
        if ladder.get("status") != ldr.STATUS_ARMED:
            return
        self.state["ladder_early_floor"] = {"morning": ladder["morning"], "at": floor.isoformat()}
        # From the early rung before any floor, so moving the alarms back puts it back.
        base = _time(ladder.get("early_base")) or _time((ladder.get("times") or {}).get("early"))
        if base is not None:
            ladder["early_base"] = base.isoformat()
            ladder["times"]["early"] = max(base, floor).isoformat()
            self._schedule_ladder_rollover()
        ladder.update(changed_by=changed_by, change_reason=reason)
        self._changed()

    def _ladder_earlier(self, minutes: int, kind: str, condition: str | None = None) -> None:
        """Worse weather or traffic: the rungs still ahead move earlier, never later."""
        ladder = self.state.get("ladder") or {}
        if minutes <= 0 or ladder.get("status") != ldr.STATUS_ARMED:
            return
        self._move_ladder(-minutes, rungs_ahead_only=True)
        ladder["earlier_minutes"] = ladder.get("earlier_minutes", 0) + minutes
        ladder[f"{kind}_minutes"] = ladder.get(f"{kind}_minutes", 0) + minutes
        if kind == "weather":
            ladder["weather_extra_minutes"] = ladder.get("weather_extra_minutes", 0) + minutes
            ladder["condition"] = condition
        _LOGGER.info("Bedtime Bridge ladder moved %s min earlier for %s", minutes, condition or kind)
        self._changed()

    def _traffic_moved(self, new_leave_by: datetime | None) -> None:
        """The phone rechecked the drive when an alarm was stopped. A longer drive (an earlier
        leave-by) moves the rungs earlier by the difference not already applied."""
        ladder = self.state.get("ladder") or {}
        planned = _time(ladder.get("leave_by"))
        if new_leave_by is None or planned is None or ladder.get("leave_by_source") != "plan":
            return
        needed = int((planned - new_leave_by).total_seconds() // 60)
        self._ladder_earlier(needed - ladder.get("traffic_minutes", 0), "traffic")

    async def _weather_tick(self, _now: datetime) -> None:
        """Every 15 minutes from 2 hours before the early rung until leave-by: if the drive's
        forecast is worse than the plan allowed for, move the rungs still ahead earlier."""
        ladder = self.state.get("ladder") or {}
        if ladder.get("status") != ldr.STATUS_ARMED:
            return
        now = dt_util.utcnow()
        early, leave_by = _time(ladder["times"].get("early")), _time(ladder.get("leave_by"))
        if early is None or leave_by is None or not (early - timedelta(hours=2) <= now < leave_by):
            return
        weather = self.options.get(CONF_WEATHER)
        if not weather or self.hass.states.get(weather) is None:
            return
        try:
            response = await self.hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": weather, "type": "hourly"}, blocking=True, return_response=True
            )
        except Exception as err:  # noqa: BLE001 - a missed check is retried in 15 minutes
            _LOGGER.debug("Bedtime Bridge ladder forecast check failed: %s", err)
            return
        forecast = ((response or {}).get(weather) or {}).get("forecast") or []
        plan = self.state.get("ladder_plan") or {}
        minutes = plan.get("weather_minutes")
        if not minutes:
            extra = int(self.setting_value("weather_extra_minutes") or 15)
            minutes = {condition: extra for condition in ldr.DEFAULT_WEATHER_CONDITIONS}
        arrive = _time(ladder.get("shift_start")) or _time(ladder.get("appointment_start")) or leave_by + timedelta(hours=1)
        worst, condition = ldr.worst_weather(forecast, leave_by, arrive, minutes, plan.get("freezing_at"))
        planned_extra = int(plan.get("weather_extra_minutes") or 0) if ladder.get("leave_by_source") == "plan" else 0
        self._ladder_earlier(worst - planned_extra - ladder.get("weather_minutes", 0), "weather", condition)

    # MARK: Changes after bedtime

    def _state_of(self, option: str) -> str | None:
        state = self.hass.states.get(self.options.get(option) or "")
        if state is None:
            return None
        # v0.5.4 item 35: an "On means asleep" helper reads the other way round.
        if made.inverted(self.raw_options, option) and state.state in ("on", "off"):
            return "off" if state.state == "on" else "on"
        return state.state

    def _next_shift_start(self) -> str | None:
        sensor = self.hass.states.get(self.options.get(CONF_SHIFT_SENSOR) or "")
        value = sensor.attributes.get("next_shift_start") if sensor else None
        return str(value) if value else None

    @callback
    def _shift_changed(self, event: Event[EventStateChangedData]) -> None:
        """Recomputes the ladder, and flags the plan as outdated when the override or HA's next
        shift changes after it."""
        self.hass.async_create_task(self._recompute_ladder())
        if self.state.get("plan_outdated") or not self._plan_still_ahead():
            return
        override = self._state_of(CONF_SHIFT_OVERRIDE)
        shift_start = self._next_shift_start()
        reason = None
        if override not in (None, *UNKNOWN_STATES) and override != self.state.get("planned_override"):
            reason = f"Shift override changed to {override}"
        elif (shift_start and shift_start != self.state.get("planned_shift_start")
              and (self.state.get("ladder_plan") or {}).get("schedule") != "app"):
            # v0.6.0 item 19 (Fable review): a leftover calendar sensor doesn't speak for a
            # schedule kept in the app.
            when = dt_util.parse_datetime(shift_start)
            reason = f"Next shift moved to {timefmt.day_long(when) if when else shift_start}"
        if reason is None:
            return
        self.state["plan_outdated"] = True
        self.state["outdated_reason"] = reason
        self.hass.bus.async_fire(EVENT, {"type": EVENT_PLAN_OUTDATED, "reason": reason})
        self.hass.async_create_task(self._notify_outdated(reason))
        self._changed()

    def _plan_still_ahead(self) -> bool:
        """A plan exists and its wake-up hasn't happened yet (or it planned no alarm recently)."""
        last_plan = dt_util.parse_datetime(self.state.get("last_plan") or "")
        if last_plan is None:
            return False
        wake = dt_util.parse_datetime(self.state.get("wake_time") or "")
        if wake is not None:
            return wake > dt_util.utcnow()
        return dt_util.utcnow() - last_plan < timedelta(hours=18)

    async def _notify_outdated(self, reason: str) -> None:
        notify = (self.options.get(CONF_NOTIFY) or "").strip()
        if not notify or not self.options.get(CONF_REPLAN_ALERT, True):
            return
        domain, _, service = notify.partition(".")
        try:
            await self.hass.services.async_call(
                domain,
                service,
                {
                    "title": "Alarms may be wrong",
                    "message": f"{reason} after you planned. Tap to re-plan.",
                    "data": {
                        "tag": "bedtime_bridge_replan",
                        "url": REPLAN_URL,
                        "actions": [{"action": "URI", "title": "Update alarms", "uri": REPLAN_URL}],
                    },
                },
                blocking=False,
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Bedtime Bridge couldn't send the re-plan notification: %s", err)
            return
        self.clears.drop("bedtime_bridge_replan")  # v0.5.4 item 81

    async def _snoozed(self, data: dict[str, Any]) -> None:
        if data.get("moved"):
            # v0.5.5 item 5: a snooze's ring moved without a slide (a new plan, an edit, the app
            # opened). Only the times follow: not a snooze, so no count, no Wake Fade, no event.
            if data.get("rings_again"):
                self.state["snoozed_until"] = data["rings_again"]
                self.state["alarm_time"] = data["rings_again"]
                now = dt_util.utcnow()
                if rings := _time(data["rings_again"]):
                    # v0.5.5 item 44: the ring it moved from is gone (a ring still to come that
                    # won't happen); rings already past stay watched.
                    kept = [r for r in (self.state.get("snooze_rings") or [])
                            if (t := _time(r)) and now - timedelta(hours=4) < t <= now]
                    self.state["snooze_rings"] = sorted({*kept, rings.isoformat()})
            if isinstance(data.get("alarm_times"), list):
                self.state["alarm_times"] = [t for t in data["alarm_times"] if isinstance(t, str)]
            # v0.5.5 item 44: the entities show the new times (no event fires to refresh them).
            self._changed()
            return
        if data.get("test"):
            await self._wake(CONF_SNOOZE_HELPER)
            self._event(MSG_SNOOZED, test=True)
            return
        self.state["snoozed_until"] = data.get("rings_again")
        self.state["snooze_count"] = int(self.state.get("snooze_count") or 0) + 1
        # v0.6.0 item 36: the times left, as a moved snooze already keeps them.
        if isinstance(data.get("alarm_times"), list):
            self.state["alarm_times"] = [t for t in data["alarm_times"] if isinstance(t, str)]
        if data.get("rings_again"):
            self.state["alarm_time"] = data["rings_again"]
        if self.state.get("routine_ran") or self._state_of(CONF_SNOOZE_HELPER) == "on":
            # Already up (the wake routine ran), or the fade is running: nothing to start.
            self.state["skipped_away"] = self._away_blocks()
        else:
            await self._wake(CONF_SNOOZE_HELPER)
        self._event(MSG_SNOOZED)

    async def _stopped(self, data: dict[str, Any]) -> None:
        if data.get("test"):
            # A test alarm runs the real routines but leaves tonight's plan alone.
            await self._wake(CONF_STOP_HELPER)
            self._event(MSG_STOPPED, test=True)
            return
        # all: the slide-and-hold that ends every alarm left in this wake-up (item 2).
        stop_all = bool(data.get("all"))
        remaining = [] if stop_all else data.get("alarm_times") or []
        # Another alarm still snoozing (v0.4.11 item 6): it's left, and its ring-again stays.
        self.state["snoozed_until"] = (data.get("snoozed_until") or None) if remaining else None
        self.state["alarm_times"] = remaining
        self.state["alarm_time"] = remaining[0] if remaining else None
        # v0.6.0 item 38 (Fable review): more of this wake-up to come; the app says so with last.
        self.state["wake_up_open"] = dt_util.utcnow().isoformat() if remaining and data.get("last") is False else None
        if not remaining:
            self.state["alarm_cause"] = "noalarm"
            self._end_morning()
        # early: ended from Tonight before any alarm rang (item 8), so the early helper runs.
        early = bool(data.get("early"))
        # How the phone decided what to keep (v0.4.1 item 1), so a morning can be traced.
        self.state["last_stop"] = {
            "time": dt_util.utcnow().isoformat(), "all": stop_all, "up": data.get("up"),
            "stop_behavior": data.get("stop_behavior"), "alarms_left": len(remaining),
        }
        # The wake routine runs on the first stop of a wake-up only (item 9): Wake Sequence
        # treats its helper as a button, so every turn-on would re-run it.
        first = not self.state.get("routine_ran")
        if first:
            helper = CONF_EARLY_HELPER if early and self.options.get(CONF_EARLY_HELPER) else CONF_STOP_HELPER
            await self._wake(helper)
            self.state["routine_ran"] = True
        else:
            self.state["skipped_away"] = self._away_blocks()
        if not remaining and self.is_home():
            # v0.5.4 item 35: the wake-up is over. Bedtime Bridge's own Awake For Day goes on and
            # its Bedtime switch off (your own helpers are left to your wake routine).
            if self.is_made(CONF_AWAKE_HELPER):
                await self._set_helper(CONF_AWAKE_HELPER, True)
            await self._turn_off_made(CONF_BEDTIME_HELPER)
        self._event(MSG_STOPPED, all=stop_all, early=early, routine=first)
        # Good morning on the first stop; a later stop sends it again only while the shortcut
        # hasn't reported it ran (v0.4.10 item 2).
        resend = not first and CMD_MORNING in self._pending_commands
        if (first or resend) and not self.state.get("skipped_away"):
            # The phone's own morning shortcut (disarm, good morning, Focus off) used to run
            # from the Clock app; app alarms don't trigger that, so this push does instead.
            await self._command(CMD_MORNING)
        if first:
            # v0.5.4 item 27: home or away (away it's for the phone only; the event says so).
            # v0.5.4 item 63: in a task, so its drive check doesn't hold up Stop's reply.
            self.hass.async_create_task(self.morning_briefing(), "bedtime_bridge morning briefing")

    # MARK: v0.5.4 items 27 and 28: briefings

    async def morning_briefing(self, *, quiet: bool = False) -> str | None:
        """Built at good morning, with one fresh drive check when the drive is checked here and
        you're home (away, the phone's leave-by from where you are stands). On the Briefing
        sensor and the morning_briefing event; None with the morning briefing off, or before an
        app with briefings (0.5.4) has sent its settings."""
        if "briefing_settings" not in self.state:
            return None
        cfg = brf.settings(self.state.get("briefing_settings"))
        if not cfg.get("morning", True):
            return None
        now = dt_util.utcnow()
        away = not self.is_home()
        leave_by = _time(self.state.get("leave_by"))
        if not away and leave_by is not None and now < leave_by + timedelta(minutes=LEAVE_CHECK_TAIL_MINUTES):
            await self._drive_check_here(notify=False)
            leave_by = _time(self.state.get("leave_by"))
        trip = self.state.get("trip") or {}
        text = brf.morning_text(
            cfg, brf.fresh_facts(self.state.get("briefing_facts"), now), now=now, leave_by=leave_by,
            drive_minutes=self.state.get("drive_minutes"), planned_minutes=trip.get("planned_minutes"), away=away,
        )
        record = self.state.setdefault("briefing", {})
        record.update({"morning": text, "morning_at": now.isoformat(), "kind": "morning", "away": away, "quiet": quiet})
        self.hass.bus.async_fire(EVENT, {"type": EVENT_MORNING_BRIEFING, "text": text, "away": away, "quiet": quiet})
        self._changed()
        return text

    def _schedule_briefing_fallback(self, delay: timedelta = BRIEFING_FALLBACK, night: str | None = None) -> None:
        self._cancel_briefing_fallback()
        night = night or self.state.get("bedtime_started_at")
        # v0.5.4 item 72: kept in the state, so a restart or reload inside the 2 minutes re-arms it.
        self.state["briefing_fallback"] = {"at": (dt_util.utcnow() + delay).isoformat(), "night": night}
        self._save()

        @callback
        def fallback(_now: datetime) -> None:
            self._briefing_unsub = None
            self.state["briefing_fallback"] = None
            self._save()
            record = self.state.get("briefing") or {}
            # v0.5.4 item 71: not when tonight's was already announced (bedtime reported twice).
            if (night and self.state.get("bedtime_started_at") == night and record.get("bedtime_fired_for") != night
                    and record.get("bedtime_fired_night") != _night_of(night)):
                self.hass.async_create_task(self.bedtime_briefing(fallback=True))

        self._briefing_unsub = async_call_later(self.hass, delay, fallback)

    def _cancel_briefing_fallback(self, forget: bool = True) -> None:
        """`forget` False (unloading): the timer goes, the stored deadline stays for setup."""
        if self._briefing_unsub:
            self._briefing_unsub()
            self._briefing_unsub = None
        if forget and self.state.get("briefing_fallback"):
            self.state["briefing_fallback"] = None
            self._save()

    @callback
    def _resume_briefing_fallback(self) -> None:
        """v0.5.4 item 72: a fallback that was waiting when Home Assistant restarted (or the entry
        reloaded): re-armed for what's left, or run now if it came due during the restart (up to
        2 minutes late). Older, or for another night: dropped."""
        pending = self.state.get("briefing_fallback") or {}
        due, night = _time(pending.get("at")), pending.get("night")
        if due is None or not night or night != self.state.get("bedtime_started_at"):
            self._cancel_briefing_fallback()
            return
        left = due - dt_util.utcnow()
        if left > timedelta(0):
            self._schedule_briefing_fallback(left, night)
        elif -left <= BRIEFING_FALLBACK:
            self._schedule_briefing_fallback(timedelta(0), night)
        else:
            self._cancel_briefing_fallback()

    async def bedtime_briefing(self, *, fire: bool = True, fallback: bool = False) -> str | None:
        """Built at bedtime from the alarms set, the house (home checks, problems only), the
        phone's battery and the app's facts. On the Briefing sensor; the bedtime_briefing event
        fires once a night (`fire`). `fallback`: no plan came (v0.5.4 item 60), so the alarms are
        said only when they're known (alarms still ahead, or a plan from today)."""
        if "briefing_settings" not in self.state:
            return None
        cfg = brf.settings(self.state.get("briefing_settings"))
        if not cfg.get("bedtime", True):
            return None
        now = dt_util.utcnow()
        away = not self.is_home()
        house = (await self._home_check({})).get("issues")
        level, charging = self._battery()
        alarms = [t for t in (_time(v) for v in self.state.get("alarm_times") or []) if t]
        planned = _time(self.state.get("last_plan"))
        alarms_known = (not fallback or any(a > now for a in alarms)
                        or (planned is not None and now - planned < BRIEFING_PLAN_KNOWN))
        text = brf.bedtime_text(
            cfg, brf.fresh_facts(self.state.get("briefing_facts"), now), now=now,
            alarms=alarms,
            leave_by=_time(self.state.get("leave_by")), sleep_goal=self.wind_down.settings.get("sleep_goal_minutes"),
            house=house, away=away, battery=level, charging=charging, alarms_known=alarms_known,
        )
        record = self.state.setdefault("briefing", {})
        night = self.state.get("bedtime_started_at")
        record.update({"bedtime": text, "bedtime_at": now.isoformat(), "kind": "bedtime", "away": away, "quiet": False,
                       "bedtime_for": night})
        # v0.5.4 item 71: once per night by its date, so bedtime reported twice (Report Ran and
        # Start Bedtime Routine) re-stamps the night without announcing it again.
        night_date = _night_of(night) or _night_of(now.isoformat())
        if fire and record.get("bedtime_fired_for") != night and record.get("bedtime_fired_night") != night_date:
            record["bedtime_fired_for"] = night
            record["bedtime_fired_night"] = night_date
            self.hass.bus.async_fire(EVENT, {"type": EVENT_BEDTIME_BRIEFING, "text": text, "away": away})
        self._changed()
        return text

    def _check_alarms_set(self, data: dict[str, Any]) -> None:
        """The phone reads back what AlarmKit holds; fewer than it meant to set is reported."""
        planned = data.get("alarms_planned")
        actual = len(data.get("alarm_times") or [])
        if isinstance(planned, int) and actual < planned:
            self.state["alarms_missing"] = {"set": actual, "planned": planned, "time": dt_util.utcnow().isoformat()}
            _LOGGER.warning("Bedtime Bridge: the phone set %s of %s alarms", actual, planned)
            self.hass.bus.async_fire(EVENT, {"type": EVENT_ALARMS_MISSING, "set": actual, "planned": planned})
        else:
            self.state["alarms_missing"] = None

    @callback
    def _end_morning(self) -> None:
        """The wake-up is over (every alarm stopped, ended all, or changed): this morning's
        snoozes become Snoozes last morning and the count starts again at 0."""
        self.state["snoozes_last_morning"] = int(self.state.get("snooze_count") or 0)
        # For the weekly summary (v0.4.3 item 15): snoozes. Backstops are counted as they fire
        # (v0.4.11 item 26, _backstop_fired).
        now = dt_util.utcnow()
        weekly.record(self.state.setdefault("history", []), "morning", now, snoozes=self.state["snoozes_last_morning"])
        self.state["snooze_count"] = 0

    @callback
    def _backstop_fired(self, event: Event) -> None:
        """Wake Deadline Backstop escalated a rung (v0.4.11 item 26): kept for the app's red
        *fired* tag until the next plan, and counted for the weekly summary."""
        rung = str(event.data.get("rung") or "").lower()
        if rung not in ("early", "deadline", "late"):
            _LOGGER.debug("Bedtime Bridge: backstop fired with an unknown rung %r", rung)
            return
        now = dt_util.utcnow()
        level = event.data.get("level")
        fired = list(self.state.get("backstops_fired") or [])
        fired.append({"rung": rung, "level": level, "time": now.isoformat()})
        self.state["backstops_fired"] = fired
        weekly.record(self.state.setdefault("history", []), "backstop", now, rung=rung, level=level)
        # v0.5.4 item 81: the backstop sent its notification again; an owed clear would remove it.
        self.clears.drop(RESTART_NOTIFICATION_TAGS)
        self._changed()

    # MARK: Temporary alarms

    # MARK: Nap (v0.4.2 item 3)

    async def _nap_started(self, data: dict[str, Any]) -> dict[str, Any]:
        """A nap's alarms are set. Like temporary alarms, nothing about the morning changes: no
        wake routines, Good Morning or backstops; Sleep In and Awake For Day stay. The napping
        helper goes on while you're home; the nap Focus shortcut runs home or away."""
        times = [t.isoformat() for t in sorted(t for t in (_time(v) for v in data.get("alarm_times") or []) if t)]
        weekly.record(self.state.setdefault("history", []), "nap", dt_util.utcnow(), minutes=data.get("minutes"))
        self.state["nap_alarm_times"] = times
        helper = self.options.get(CONF_NAP_HELPER)
        # A binary sensor picked as the Napping helper is only read (v0.5.4 item 35).
        helper_on = made.writable(helper) and self.is_home()
        if helper_on:
            await self._turn_on(helper)
        focus = await self._command(CMD_NAP_START)
        self._event(MSG_NAP_STARTED, alarm_times=times, napping_helper=helper_on)
        return {"napping_helper": helper_on, "nap_focus": focus}

    async def _nap_ended(self, data: dict[str, Any]) -> dict[str, Any]:
        """The nap is over (its last alarm stopped, or End nap). The helper goes off if it's on,
        so a nap started at home doesn't leave it on after you've gone out."""
        self.state["nap_alarm_times"] = []
        helper = self.options.get(CONF_NAP_HELPER)
        if helper and self.hass.states.is_state(helper, "on"):
            await self._turn_off(helper)
        await self._clear_command(CMD_NAP_START)
        focus = await self._command(CMD_NAP_END)
        self._event(MSG_NAP_ENDED)
        return {"nap_focus": focus}

    async def _temporary_snoozed(self, data: dict[str, Any]) -> None:
        """Quiet: nothing in the house changes. With wake (v0.5.2 item 2: Wake me, or Quiet, then
        wake me from its Nth snooze) the snooze helper runs the same way a regular snooze does;
        either way the regular alarms' snooze count and times stay as they are."""
        wake = bool(data.get("wake"))
        if wake and self._state_of(CONF_SNOOZE_HELPER) != "on":
            await self._wake(CONF_SNOOZE_HELPER)
        self._event(MSG_TEMP_SNOOZED, rings_again=data.get("rings_again"), wake=wake)

    async def _temporary_stopped(self, data: dict[str, Any]) -> None:
        """Up for a moment, then back to bed. Awake For Day, Sleep In and the regular alarms
        stay. With "disarm", the phone disarms now and re-arms once you're back in bed. With
        wake (v0.5.2 item 2) the stop helper runs the same way a regular stop does, without
        ending the regular alarms or marking the morning's wake routine as run."""
        house_alarm = data.get("house_alarm")
        # v0.5.3 item 49: "nothing": the house isn't touched at all, lights included.
        wake = bool(data.get("wake")) and house_alarm != "nothing"
        if wake:
            await self._wake(CONF_STOP_HELPER)
        disarmed = False
        if house_alarm == "disarm" and self.is_home():
            disarmed = await self._command(CMD_DISARM)
        if disarmed:
            now = dt_util.utcnow()
            regular = sorted(t for t in (_time(v) for v in data.get("alarm_times") or []) if t and t > now)
            give_up = now + timedelta(hours=REARM_GIVE_UP_HOURS)
            self.state["rearm_since"] = now.isoformat()
            self.state["rearm_until"] = min([give_up, *regular[:1]]).isoformat()
            self._watch_bed("rearm")
        self._event(MSG_TEMP_STOPPED, house_alarm=house_alarm, disarmed=disarmed, wake=wake)

    # MARK: Back in bed

    @callback
    def _watch_bed(self, kind: str) -> None:
        """Waits for you to be back in bed (occupied for the kind's delay, not counting the
        sensor's short dropouts) before its deadline: "rearm" re-arms the house, "rerun" runs
        bedtime again."""
        self._cancel_bed(kind)
        since = _time(self.state.get(f"{kind}_since"))
        until = _time(self.state.get(f"{kind}_until"))
        bed = self.options.get(CONF_BED_SENSOR)
        if since is None:
            return
        if not bed or until is None or until <= dt_util.utcnow():
            self._end_bed(kind)
            return
        check = lambda *_: self._bed_check(kind)  # noqa: E731
        self._bed_unsubs[kind] = [
            async_track_state_change_event(self.hass, [bed], callback(check)),
            async_track_point_in_utc_time(self.hass, callback(check), until),
        ]
        self._bed_check(kind)

    @callback
    def _bed_check(self, kind: str) -> None:
        since = _time(self.state.get(f"{kind}_since"))
        until = _time(self.state.get(f"{kind}_until"))
        if since is None:
            return
        now = dt_util.utcnow()
        if until is None or now >= until:
            # Not back in bed in time: leave it to the morning (re-arming or starting bedtime
            # as you get up would be worse than doing nothing).
            self._end_bed(kind)
            return
        bed = self.hass.states.get(self.options.get(CONF_BED_SENSOR) or "")
        if bed is None:
            return
        if bed.state != "on":
            # Out of bed only once it has been empty for a while: the sensor drops out for up
            # to ~19 s while you're in it, which mustn't start the wait over.
            empty_at = bed.last_changed + timedelta(seconds=BED_EMPTY_SECONDS)
            if now >= empty_at:
                self._in_bed_since.pop(kind, None)
            elif kind in self._in_bed_since:
                self._bed_unsubs.setdefault(kind, []).append(
                    async_track_point_in_utc_time(self.hass, callback(lambda _now: self._bed_check(kind)), empty_at)
                )
            return
        started = self._in_bed_since.setdefault(kind, max(bed.last_changed, since))
        minutes = self.options.get(CONF_REARM_MINUTES) if kind == "rearm" else self.options.get(CONF_RERUN_MINUTES)
        default = DEFAULT_REARM_MINUTES if kind == "rearm" else DEFAULT_RERUN_MINUTES
        delay = timedelta(minutes=int(minutes or default))
        in_bed_for = now - started
        if in_bed_for >= delay:
            self._end_bed(kind)
            self.hass.async_create_task(self._back_in_bed(kind))
            return
        self._bed_unsubs.setdefault(kind, []).append(
            async_call_later(self.hass, delay - in_bed_for, callback(lambda _now: self._bed_check(kind)))
        )

    async def _back_in_bed(self, kind: str) -> None:
        if kind == "rearm":
            self._event(EVENT_REARMED, armed=await self._command(CMD_ARM))
        else:
            self._event(EVENT_RERUN, sent=await self._command(CMD_BEDTIME))

    @callback
    def _end_bed(self, kind: str) -> None:
        self._cancel_bed(kind)
        if self.state.get(f"{kind}_since") is not None:
            self.state[f"{kind}_since"] = None
            self.state[f"{kind}_until"] = None
            self._changed()

    @callback
    def _cancel_bed(self, kind: str) -> None:
        self._in_bed_since.pop(kind, None)
        for unsub in self._bed_unsubs.pop(kind, []):
            unsub()

    @callback
    def _cancel_timers(self) -> None:
        for kind in list(self._bed_unsubs):
            self._cancel_bed(kind)
        if self._restore_unsub:
            self._restore_unsub()
            self._restore_unsub = None
        if self._ladder_unsub:
            self._ladder_unsub()
            self._ladder_unsub = None
        if self._charge_unsub:
            self._charge_unsub()
            self._charge_unsub = None
        for command in list(self._command_unsubs):
            self._unwatch_command(command)
        self._cancel_leave_checks()
        for unsub in self._command_check_unsubs.values():
            unsub()
        self._command_check_unsubs = {}
        self._cancel_unanswered()
        self._cancel_resends()
        self._cancel_briefing_fallback(forget=False)

    # MARK: Unanswered alarms (v0.5.2 item 3)

    def _watched_alarms(self) -> list[datetime]:
        """The app's alarm times, each snooze's ring-again (v0.5.3 item 40), and alarms that rang
        unanswered (item 39), each checked 10 minutes after."""
        values = [*(self.state.get("alarm_times") or []), self.state.get("snoozed_until"),
                  *(self.state.get("snooze_rings") or []), *(self.state.get("pending_unanswered") or [])]
        return sorted({t for t in (_time(v) for v in values) if t})

    @callback
    def _cancel_unanswered(self) -> None:
        for unsub in self._unanswered_unsubs:
            unsub()
        self._unanswered_unsubs = []

    @callback
    def _schedule_unanswered(self, catch_up: bool = False) -> None:
        """A check 10 minutes after each of the app's alarms (and a snooze's ring-again). It
        works with backstops off. Clock app alarms are left out: their snooze and stop come from
        your own automations, if at all."""
        self._cancel_unanswered()
        if self.state.get("alarm_style") == "clock":
            return
        now = dt_util.utcnow()
        for alarm in self._watched_alarms():
            due = alarm + UNANSWERED_AFTER
            if due <= now:
                # v0.5.3 item 7: a check that came due while Home Assistant was down (within the
                # hour) still runs, once, after a restart.
                if catch_up and now - due < timedelta(hours=1):
                    self.hass.async_create_task(self._unanswered_due(alarm))
                continue
            self._unanswered_unsubs.append(async_track_point_in_utc_time(
                self.hass, callback(lambda _now, alarm=alarm: self.hass.async_create_task(self._unanswered_due(alarm))), due))

    async def _unanswered_due(self, alarm: datetime) -> None:
        """Still set, and no snooze or stop since it rang: the snooze helper runs (lights up,
        unless you're away or chose events only) and a time-sensitive notification says so."""
        if not any(abs((t - alarm).total_seconds()) < 60 for t in self._watched_alarms()):
            return
        # v0.5.3 item 41: answered per alarm (an older app: any answer since it rang).
        if self._answered(alarm):
            return
        # Fix decided 12 Nov: skipped when Home Assistant knows you're up: the stop routine ran
        # and you're out of bed. Never on the bed sensor alone.
        if self.state.get("routine_ran") and self.awake.s.get("out_since"):
            return
        done = list(self.state.get("unanswered_alarms") or [])
        if alarm.isoformat() in done:
            return
        self.state["unanswered_alarms"] = [*done[-19:], alarm.isoformat()]
        _LOGGER.info("Bedtime Bridge: the %s alarm wasn't answered", alarm.isoformat())
        if self._state_of(CONF_SNOOZE_HELPER) != "on":
            await self._wake(CONF_SNOOZE_HELPER)
        await self.wind_down._notify("Alarm not answered", f"Your {timefmt.short(alarm)} alarm wasn't answered",
                                     tag="alarm_unanswered", time_sensitive=True)
        self._event(EVENT_ALARM_UNANSWERED, unanswered_alarm=alarm.isoformat())

    # MARK: Leave checks (v0.4.1 item 17)

    @callback
    def _cancel_leave_checks(self) -> None:
        for unsub in self._leave_unsubs:
            unsub()
        self._leave_unsubs = []

    @callback
    def _schedule_leave_checks(self) -> None:
        """A RUN_LEAVE_CHECK push at each reminder (minutes before leave-by) still ahead, until 15
        minutes after leave-by. The phone re-checks the drive, notifies and reports leave_by."""
        self._cancel_leave_checks()
        leave_by = _time(self.state.get("leave_by"))
        if leave_by is None or self.state.get("remote"):
            # No trip on a remote day (v0.4.11 item 5): nothing to check.
            return
        now = dt_util.utcnow()
        if now > leave_by + timedelta(minutes=LEAVE_CHECK_TAIL_MINUTES):
            return
        for minutes in sorted(set(self.state.get("leave_reminders") or []), reverse=True):
            due = leave_by - timedelta(minutes=minutes)
            if due <= now:
                continue
            self._leave_unsubs.append(
                async_track_point_in_utc_time(self.hass, callback(lambda _now: self.hass.async_create_task(self._leave_check_due())), due)
            )

    async def _leave_check_due(self) -> None:
        leave_by = _time(self.state.get("leave_by"))
        now = dt_util.utcnow()
        if leave_by is None or now > leave_by + timedelta(minutes=LEAVE_CHECK_TAIL_MINUTES):
            return
        if not self.is_home() or not self._leave_shift_still_on():
            # Already left, or the shift came off the calendar: no more checks.
            self.state["leave_reminders"] = []
            self._cancel_leave_checks()
            if self.is_home():
                self._drop_leave_by()
            return
        if await self._drive_check_here():
            return
        await self._command(CMD_LEAVE_CHECK)

    # MARK: Drive times here (0.4.4)

    def _drive_services(self) -> list[Any]:
        """The services chosen in the options, as far as their keys are in; empty means the phone."""
        source = self.options.get(CONF_DRIVE_SOURCE) or dtm.SOURCE_PHONE
        if source == dtm.SOURCE_PHONE:
            return []
        session = async_get_clientsession(self.hass)
        live = self.state.get("live_activity") or {}
        services: dict[str, Any] = {}
        if self.options.get(CONF_MAPS_KEY) and self.options.get(CONF_MAPS_KEY_ID) and live.get("team_id"):
            if self._maps is None:
                self._maps = dtm.AppleMaps(session, self.options[CONF_MAPS_KEY], self.options[CONF_MAPS_KEY_ID], live["team_id"])
            services[dtm.SOURCE_APPLE] = self._maps
        if self.options.get(CONF_TOMTOM_KEY):
            services[dtm.SOURCE_TOMTOM] = dtm.TomTom(session, self.options[CONF_TOMTOM_KEY])
        if self.options.get(CONF_GOOGLE_KEY):
            services[dtm.SOURCE_GOOGLE] = dtm.Google(session, self.options[CONF_GOOGLE_KEY], live.get("bundle_id"),
                                                     usage=self.google_usage())
        if source == dtm.SOURCE_LONGEST:
            return list(services.values())
        return [services[source]] if source in services else []

    def asks_google(self) -> bool:
        """Whether the leave checks here ask Google (Google or Longest of all, with a Google key).
        With "phone" Home Assistant never calls Google."""
        source = self.options.get(CONF_DRIVE_SOURCE) or dtm.SOURCE_PHONE
        if source not in (dtm.SOURCE_GOOGLE, dtm.SOURCE_LONGEST) or not self.options.get(CONF_GOOGLE_KEY):
            return False
        # Fable review: while Google is stopped here, the phone asks it for the leave checks.
        return self.google_usage().stopped() is None

    def google_usage(self) -> dtm.GoogleUsage:
        """v0.6.0 item 0.5.6-14: Google requests per day, saved with the state."""
        store = self.state.get("google_requests")
        if not isinstance(store, dict):
            store = self.state["google_requests"] = {}
        return dtm.GoogleUsage(store, self.options.get(CONF_GOOGLE_BUDGET))

    def _home_place(self) -> dtm.Place | None:
        home = self.hass.states.get("zone.home")
        if home is None or home.attributes.get("latitude") is None:
            if self.hass.config.latitude is None:
                return None
            return dtm.Place("home", self.hass.config.latitude, self.hass.config.longitude)
        return dtm.Place("home", home.attributes["latitude"], home.attributes["longitude"])

    async def _drive_check_here(self, notify: bool = True) -> bool:
        """A leave check worked out here: the drive arriving by the trip's arrive-by with the
        traffic expected then; leave-by moves and you're told. False: ask the phone instead.
        `notify` False: v0.5.4 item 27's check at good morning, which the briefing says."""
        services = self._drive_services()
        trip = self.state.get("trip") or {}
        arrive_by = _time(trip.get("arrive_by"))
        origin = self._home_place()
        if not services or arrive_by is None or origin is None or not (trip.get("location") or trip.get("latitude") is not None):
            return False
        destination = dtm.Place(trip.get("location") or "", trip.get("latitude"), trip.get("longitude"))
        try:
            seconds = await dtm.drive_seconds(services, origin, destination, arrive_by)
        except dtm.GoogleStopped as err:
            # v0.6.0 item 0.5.6-14: Google stopped for now; said once a day, not every leave check.
            if not self.google_usage().logged_today():
                _LOGGER.info("Bedtime Bridge isn't asking Google, the phone checks the drive: %s", err)
            self._changed()
            return False
        except dtm.DriveTimeError as err:
            _LOGGER.warning("Bedtime Bridge couldn't check the drive here, asking the phone: %s", err)
            self._changed()  # the Google count (v0.6.0 item 0.5.6-14)
            return False
        except Exception:  # noqa: BLE001 - v0.6.0 item 36: never lose the leave check to a bug
            _LOGGER.exception("Bedtime Bridge couldn't check the drive here, asking the phone")
            return False
        planned = int(trip.get("planned_minutes") or self.state.get("drive_minutes") or 0)
        minutes = round(seconds / 60)
        leave_by = arrive_by - timedelta(seconds=seconds)
        await self._leave_by({"leave_by": leave_by.isoformat(), "drive_minutes": minutes, "note": "checked by Home Assistant"})
        if notify:
            await self._notify_leave(leave_by, minutes, planned, trip)
        # The phone's leave-time backup can go (v0.4.7 item 2).
        await self.live.leave_checked()
        if self.live.configured and self.state.get("morning_updates_until"):
            await self.live.morning()
        return True

    async def _notify_leave(self, leave_by: datetime, minutes: int, planned: int, trip: dict[str, Any]) -> None:
        """"Leave by 5:48 · 32 min drive (5 min worse than planned)", or "Leave now · …", as the app says it."""
        now = dt_util.utcnow()
        head = "Leave now" if leave_by <= now + timedelta(minutes=1) else f"Leave by {timefmt.short(leave_by)}"
        difference = minutes - planned if planned else 0
        change = f" ({abs(difference)} min {'worse' if difference > 0 else 'lighter'} than planned)" if abs(difference) >= 3 else ""
        start = _time(trip.get("start"))
        body = f"{trip.get('title') or 'Your drive'}" + (f" at {timefmt.long(start)}." if start else ".")
        await self.wind_down._notify(f"{head} · {minutes} min drive{change}", body, tag="leave_check", time_sensitive=True)

    def _drop_leave_by(self) -> None:
        """The shift behind leave-by is gone: leave-by goes too, so nothing shows a morning
        that isn't happening."""
        self.state["leave_by"] = None
        self.state["leave_reminders"] = []
        self._cancel_leave_checks()
        self._changed()

    def _leave_shift_still_on(self) -> bool:
        """With no alarms left on a work morning, the checks run only while the calendar still
        shows the shift (v0.4.2 item 1): the shift sensor's next start is the planned one."""
        plan = self.state.get("ladder_plan") or {}
        if self.state.get("alarm_times") or plan.get("type") != "shift":
            return True
        # v0.6.0 item 19: a schedule kept in the app has no calendar to check; the app sends a new
        # plan when the day changes.
        if plan.get("schedule") == "app":
            return True
        planned = _time(plan.get("shift_start"))
        sensor = self.hass.states.get(self.options.get(CONF_SHIFT_SENSOR) or "")
        if planned is None or sensor is None:
            return True
        start = _time(sensor.attributes.get("next_shift_start"))
        return start is not None and abs((start - planned).total_seconds()) < 60

    # MARK: Change this morning (item 3)

    async def _morning_changed(self, data: dict[str, Any]) -> None:
        """The morning's alarms were moved later or earlier, or cancelled, while snoozing or
        after you were up. The backstops move with the wake-up (or stay with no alarms), Sleep
        In follows, and the house goes back to bedtime."""
        times = sorted(t for t in (_time(v) for v in data.get("alarm_times") or []) if t)
        delay = int(data.get("delay_minutes") or 0)
        # alarms: sleep longer, same start; wake_up: the start moved too (v0.4.1 item 7).
        scope = data.get("scope") or "wake_up"
        gap = int(data.get("early_gap_minutes") or 5)
        late = _time(((self.state.get("ladder") or {}).get("times") or {}).get("late"))
        if scope == "alarms" and times and late and (self.state.get("ladder") or {}).get("status") == "armed" and times[-1] > late:
            message = (f"The last new alarm ({dt_util.as_local(times[-1]).strftime('%H:%M')}) is after the late backstop "
                       f"({dt_util.as_local(late).strftime('%H:%M')}); nothing was changed. Shift the entire wake-up instead.")
            _LOGGER.warning("Bedtime Bridge refused a morning change: %s", message)
            self.hass.bus.async_fire(EVENT, {"type": EVENT_CHANGE_REFUSED, "reason": message})
            return {"error": message}
        phase = data.get("phase") or "up"
        if phase == "bedtime" and self._state_of(CONF_SNOOZE_HELPER) == "on":
            # v0.5.5 item 22: "before bed" while the snooze fade is running isn't before bed (Wake
            # up later after a snooze, from an app before 0.5.5): the fade stops, so the lights
            # go back down and the next snooze starts it again.
            phase = "snoozing"
        if phase == "snoozing" and (self.state.get("routine_ran") or self._state_of(CONF_AWAKE_HELPER) == "on"):
            # Don't trust the phase alone (v0.4.1 item 2): once the wake routine ran or Awake For
            # Day is on, the house has to go back to bed.
            phase = "up"
        reason, note = data.get("reason"), data.get("note")
        # The snooze count keeps going across a change (v0.4.1 item 5): it's one morning.
        self.state["snoozed_until"] = None
        self.state["alarm_times"] = [t.isoformat() for t in times]
        self.state["alarm_time"] = times[0].isoformat() if times else None
        self.state["series_start"] = times[0].isoformat() if times else None
        if phase != "edit":
            # A new series is a new wake-up; an edited one is the same (v0.4.1 item 13).
            self.state["routine_ran"] = False
        self._check_alarms_set(data)
        if not times:
            self.state["alarm_cause"] = "noalarm"
            self.pending.cleared()
        if phase not in ("bedtime", "edit"):
            # Mornings changed, for the weekly summary: once per morning, not Sleep later or edits.
            history = self.state.setdefault("history", [])
            today = dt_util.as_local(dt_util.utcnow()).date().isoformat()
            if not any(h.get("kind") == "change" and dt_util.as_local(dt_util.parse_datetime(h["time"])).date().isoformat() == today for h in history):
                weekly.record(history, "change", dt_util.utcnow(), reason=data.get("reason"))
        self.state["last_morning_change"] = {
            "delay_minutes": delay if times else None,
            "alarm_times": self.state["alarm_times"],
            "reason": reason,
            "note": note,
            "phase": phase,
            "scope": scope,
            "time": dt_util.utcnow().isoformat(),
        }
        # The one thing allowed to move the backstops later. No alarms: they stay (the calendar
        # still shows the shift; deleting it is what turns them off).
        if times and scope == "alarms":
            # Deadline and late stay; early follows the new last alarm.
            self.ladder_early_floor(times[-1] + timedelta(minutes=gap), data.get("changed_by") or "app", reason)
        elif times and delay:
            self.change_morning_ladder(delay, data.get("changed_by") or "app", reason)
        # Leave checks (v0.4.2 item 1): an entire wake-up moved takes leave-by and the checks
        # with it; shifting the alarms only, or no alarms, keeps them as they are.
        leave_by = _time(self.state.get("leave_by"))
        if times and scope == "wake_up" and delay and leave_by is not None:
            self.state["leave_by"] = (leave_by + timedelta(minutes=delay)).isoformat()
            trip = self.state.get("trip") or {}
            if _time(trip.get("arrive_by")):
                trip["arrive_by"] = (_time(trip["arrive_by"]) + timedelta(minutes=delay)).isoformat()
            self._schedule_leave_checks()
        if phase not in ("bedtime", "edit"):
            await self._restart_wake_automations()
        # Blocking: Sleep In must be set before the bedtime routine can move the curtains.
        if times:
            await self._sync_sleep_in(blocking=True)
        else:
            await self._set_sleep_in(bool(data.get("sleep_in", True)), blocking=True)
            if not self._leave_shift_still_on():
                # No alarms, and the shift is already off the calendar: no leave-by either.
                self._drop_leave_by()
        at_home = self.is_home()
        if phase in ("bedtime", "edit"):
            # Sleep later before bed (item 12) or an edited alarm (item 13): nothing in the house,
            # except that an edit ending the only snooze fades the bedroom back down.
            helper = self.options.get(CONF_SNOOZE_HELPER)
            if phase == "edit" and data.get("snooze_ended") and helper:
                await self._turn_off(helper)
        elif phase == "snoozing":
            # Only the snooze ramp has started: fade the nightstands back down.
            helper = self.options.get(CONF_SNOOZE_HELPER)
            if helper:
                await self._turn_off(helper)
        elif at_home:
            # After a stop: Sleep In first (above), then the bedtime routine, then the phone.
            bedtime = self.options.get(CONF_BEDTIME_HELPER)
            if bedtime:
                await self._turn_on(bedtime, blocking=True)
            await self._command(CMD_BACK_TO_BED)
        self._event(
            MSG_MORNING_CHANGED, delay_minutes=delay if times else None, reason=reason, note=note,
            phase=phase, scope=scope, sleep_in=self._state_of(CONF_SLEEP_IN),
        )

    async def _restart_wake_automations(self) -> None:
        """Stops what the automations you picked are already running (turning an automation off
        cancels its runs) and clears the backstop's notification. v0.5.4 item 62: none picked,
        none restarted (no default ids)."""
        for entity_id in self.options.get(CONF_RESTART_AUTOMATIONS) or []:
            state = self.hass.states.get(entity_id)
            if state is None or state.state != "on":
                continue
            try:
                await self.hass.services.async_call(
                    "automation", "turn_off", {"entity_id": entity_id, "stop_actions": True}, blocking=True
                )
                await self.hass.services.async_call("automation", "turn_on", {"entity_id": entity_id}, blocking=True)
            except HomeAssistantError as err:
                _LOGGER.warning("Bedtime Bridge couldn't restart %s: %s", entity_id, err)
        await self._clear_tags()

    async def _clear_tags(self, bedtime: bool = False) -> None:
        """Backstops' Notification tags to clear, each owed until the phone is reachable
        (v0.5.4 item 77). At bedtime without wake_backstop (item 81)."""
        for tag in clear_tags(self.options.get(CONF_CLEAR_TAGS)):
            if bedtime and tag in RESTART_NOTIFICATION_TAGS:
                continue
            await self.clears.send(tag)

    async def _turn_off(self, entity_id: str) -> None:
        if not made.writable(entity_id):
            return  # v0.5.4 item 35: a binary sensor is only read
        try:
            await self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": entity_id}, blocking=False)
        except HomeAssistantError as err:
            _LOGGER.warning("Bedtime Bridge couldn't turn off %s: %s", entity_id, err)

    # MARK: Undo bedtime

    async def _bedtime_undone(self, data: dict[str, Any]) -> None:
        """Up again after bedtime ran: alarms are off on the phone; here the house is
        disarmed, Awake For Day goes back on and Sleep In off. Once you're back in bed, bedtime
        runs again (RUN_BEDTIME, the same push the bedtime nudge sends)."""
        # Wind-down doesn't restart; only its reminders run (v0.4.3 item 5).
        self.wind_down.bedtime_undone()
        self.state.update(empty_state())
        now = dt_util.utcnow()
        self.state["undone_at"] = now.isoformat()
        self._end_bed("rearm")
        at_home = self.is_home()
        disarmed = await self._command(CMD_DISARM) if at_home else False
        if at_home:
            await self._set_helper(CONF_AWAKE_HELPER, True)
        await self._set_sleep_in(False)
        if at_home:
            self.state["rerun_since"] = now.isoformat()
            self.state["rerun_until"] = (now + timedelta(hours=REARM_GIVE_UP_HOURS)).isoformat()
            self._watch_bed("rerun")
        self._event(MSG_BEDTIME_UNDONE, disarmed=disarmed)

    # MARK: Skipped mornings

    async def _skipped(self, data: dict[str, Any]) -> None:
        """Skipped the night before: that morning's wake-up doesn't exist. A skipped shift
        turns the shift override Off (which disarms the wake failsafe) until the shift has
        passed. Stopping alarms in the morning never does this."""
        self.state.update(empty_state())
        # v0.4.10 item 10: No alarms now skips, so it clears what cancelled did: bedtime pending,
        # and your own alarms' backstops (v0.4.8 item 7).
        self.pending.cleared()
        if (self.state.get("ladder_plan") or {}).get("type") == "own":
            self.state["ladder_plan"] = self._app_ladder_plan()
        # Counts as tonight's plan, so the missing-plan check doesn't nag about the shift.
        self.state["last_plan"] = dt_util.utcnow().isoformat()
        self.state["plan_error"] = None
        override = self.options.get(CONF_SHIFT_OVERRIDE)
        current = self._state_of(CONF_SHIFT_OVERRIDE)
        failsafe_off = False
        if data.get("shift") and override and current not in (None, "Off", *UNKNOWN_STATES):
            start = _time(data.get("shift_start"))
            restore_at = (start + timedelta(hours=1)) if start else dt_util.utcnow() + timedelta(hours=18)
            self.state["skip_restore_to"] = current
            self.state["skip_restore_at"] = restore_at.isoformat()
            # Our own change: not a reason to flag the plan as out of date.
            self.state["planned_override"] = "Off"
            failsafe_off = await self._select(override, "Off")
            self._schedule_skip_restore()
        if data.get("sleep_in"):
            await self._set_sleep_in(True)
        self._event(MSG_SKIPPED, shift=data.get("shift"), failsafe_off=failsafe_off, sleep_in=bool(data.get("sleep_in")))

    async def _unskipped(self, data: dict[str, Any]) -> None:
        """Undo: the override goes back first, so the plan that follows sees the shift."""
        await self._restore_override()
        self._event(MSG_UNSKIPPED)

    @callback
    def _schedule_skip_restore(self) -> None:
        if self._restore_unsub:
            self._restore_unsub()
            self._restore_unsub = None
        restore_at = _time(self.state.get("skip_restore_at"))
        if restore_at is None or not self.state.get("skip_restore_to"):
            return
        if restore_at <= dt_util.utcnow():
            self.hass.async_create_task(self._restore_override())
            return
        self._restore_unsub = async_track_point_in_utc_time(self.hass, self._restore_due, restore_at)

    @callback
    def _restore_due(self, _now: datetime) -> None:
        self._restore_unsub = None
        self.hass.async_create_task(self._restore_override())

    async def _restore_override(self) -> None:
        """Puts the override back, unless you've changed it yourself since."""
        restore_to = self.state.get("skip_restore_to")
        self.state["skip_restore_to"] = None
        self.state["skip_restore_at"] = None
        if self._restore_unsub:
            self._restore_unsub()
            self._restore_unsub = None
        override = self.options.get(CONF_SHIFT_OVERRIDE)
        if restore_to and override and self._state_of(CONF_SHIFT_OVERRIDE) == "Off":
            self.state["planned_override"] = restore_to
            await self._select(override, restore_to)
        self._changed()

    async def _select(self, entity_id: str, option: str) -> bool:
        domain = entity_id.partition(".")[0]
        try:
            await self.hass.services.async_call(
                domain, "select_option", {"entity_id": entity_id, "option": option}, blocking=True
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Bedtime Bridge couldn't set %s to %s: %s", entity_id, option, err)
            return False
        return True

    # MARK: Sleep In

    async def _sync_sleep_in(self, blocking: bool = False) -> None:
        """Sleep In follows the plan: on when the first alarm is after the curtains would open
        (sunrise + offset), off when it's before. No alarm leaves it as it is (the bedtime
        shortcut asks). Only while you're home."""
        first = _time(self.state.get("series_start"))
        # The sunrise on the first alarm's own day: in the morning (item 3) next_rising is tomorrow's.
        curtains = _time(self._curtains_open(first)) if first else None
        if first is None or curtains is None:
            return
        await self._set_sleep_in(first > curtains, blocking)

    async def _set_sleep_in(self, on: bool, blocking: bool = False) -> None:
        helper = self.options.get(CONF_SLEEP_IN)
        state = self.hass.states.get(helper or "")
        if not made.writable(helper) or not self.is_home() or (state is not None and state.state == ("on" if on else "off")):
            return
        try:
            await self.hass.services.async_call(
                "homeassistant", "turn_on" if on else "turn_off", {"entity_id": helper}, blocking=blocking
            )
        except HomeAssistantError as err:
            _LOGGER.warning("Bedtime Bridge couldn't set %s: %s", helper, err)

    # MARK: Away bedtime

    async def _home_check(self, data: dict[str, Any]) -> dict[str, Any]:
        """For the bedtime summary when you're away: anything at home that needs a look."""
        issues: list[str] = []
        entity_ids = self.options.get(CONF_HOME_CHECKS) or []
        for entity_id in [entity_ids] if isinstance(entity_ids, str) else entity_ids:
            if (issue := home_issue(self.hass.states.get(entity_id), entity_id)) is not None:
                issues.append(issue)
        return {"issues": issues, "checked": len(entity_ids)}

    # MARK: Phone commands

    async def _command(self, message: str, resend: int = 0) -> bool:
        """A push the phone's Shortcuts automation turns into a shortcut run (Set Disarm, Set
        Arm Stay, the morning shortcut). Off unless phone commands are turned on.
        `resend`: v0.5.3 item 35, Good Morning sent again under its own tag (a push replacing
        one with the same tag may not trigger the automation again)."""
        sent = await self._push(
            {
                "title": "Bedtime Bridge",
                "message": message,
                # Time-sensitive like the bedtime nudge's RUN_BEDTIME, which reaches the
                # Shortcuts trigger with the phone locked. One tag per command, so each can
                # be cleared on its own once its shortcut has run.
                "data": {"tag": _command_tag(message) + (f"_{resend}" if resend else ""),
                         "push": {"interruption-level": "time-sensitive"}},
            },
            message,
        )
        if sent and resend:
            self._resent = max(self._resent, resend)
            return True
        if sent and message in MORNING_COMMANDS:
            self._schedule_resends(message)
            # v0.5.4 item 77: Good Morning; owed clears go again.
            await self.clears.resend("good_morning")
        if sent:
            self._pending_commands.add(message)
            if message == CMD_BEDTIME:
                self.state["bedtime_push_at"] = dt_util.utcnow().isoformat()
            if message in WATCHED_COMMANDS:
                self._check_command_ran(message)
        return sent

    @callback
    def _check_command_ran(self, message: str) -> None:
        """v0.4.8 item 11: a push that isn't reported as ran within 10 minutes is an automatic
        report."""
        if old := self._command_check_unsubs.pop(message, None):
            old()

        @callback
        def check(_now: datetime) -> None:
            self._command_check_unsubs.pop(message, None)
            if message in self._pending_commands:
                self.hass.async_create_task(reports.automatic(
                    self, f"not_ran_{message.lower()}",
                    f"{message} was sent to the phone 10 minutes ago and its shortcut hasn't reported it ran"))
                # v0.5.3 item 35: say so, not only in a report (at home only: away, the house
                # isn't waiting on it).
                if message in MORNING_COMMANDS and self.is_home():
                    self.hass.async_create_task(self.wind_down._notify(
                        "Good Morning didn't run",
                        "Good Morning didn't run on your phone. The house is still armed and Sleep Focus may still be on.",
                        tag="good_morning_not_ran", time_sensitive=True))

        self._command_check_unsubs[message] = async_call_later(self.hass, COMMAND_RAN_WAIT, check)

    async def _clear_command(self, message: str) -> None:
        """Takes a command's push off the phone once its shortcut has run."""
        if message in self._pending_commands:
            self._pending_commands.discard(message)
            # v0.5.4 item 77: owed until the phone is reachable.
            await self.clears.send(_command_tag(message))
            if message in MORNING_COMMANDS:
                self._cancel_resends()
                # Only the resends that went out are on the phone to clear.
                for n in range(1, self._resent + 1):
                    await self.clears.send(f"{_command_tag(message)}_{n}")
                self._resent = 0

    @callback
    def _schedule_resends(self, message: str) -> None:
        """v0.5.3 item 35: Good Morning is sent again after 2 and 5 minutes while its shortcut
        hasn't reported it ran (12 Nov: one push never reached the shortcut)."""
        self._cancel_resends()
        for n, after in enumerate(GOOD_MORNING_RESENDS, start=1):
            @callback
            def resend(_now: datetime, n: int = n) -> None:
                if message in self._pending_commands:
                    _LOGGER.info("Bedtime Bridge: %s hasn't reported it ran; sending it again (%s)", message, n)
                    self.hass.async_create_task(self._command(message, resend=n))
            self._resend_unsubs.append(async_call_later(self.hass, after, resend))

    @callback
    def _cancel_resends(self) -> None:
        for unsub in self._resend_unsubs:
            unsub()
        self._resend_unsubs = []

    async def _push(self, payload: dict[str, Any], what: str) -> bool:
        notify = (self.options.get(CONF_NOTIFY) or "").strip()
        if not notify or not self.options.get(CONF_PHONE_COMMANDS):
            return False
        domain, _, service = notify.partition(".")
        try:
            await self.hass.services.async_call(domain, service, payload, blocking=False)
        except HomeAssistantError as err:
            _LOGGER.warning("Bedtime Bridge couldn't send %s to the phone: %s", what, err)
            return False
        # v0.5.4 item 81: sent again, so an owed clear for its tag would remove this one.
        if tag := (payload.get("data") or {}).get("tag"):
            self.clears.drop(tag)
        return True

    async def _command_ran(self, data: dict[str, Any]) -> None:
        """The shortcut a command asked for has run (its Report Shortcut Ran step). Outcome
        "already_done": the phone got the push but had nothing to do (Good Morning already ran)."""
        command = data.get("command")
        # "ran" is Started (the shortcut's first step); "completed" its last; "already_done".
        outcome = data.get("outcome") or "ran"
        if command == CMD_BEDTIME and outcome == "ran":
            # The bedtime shortcut's first step: this is bedtime, whether a push or you ran it
            # (it never sends "bedtime" itself).
            await self.bedtime_started(data)
        now = dt_util.utcnow().isoformat()
        if command in COMMANDS:
            await self._clear_command(command)
        if command == CMD_MORNING:
            # The quiet good morning's shortcut reports as Good Morning (the same moment).
            await self._clear_command(CMD_QUIET_MORNING)
        started: dict[str, str] = self.state.setdefault("command_started", {})
        completing: list[str] = self.state.setdefault("commands_completing", [])
        last = self.state.get("last_command") or {}
        if outcome == "ran":
            # Only watched once that shortcut has reported Completed at least once, so one with
            # just a Started step never raises a false alarm.
            if command in completing:
                started[command] = now
                self._watch_command(command)
            last = {"command": command, "outcome": outcome, "time": now, "started": now, "completed": None}
        else:
            started.pop(command, None)
            self._unwatch_command(command)
            if outcome == "completed" and command not in completing:
                completing.append(command)
            same = last.get("command") == command
            last = {
                "command": command, "outcome": outcome, "time": now,
                "started": last.get("started") if same else None,
                "completed": now if outcome == "completed" else None,
            }
        self.state["last_command"] = last
        self._event(MSG_COMMAND_RAN, command=command, outcome=outcome)

    @callback
    def _watch_command(self, command: str) -> None:
        """Flags a shortcut that started but hasn't completed in time (it stopped partway)."""
        self._unwatch_command(command)
        started = _time((self.state.get("command_started") or {}).get(command))
        if started is None:
            return
        minutes = int(self.options.get(CONF_COMPLETE_MINUTES) or DEFAULT_COMPLETE_MINUTES)
        due = max(started + timedelta(minutes=minutes), dt_util.utcnow() + timedelta(seconds=1))
        self._command_unsubs[command] = async_track_point_in_utc_time(
            self.hass, callback(lambda _now: self.hass.async_create_task(self._command_stuck(command))), due
        )

    @callback
    def _unwatch_command(self, command: str) -> None:
        unsub = self._command_unsubs.pop(command, None)
        if unsub:
            unsub()

    async def _command_stuck(self, command: str) -> None:
        self._command_unsubs.pop(command, None)
        started = (self.state.get("command_started") or {}).pop(command, None)
        if started is None:
            return
        name = COMMAND_NAMES.get(command, command)
        self.hass.bus.async_fire(EVENT, {"type": EVENT_COMMAND_INCOMPLETE, "command": command, "started": started})
        await self._push(
            {
                "title": "Phone shortcut didn't finish",
                "message": f"{name} started on your phone but didn't finish. Check what it should have done.",
                "data": {"tag": f"{COMMAND_TAG}_incomplete", "push": {"interruption-level": "time-sensitive"}},
            },
            f"{command} incomplete",
        )
        _LOGGER.warning("Bedtime Bridge: %s started at %s but never reported Completed", command, started)
        self._changed()

    async def _cancelled(self, data: dict[str, Any]) -> None:
        """Alarms cleared. "No alarms tomorrow" (v0.4.3 item 3) sends sleep_in; the ladder then
        follows the calendar alone (armed while a covered shift is on it)."""
        self.state.update(empty_state())
        self.pending.cleared()
        # v0.4.8 item 7: your own alarms' backstops go with them; a shift's follow the calendar.
        if (self.state.get("ladder_plan") or {}).get("type") == "own":
            self.state["ladder_plan"] = self._app_ladder_plan()
        if "sleep_in" in data:
            await self._set_sleep_in(bool(data["sleep_in"]))
        self._event(MSG_CANCELLED, sleep_in=data.get("sleep_in"), morning=data.get("morning"))

    async def _bedtime(self, data: dict[str, Any]) -> dict[str, Any]:
        """Starts the bedtime routine by turning on its helper, only when you're home."""
        await self.bedtime_started(data)
        at_home = self.is_home()
        helper = self.options.get(CONF_BEDTIME_HELPER)
        started = False
        duty_night = self.duty_night_now()
        if helper and not duty_night and (at_home or not self.options.get(CONF_HOME_ONLY, True)):
            started = await self._turn_on(helper)
        self.state["skipped_away"] = bool(helper) and not started and (not at_home or duty_night)
        # The bedtime shortcut's own step, so a RUN_BEDTIME push has done its job.
        await self._clear_command(CMD_BEDTIME)
        # v0.5.4 item 60: tonight's briefing only (none yet while the plan is coming).
        record = self.state.get("briefing") or {}
        # v0.5.4 item 71: by the night's date, so a second bedtime report tonight still carries it.
        night = self.state.get("bedtime_started_at")
        same_night = record.get("bedtime_for") == night or (
            record.get("bedtime_for") and _night_of(record.get("bedtime_for")) == _night_of(night))
        tonight = record.get("bedtime") if same_night else None
        self._event(MSG_BEDTIME, started=started, at_home=at_home, briefing=tonight)
        return {"started": started, "at_home": at_home}

    async def _leave_by(self, data: dict[str, Any]) -> None:
        self._traffic_moved(_time(data.get("leave_by")))
        self.state["leave_by"] = data.get("leave_by")
        # A fresh drive time: the reminders still ahead follow the new leave-by (item 17).
        await self._clear_command(CMD_LEAVE_CHECK)
        self._schedule_leave_checks()
        if data.get("drive_minutes") is not None:
            self.state["drive_minutes"] = data["drive_minutes"]
        self._event(MSG_LEAVE_BY, note=data.get("note"))

    # MARK: Effects

    def is_home(self) -> bool:
        """True when any presence entity is home (or unknown); True when none are set."""
        entity_ids = self.options.get(CONF_PRESENCE) or []
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if not entity_ids:
            return True
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in UNKNOWN_STATES or state.state in HOME_STATES:
                return True
        return False

    def duty_night_now(self) -> bool:
        """v0.4.11 item 75: tonight (or this morning, until noon) follows a duty day."""
        now = dt_util.now()
        day = now.date() if now.hour >= 12 else now.date() - timedelta(days=1)
        return bool(self.state.get("duty_night")) or day.isoformat() in (self.state.get("duty_nights") or [])

    def duty_morning(self, when: datetime | None) -> bool:
        """The morning of `when` comes after a duty day."""
        if when is None:
            return False
        day = dt_util.as_local(when).date() - timedelta(days=1)
        return day.isoformat() in (self.state.get("duty_nights") or [])

    def _away_blocks(self) -> bool:
        # v0.4.11 item 75: on a duty night you're at work, so it counts as away whatever
        # presence says (someone else at home keeps the house quiet).
        if self.duty_night_now():
            return True
        return bool(self.options.get(CONF_HOME_ONLY, True)) and not self.is_home()

    async def _wake(self, helper_option: str) -> None:
        """Turns on the snooze/stop helper unless the user picked events only, or is away."""
        self.state["skipped_away"] = self._away_blocks()
        if self.state["skipped_away"] or self.options.get(CONF_WAKE_ACTION, WAKE_BOTH) == WAKE_EVENTS:
            return
        entity_id = self.options.get(helper_option)
        if entity_id:
            await self._turn_on(entity_id)

    async def _turn_on(self, entity_id: str, blocking: bool = False) -> bool:
        if not made.writable(entity_id):
            return False  # v0.5.4 item 35: a binary sensor is only read
        try:
            await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": entity_id}, blocking=blocking)
        except HomeAssistantError as err:
            # A renamed or deleted helper must not stop the event and state update.
            _LOGGER.warning("Bedtime Bridge couldn't turn on %s: %s", entity_id, err)
            return False
        return True

    def _event(self, kind: str, last_event: str | None = None, **extra: Any) -> None:
        """Fires a bedtime_bridge event of type `kind`; Last event shows `last_event` when
        it says more than the type (a plan: alarms_set, planned_for_clock or no_alarm)."""
        self.state["last_event"] = last_event or kind
        self.state["last_event_time"] = dt_util.utcnow().isoformat()
        # Snooze/stop events are skipped with "helpers only" and while you're away (so event
        # automations don't light an empty house); plan/cancel/leave-by/bedtime always fire.
        helpers_only = self.options.get(CONF_WAKE_ACTION) == WAKE_HELPERS
        wake_event = kind in (MSG_SNOOZED, MSG_STOPPED)
        if not (wake_event and (helpers_only or self.state.get("skipped_away"))):
            self.hass.bus.async_fire(
                EVENT,
                {
                    "type": kind,
                    "alarm_cause": self.state.get("alarm_cause"),
                    "alarm_time": self.state.get("alarm_time"),
                    "wake_time": self.state.get("wake_time"),
                    "alarm_times": self.state.get("alarm_times"),
                    "label": self.state.get("label"),
                    "leave_by": self.state.get("leave_by"),
                    "snooze_count": self.state.get("snooze_count"),
                    **extra,
                },
            )
        self._changed()


def home_issue(state: Any, entity_id: str) -> str | None:
    """"Garage Door is open", "Front Door Lock isn't responding", or None when it's fine."""
    name = state.attributes.get("friendly_name", entity_id) if state is not None else entity_id
    if state is None or state.state in UNKNOWN_STATES:
        return f"{name} isn't responding"
    domain = entity_id.partition(".")[0]
    if domain == "cover" and state.state in ("open", "opening", "closing"):
        return f"{name} is open"
    if domain == "lock" and state.state in ("unlocked", "unlocking", "open", "opening"):
        return f"{name} is unlocked"
    if domain == "alarm_control_panel" and state.state == "disarmed":
        # v0.5.4 item 28: the security system, when it's in Home Assistant.
        return f"{name} isn't armed"
    if domain == "lock" and state.state == "jammed":
        return f"{name} is jammed"
    if domain == "binary_sensor" and state.state == "on":
        wet = state.attributes.get("device_class") in ("moisture", "smoke", "gas", "carbon_monoxide", "safety", "problem")
        return f"{name} {'is reporting a problem' if wet else 'is open'}"
    return None

