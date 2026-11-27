"""Bedtime Bridge: the iPhone wake-up planner's companion in Home Assistant."""

from __future__ import annotations

import json
import logging

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .bridge import BedtimeBridge
from .pairing import async_delete_cloudhook
from .const import CONF_NOTIFY, CONF_WEBHOOK_ID, ISSUE_APP_OUTDATED, ISSUE_INTEGRATION_OUTDATED, CONF_WIND_DOWN_NOTIFY, DOMAIN, MADE_HELPERS, SOURCE_MINE, SOURCE_SUFFIX

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SWITCH, Platform.SELECT]

type BedtimeBridgeConfigEntry = ConfigEntry[BedtimeBridge]


async def async_migrate_entry(hass: HomeAssistant, entry: BedtimeBridgeConfigEntry) -> bool:
    """1.2 (v0.5.4 item 35): every helper an existing setup has is "Use mine", picked or not, so
    nothing is replaced and no switches appear. New installs start with Bedtime Bridge's own."""
    if entry.version > 1:
        return False
    if entry.minor_version < 2:
        options = dict(entry.options)
        for option in MADE_HELPERS:
            options.setdefault(f"{option}{SOURCE_SUFFIX}", SOURCE_MINE)
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        _LOGGER.debug("Bedtime Bridge options migrated to 1.2: helpers kept as picked")
    if entry.minor_version < 3:
        # 1.3 (v0.5.5 item 18): wind-down always goes to the Phone page's phone. A wind-down
        # target that is that same phone is cleared; a different device (a Mac, an iPad) stays.
        options = dict(entry.options)
        if same_notify(options.get(CONF_WIND_DOWN_NOTIFY), options.get(CONF_NOTIFY)):
            options[CONF_WIND_DOWN_NOTIFY] = None
        hass.config_entries.async_update_entry(entry, options=options, minor_version=3)
        _LOGGER.debug("Bedtime Bridge options migrated to 1.3: wind-down uses the phone")
    return True


def same_notify(wind_down: str | None, phone: str | None) -> bool:
    """Whether the wind-down target is just the phone's notify action again."""
    a, b = (wind_down or "").strip(), (phone or "").strip()
    return bool(a) and a.removeprefix("notify.") == b.removeprefix("notify.")


async def async_setup_entry(hass: HomeAssistant, entry: BedtimeBridgeConfigEntry) -> bool:
    bridge = BedtimeBridge(hass, entry)
    await bridge.async_load()
    entry.runtime_data = bridge
    bridge.async_start()

    async def handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        try:
            message = await request.json()
        except (ValueError, json.JSONDecodeError):
            return web.json_response({"error": "body must be JSON"}, status=400)
        if not isinstance(message, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)
        try:
            return web.json_response(await bridge.async_handle(message))
        except Exception:  # noqa: BLE001 - report to the phone instead of a bare 500
            _LOGGER.exception("Bedtime Bridge message failed: %s", message.get("type"))
            return web.json_response({"error": "Home Assistant could not handle the message"}, status=500)

    # local_only=False: the phone also reports snoozes and stops when it's away from home.
    webhook.async_register(
        hass, DOMAIN, "Bedtime Bridge", entry.data[CONF_WEBHOOK_ID], handle_webhook, local_only=False
    )
    entry.async_on_unload(lambda: webhook.async_unregister(hass, entry.data[CONF_WEBHOOK_ID]))
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BedtimeBridgeConfigEntry) -> bool:
    # v0.5.3 item 25: the state is written now, not left to a delayed save a reload would lose.
    bridge = getattr(entry, "runtime_data", None)
    if bridge is not None and hasattr(bridge, "async_flush"):
        await bridge.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: BedtimeBridgeConfigEntry) -> None:
    """Deleting the integration also deletes its Nabu Casa cloudhook, if it made one, and
    (v0.6.0 item 36) its repair notices."""
    await async_delete_cloudhook(hass, entry.data[CONF_WEBHOOK_ID])
    for issue in (ISSUE_APP_OUTDATED, ISSUE_INTEGRATION_OUTDATED):
        ir.async_delete_issue(hass, DOMAIN, issue)


async def _async_reload(hass: HomeAssistant, entry: BedtimeBridgeConfigEntry) -> None:
    """Options changed (e.g. new shift types add lead-time numbers): reload."""
    await hass.config_entries.async_reload(entry.entry_id)
