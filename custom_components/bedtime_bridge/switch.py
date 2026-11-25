"""Helpers Bedtime Bridge makes itself (v0.5.4 item 35): Sleep In, Awake For Day, Napping,
Winding Down, Bedtime, Snooze, Stop, Early wake-up and the exit latch, each only when its option
says "Bedtime Bridge makes it". They keep their state across restarts, like an input_boolean."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import BedtimeBridgeConfigEntry
from .const import DOMAIN, MADE_SWITCHES, TRIGGER_SWITCHES
from .entity import BedtimeBridgeEntity
from .helpers import made_options, made_unique_id

ICONS = {
    "sleep_in": "mdi:bed-clock",
    "awake_for_day": "mdi:white-balance-sunny",
    "napping": "mdi:sleep",
    "winding_down": "mdi:weather-night",
    "bedtime": "mdi:bed",
    "snooze": "mdi:alarm-snooze",
    "stop": "mdi:alarm-off",
    "early_wake_up": "mdi:alarm-check",
    "exit_latch": "mdi:door-open",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: BedtimeBridgeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    bridge = entry.runtime_data
    wanted = [option for option in made_options(bridge.raw_options) if option in MADE_SWITCHES]
    # A helper switched to "Use mine" takes its old switch with it.
    registry = er.async_get(hass)
    for option in MADE_SWITCHES:
        if option not in wanted and (
            entity_id := registry.async_get_entity_id("switch", DOMAIN, made_unique_id(entry, option))
        ):
            registry.async_remove(entity_id)
    async_add_entities(MadeSwitch(bridge, option) for option in wanted)


class MadeSwitch(BedtimeBridgeEntity, SwitchEntity, RestoreEntity):
    """On or off like a helper; Bedtime Bridge and your automations turn it on and off."""

    def __init__(self, bridge, option: str) -> None:
        key = MADE_SWITCHES[option]
        super().__init__(bridge, f"helper_{key}")
        self._key = key
        self._attr_translation_key = key
        self._attr_icon = ICONS[key]
        self._attr_is_on = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._attr_is_on and self._key in TRIGGER_SWITCHES:
            # Already on: off and on again, so automations that start on "on" run again.
            self._attr_is_on = False
            self.async_write_ha_state()
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
