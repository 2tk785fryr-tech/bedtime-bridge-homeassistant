"""The shift override Bedtime Bridge makes itself (v0.5.4 item 35), when its option says
"Bedtime Bridge makes it": Auto (follow the calendar), Off (no shift tomorrow: skipping a shift
sets it, and the backstop ladder stands down), or one of your shift types."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import BedtimeBridgeConfigEntry
from .const import (
    CONF_SHIFT_OVERRIDE,
    CONF_SHIFT_TYPES,
    DOMAIN,
    SHIFT_OVERRIDE_AUTO,
    SHIFT_OVERRIDE_OFF,
    shift_types,
)
from .entity import BedtimeBridgeEntity
from .helpers import made_options, made_unique_id


async def async_setup_entry(
    hass: HomeAssistant, entry: BedtimeBridgeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    bridge = entry.runtime_data
    if CONF_SHIFT_OVERRIDE in made_options(bridge.raw_options):
        async_add_entities([ShiftOverride(bridge)])
        return
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id("select", DOMAIN, made_unique_id(entry, CONF_SHIFT_OVERRIDE)):
        registry.async_remove(entity_id)


class ShiftOverride(BedtimeBridgeEntity, SelectEntity, RestoreEntity):
    _attr_translation_key = "shift_override"
    _attr_icon = "mdi:calendar-edit"

    def __init__(self, bridge) -> None:
        super().__init__(bridge, "helper_shift_override")
        shifts = [s for s in shift_types(bridge.raw_options.get(CONF_SHIFT_TYPES)) if s not in (SHIFT_OVERRIDE_AUTO, SHIFT_OVERRIDE_OFF)]
        self._attr_options = [SHIFT_OVERRIDE_AUTO, SHIFT_OVERRIDE_OFF, *shifts]
        self._attr_current_option = SHIFT_OVERRIDE_AUTO

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None and last.state in self._attr_options:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()
