"""Owed notification clears (v0.5.4 item 77).

A `clear_notification` is a background push, and iOS holds those back in Low Power Mode, keeping
only the latest one per app. So every clear is owed until the phone is reachable, and all owed
clears go again together when it is: the battery state leaves Low Power Mode or starts charging,
the app sends a message, or Good Morning. Kept in the saved state; dropped after 6 hours.

v0.5.4 item 81: an owed clear never removes a newer notification with the same tag. It's dropped
when that tag is sent again (the integration's own pushes), and the Backstops tags (yours and
`wake_backstop`, sent by automations we don't see) when the backstops arm for a new morning or at
the first alarm of the morning.
v0.5.4 item 90: with no battery-state sensor (Low Power Mode unknown), clears are still owed and
sent again at app messages and Good Morning, until they expire.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import CONF_CLEAR_TAGS, CONF_NOTIFY, CONF_PHONE_BATTERY_STATE, clear_tags

if TYPE_CHECKING:
    from .bridge import BedtimeBridge

_LOGGER = logging.getLogger(__name__)

# v0.5.4 item 81: a few hours, not 24 (last night's clear mustn't reach this morning).
OWED_FOR = timedelta(hours=6)
# A message from the app resends at most this often (the app can send many in a row).
APP_RESEND_GAP = timedelta(minutes=10)
LOW_POWER_ATTR = "Low Power Mode"
CHARGING = ("charging", "full")


def _on(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "on", "yes"))


class OwedClears:
    """Clears the phone may not have got yet."""

    def __init__(self, bridge: BedtimeBridge) -> None:
        self.bridge = bridge
        self.hass = bridge.hass
        self._last_app_resend: datetime | None = None

    # MARK: The battery-state sensor

    def battery_state_entity(self) -> str | None:
        """The option, or the notify service's mobile_app device's battery state sensor."""
        if picked := self.bridge.options.get(CONF_PHONE_BATTERY_STATE):
            return picked
        notify = (self.bridge.options.get(CONF_NOTIFY) or "").strip()
        _, _, service = notify.partition(".")
        if not service.startswith("mobile_app_"):
            return None
        phone = service.removeprefix("mobile_app_")
        guess = f"sensor.{phone}_battery_state"
        if self.hass.states.get(guess):
            return guess
        # Renamed entities: the device whose name gives the notify service, then its sensor.
        try:
            entities = er.async_get(self.hass)
            for device in dr.async_get(self.hass).devices.values():
                if not any(domain == "mobile_app" for domain, _ in device.identifiers):
                    continue
                if slugify(device.name_by_user or device.name or "") != phone and slugify(device.name or "") != phone:
                    continue
                for entry in er.async_entries_for_device(entities, device.id):
                    if entry.domain == "sensor" and str(entry.unique_id).endswith("battery_state"):
                        return entry.entity_id
        except Exception:  # noqa: BLE001 - a guess only
            return None
        return None

    def low_power(self) -> bool | None:
        """True in Low Power Mode, False out of it, None when not known."""
        entity_id = self.battery_state_entity()
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE) or LOW_POWER_ATTR not in state.attributes:
            return None
        return _on(state.attributes.get(LOW_POWER_ATTR))

    @callback
    def async_start(self) -> None:
        entity_id = self.battery_state_entity()
        if entity_id is None:
            # At startup the companion app's sensor may not be loaded yet: watch its usual id.
            _, _, service = (self.bridge.options.get(CONF_NOTIFY) or "").strip().partition(".")
            if service.startswith("mobile_app_"):
                entity_id = f"sensor.{service.removeprefix('mobile_app_')}_battery_state"
        if entity_id:
            self.bridge.entry.async_on_unload(
                async_track_state_change_event(self.hass, [entity_id], self._battery_changed))

    @callback
    def _battery_changed(self, event: Event[EventStateChangedData]) -> None:
        """Leaving Low Power Mode or starting to charge: the phone is reachable."""
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if new is None or old is None:
            return
        left_low_power = _on(old.attributes.get(LOW_POWER_ATTR)) and not _on(new.attributes.get(LOW_POWER_ATTR))
        started_charging = new.state.lower() in CHARGING and old.state.lower() not in CHARGING
        if left_low_power or started_charging:
            self.hass.async_create_task(self.resend("battery"))

    # MARK: Owing and resending

    def owed(self) -> list[dict[str, Any]]:
        """The owed clears, without those older than OWED_FOR, and without a Backstops tag owed
        from before this morning's first alarm (item 81: the backstop may have sent it again)."""
        now = dt_util.utcnow()
        first = dt_util.parse_datetime(str(self.bridge.state.get("series_start") or ""))
        backstop_tags = set(clear_tags(self.bridge.options.get(CONF_CLEAR_TAGS)))
        kept = []
        for item in self.bridge.state.get("owed_clears") or []:
            at = dt_util.parse_datetime(str(item.get("at") or ""))
            if at is None or now - at >= OWED_FOR or not item.get("tag"):
                continue
            if first is not None and item["tag"] in backstop_tags and at < first <= now:
                continue
            kept.append(item)
        if kept != (self.bridge.state.get("owed_clears") or []):
            self.bridge.state["owed_clears"] = kept
            self.bridge._save()
        return kept

    @callback
    def drop(self, tags: Any) -> None:
        """v0.5.4 item 81: these tags were (or may have been) sent again, so their owed clears
        would remove the newer notification: no longer owed."""
        tags = {tags} if isinstance(tags, str) else set(tags)
        owed = self.bridge.state.get("owed_clears") or []
        kept = [i for i in owed if i.get("tag") not in tags]
        if len(kept) != len(owed):
            self.bridge.state["owed_clears"] = kept
            self.bridge._save()

    @callback
    def drop_backstop_tags(self) -> None:
        """The backstops armed for a new morning, or one fired: the Backstops tags (yours and
        wake_backstop) may be sent again by automations we don't see."""
        self.drop(clear_tags(self.bridge.options.get(CONF_CLEAR_TAGS)))

    async def _call(self, notify: str, tag: str) -> bool:
        domain, _, service = notify.partition(".")
        try:
            await self.hass.services.async_call(
                domain, service, {"message": "clear_notification", "data": {"tag": tag}}, blocking=False)
        except HomeAssistantError as err:
            _LOGGER.warning("Bedtime Bridge couldn't clear %s: %s", tag, err)
            return False
        return True

    async def send(self, tag: str, notify: str | None = None) -> bool:
        """Sends a clear and owes it unless the phone is known to be out of Low Power Mode
        (then this is a reachable moment, and every owed clear goes with it). Not known (no
        battery-state sensor, item 90): owed all the same."""
        notify = (notify or self.bridge.options.get(CONF_NOTIFY) or "").strip()
        if not notify:
            return False
        sent = await self._call(notify, tag)
        low_power = self.low_power()
        if low_power is False:
            await self.resend("reachable")
            return sent
        owed = [i for i in self.owed() if not (i["tag"] == tag and i.get("notify") == notify)]
        owed.append({"tag": tag, "notify": notify, "at": dt_util.utcnow().isoformat()})
        self.bridge.state["owed_clears"] = owed
        self.bridge._save()
        return sent

    async def resend(self, why: str) -> None:
        """All owed clears again, together (iOS keeps only the latest held one). Out of Low
        Power Mode they're done; still in it, or not known (item 90), they stay owed until they
        expire or are sent while it's known to be out of it."""
        owed = self.owed()
        if not owed:
            return
        if why == "app":
            now = dt_util.utcnow()
            if self._last_app_resend and now - self._last_app_resend < APP_RESEND_GAP:
                return
            self._last_app_resend = now
        _LOGGER.debug("Bedtime Bridge: sending %s owed clears again (%s)", len(owed), why)
        for item in owed:
            await self._call(item.get("notify") or "", item["tag"])
        if self.low_power() is False:
            self.bridge.state["owed_clears"] = []
            self.bridge._save()
