"""Snoozing: on from a snooze tap until the alarm is due to ring again. Bedtime pending (v0.4.8
item 1): on from setting alarms until bedtime runs or 2 h before the first alarm. Waiting on you
(v0.6.0 item 38): on while an alarm is ringing, snoozed or not answered."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.util import dt as dt_util

from . import BedtimeBridgeConfigEntry
from .bridge import _time
from .const import CONF_AWAKE_HELPER
from .entity import BedtimeBridgeEntity

# v0.6.0 item 38: Waiting on you stays on at most this long after the alarm it's waiting on.
WAITING_LIMIT = timedelta(hours=3)


def waiting_on_you(times: list[datetime], now: datetime, *, snoozed: datetime | None = None,
                   open_since: datetime | None = None, awake_since: datetime | None = None) -> tuple[bool, datetime | None]:
    """v0.6.0 item 38: whether an alarm is ringing, snoozed or not answered, and when that could
    next change. `times`: the alarms the integration watches (the app's, each ring-again, the
    unanswered). On from an alarm's time (or a snooze) until it's stopped, or 3 hours after;
    `open_since`: a Stop that wasn't the last, so it stays on between alarms of one wake-up.
    Awake for the day turned on (`awake_since`) ends it for the alarms before then (one left on
    from yesterday doesn't hide this morning's)."""
    def before_awake(t: datetime) -> bool:
        return awake_since is not None and t <= awake_since

    due = [t for t in times if t <= now and now - t < WAITING_LIMIT and not before_awake(t)]
    if snoozed is not None and snoozed > now and not (awake_since is not None and now - awake_since < WAITING_LIMIT):
        due.append(snoozed)
    if open_since is not None and open_since <= now and now - open_since < WAITING_LIMIT and not before_awake(open_since):
        due.append(open_since)
    later = [t for t in times if t > now]
    upcoming = sorted([*later, *(t + WAITING_LIMIT for t in due if t <= now)])
    if snoozed is not None and snoozed > now:
        upcoming = sorted([*upcoming, snoozed + WAITING_LIMIT])
    return bool(due), (upcoming[0] if upcoming else None)


async def async_setup_entry(
    hass: HomeAssistant, entry: BedtimeBridgeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([SnoozingSensor(entry.runtime_data), PlanOutdatedSensor(entry.runtime_data),
                        BedtimePendingSensor(entry.runtime_data), WaitingOnYouSensor(entry.runtime_data)])


class WaitingOnYouSensor(BedtimeBridgeEntity, BinarySensorEntity):
    """v0.6.0 item 38: on while an alarm is ringing, snoozed or not answered."""

    _attr_translation_key = "waiting_on_you"
    _attr_icon = "mdi:alarm-light"

    def __init__(self, bridge) -> None:
        super().__init__(bridge, "waiting_on_you")
        self._unsub_timer: CALLBACK_TYPE | None = None

    def _now(self) -> tuple[bool, datetime | None]:
        state = self.bridge.state
        # Pre-TestFlight review (H3): Clock app alarms are left out (their snooze and stop never
        # reach Home Assistant), as the not-answered check leaves them out; and away from home
        # it's off (alarms ringing at a hotel aren't the house's business).
        if state.get("alarm_style") == "clock" or not self.bridge.is_home():
            return False, None
        helper = self.hass.states.get(self.bridge.options.get(CONF_AWAKE_HELPER) or "")
        # Pre-TestFlight review (H3): read by what it means ("On means asleep" reads the other way).
        awake_since = helper.last_changed if helper is not None and self.bridge._state_of(CONF_AWAKE_HELPER) == "on" else None
        now = dt_util.utcnow()
        # Pre-TestFlight review (H3): an alarm (or a snooze's ring) a snooze or Stop has answered
        # isn't waited on any more; the not-answered check reads it the same way.
        times = [t for t in self.bridge._watched_alarms() if t > now or not self.bridge._answered(t)]
        return waiting_on_you(times, now, snoozed=_time(state.get("snoozed_until")),
                              open_since=_time(state.get("wake_up_open")), awake_since=awake_since)

    @property
    def is_on(self) -> bool:
        return self._now()[0]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule()
        self.async_on_remove(self._cancel_timer)
        # Fable review: Awake for the day turned on or off redraws it.
        if helper := self.bridge.options.get(CONF_AWAKE_HELPER):
            self.async_on_remove(async_track_state_change_event(self.hass, [helper], self._awake_changed))

    @callback
    def _handle_update(self) -> None:
        self._schedule()
        super()._handle_update()

    @callback
    def _schedule(self) -> None:
        """Looked at again when an alarm's time comes, or its 3 hours are up."""
        self._cancel_timer()
        _, at = self._now()
        if at is not None:
            self._unsub_timer = async_track_point_in_utc_time(self.hass, self._due, at + timedelta(seconds=1))

    @callback
    def _awake_changed(self, _event) -> None:
        self._handle_update()

    @callback
    def _due(self, _now: datetime) -> None:
        self._unsub_timer = None
        self._schedule()
        self.async_write_ha_state()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None


