"""Helpers Bedtime Bridge makes itself, or yours (v0.5.4 item 35).

Each helper option (Sleep In, Awake For Day, Napping, Winding Down, Bedtime, Snooze, Stop, Early
wake-up, exit latch, shift override) has a source: "made" uses the integration's own switch (or
select, for the shift override); "mine" uses the entity picked in the option, as before. The rest
of the integration reads the resolved entity id from bridge.options, so it doesn't care which.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_AWAKE_HELPER,
    CONF_AWAKE_ON_MEANS,
    DOMAIN,
    MADE_HELPERS,
    MADE_SELECTS,
    ON_MEANS_ASLEEP,
    SOURCE_MADE,
    SOURCE_MINE,
    SOURCE_SUFFIX,
)

# Entities that are only ever read: a binary sensor can't be turned on or off.
READ_ONLY_DOMAINS = ("binary_sensor",)


def source_key(option: str) -> str:
    return f"{option}{SOURCE_SUFFIX}"


def source(raw: dict[str, Any], option: str) -> str:
    """Made or mine. An entry with no source stored (before migration) counts as mine."""
    return SOURCE_MADE if raw.get(source_key(option)) == SOURCE_MADE else SOURCE_MINE


def made_unique_id(entry: ConfigEntry, option: str) -> str:
    return f"{entry.entry_id}_helper_{MADE_HELPERS[option]}"


def made_domain(option: str) -> str:
    return "select" if option in MADE_SELECTS else "switch"


def made_entity_id(hass: HomeAssistant, entry: ConfigEntry, option: str) -> str:
    """The made entity's id: from the entity registry (it may have been renamed), else the id
    it gets when first added (e.g. switch.bedtime_bridge_sleep_in)."""
    domain = made_domain(option)
    found = er.async_get(hass).async_get_entity_id(domain, DOMAIN, made_unique_id(entry, option))
    return found or f"{domain}.{DOMAIN}_{MADE_HELPERS[option]}"


def made_options(raw: dict[str, Any]) -> list[str]:
    """The helper options Bedtime Bridge makes for this entry."""
    return [option for option in MADE_HELPERS if source(raw, option) == SOURCE_MADE]


def resolve(hass: HomeAssistant, entry: ConfigEntry, raw: dict[str, Any]) -> dict[str, Any]:
    """The options with each made helper's option set to the made entity's id."""
    resolved = dict(raw)
    for option in made_options(raw):
        resolved[option] = made_entity_id(hass, entry, option)
    return resolved


def inverted(raw: dict[str, Any], option: str) -> bool:
    """Awake For Day mirrored from your own "Asleep for the night" helper: on means asleep."""
    return (
        option == CONF_AWAKE_HELPER
        and source(raw, option) == SOURCE_MINE
        and raw.get(CONF_AWAKE_ON_MEANS) == ON_MEANS_ASLEEP
    )


def writable(entity_id: str | None) -> bool:
    """A binary sensor is only read, never changed."""
    return bool(entity_id) and entity_id.partition(".")[0] not in READ_ONLY_DOMAINS