class PlanOutdatedSensor(BedtimeBridgeEntity, BinarySensorEntity):
    """On when the shift override or next shift changed after tonight's plan."""

    _attr_translation_key = "plan_outdated"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, bridge) -> None:
        super().__init__(bridge, "plan_outdated")

    @property
    def is_on(self) -> bool:
        return bool(self.bridge.state.get("plan_outdated"))

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"reason": self.bridge.state.get("outdated_reason")}


class SnoozingSensor(BedtimeBridgeEntity, BinarySensorEntity):
    _attr_translation_key = "snoozing"
    _attr_icon = "mdi:alarm-snooze"

    def __init__(self, bridge) -> None:
        super().__init__(bridge, "snoozing")
        self._unsub_timer: CALLBACK_TYPE | None = None

    @property
    def _until(self) -> datetime | None:
        value = self.bridge.state.get("snoozed_until")
        return dt_util.parse_datetime(value) if isinstance(value, str) else None

    @property
    def is_on(self) -> bool:
        until = self._until
        return until is not None and until > dt_util.utcnow()

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"rings_again": self.bridge.state.get("snoozed_until")}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_off()
        self.async_on_remove(self._cancel_timer)

    @callback
    def _handle_update(self) -> None:
        self._schedule_off()
        super()._handle_update()

    @callback
    def _schedule_off(self) -> None:
        """Turns itself off when the snooze runs out, even if the phone never reports back."""
        self._cancel_timer()
        if self.is_on:
            self._unsub_timer = async_track_point_in_utc_time(self.hass, self._snooze_over, self._until)

    @callback
    def _snooze_over(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.async_write_ha_state()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None


class BedtimePendingSensor(BedtimeBridgeEntity, BinarySensorEntity):
    """On while getting into bed should start bedtime at any hour; the Bedtime Nudge blueprint
    nudges while it's on, even outside its window."""

    _attr_translation_key = "bedtime_pending"
    _attr_icon = "mdi:bed-clock"

    def __init__(self, bridge) -> None:
        super().__init__(bridge, "bedtime_pending")
        self._unsub_timer: CALLBACK_TYPE | None = None

    @property
    def is_on(self) -> bool:
        return self.bridge.pending.is_on

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        until = self.bridge.pending.until
        return {"since": self.bridge.state.get("bedtime_pending_since"), "until": until.isoformat() if until else None}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._schedule_off()
        self.async_on_remove(self._cancel_timer)

    @callback
    def _handle_update(self) -> None:
        self._schedule_off()
        super()._handle_update()

    @callback
    def _schedule_off(self) -> None:
        """Turns itself off at the cut-off, with no message from the phone."""
        self._cancel_timer()
        if until := self.bridge.pending.until:
            self._unsub_timer = async_track_point_in_utc_time(self.hass, self._cutoff, until)

    @callback
    def _cutoff(self, _now: datetime) -> None:
        self._unsub_timer = None
        self.async_write_ha_state()

    @callback
    def _cancel_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None
