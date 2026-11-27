"""Config flow: choose what Snooze/Stop do, which entities to read, then pair the phone."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components import webhook
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .pairing import async_placeholders

from .const import shift_types as parse_shift_types
from .const import (
    CONF_AWAKE_HELPER,
    CONF_APNS_KEY,
    CONF_APNS_KEY_ID,
    CONF_BEDROOM_DOOR,
    CONF_DRIVE_SOURCE,
    CONF_GOOGLE_KEY,
    CONF_GOOGLE_BUDGET,
    DEFAULT_GOOGLE_BUDGET,
    CONF_HALLWAY_SENSORS,
    CONF_PARTNER_BED,
    CONF_MAPS_KEY,
    CONF_MAPS_KEY_ID,
    CONF_TOMTOM_KEY,
    CONF_PHONE_BATTERY,
    CONF_PHONE_BATTERY_STATE,
    CONF_WIND_DOWN_HELPER,
    CONF_WIND_DOWN_NOTIFY,
    CONF_NAP_HELPER,
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
    CONF_CLEAR_TAGS,
    CONF_LADDER_SHIFT_TYPES,
    CONF_SHIFT_OVERRIDE,
    CONF_SHIFT_SENSOR,
    CONF_SHIFT_TYPES,
    CONF_SLEEP_IN,
    CONF_SNOOZE_HELPER,
    CONF_STOP_HELPER,
    CONF_WAKE_ACTION,
    CONF_WEATHER,
    CONF_WEBHOOK_ID,
    DEFAULT_CURTAIN_OFFSET,
    DEFAULT_REARM_MINUTES,
    DEFAULT_RERUN_MINUTES,
    DEFAULT_COMPLETE_MINUTES,
    HOUSE_BRIDGE,
    HOUSE_OLD,
    DEFAULT_LADDER_SHIFT_TYPES,
    ladder_shift_types,
    DEFAULT_SHIFT_TYPES,
    DOMAIN,
    SUGGESTED,
    SUGGESTED_HALLWAY,
    SUGGESTED_HOME_CHECKS,
    WAKE_ACTIONS,
    WAKE_BOTH,
    CONF_AWAKE_ON_MEANS,
    MADE_HELPERS,
    ON_MEANS_ASLEEP,
    ON_MEANS_AWAKE,
    SOURCE_MADE,
    SOURCES,
)
from .helpers import source_key

HELPER_DOMAINS = ["input_boolean", "script", "automation", "switch", "scene"]
# v0.5.4 item 35: a helper that's read (and changed, unless it's a binary sensor).
STATE_DOMAINS = ["input_boolean", "switch", "binary_sensor"]
SOURCE_KEYS = [source_key(option) for option in MADE_HELPERS]
OPTION_KEYS = [
    CONF_PRESENCE,
    CONF_HOME_ONLY,
    CONF_BEDTIME_HELPER,
    CONF_WAKE_ACTION,
    CONF_SNOOZE_HELPER,
    CONF_STOP_HELPER,
    CONF_EARLY_HELPER,
    CONF_SHIFT_OVERRIDE,
    CONF_SHIFT_SENSOR,
    CONF_WEATHER,
    CONF_SHIFT_TYPES,
    CONF_LADDER_SHIFT_TYPES,
    CONF_CALENDAR,
    CONF_RESTART_AUTOMATIONS,
    CONF_CLEAR_TAGS,
    CONF_HOUSE_BACKSTOP,
    CONF_OLD_RUNGS,
    CONF_NOTIFY,
    CONF_REPLAN_ALERT,
    CONF_BED_SENSOR,
    CONF_EXIT_LATCH,
    CONF_PHONE_COMMANDS,
    CONF_COMPLETE_MINUTES,
    CONF_REARM_MINUTES,
    CONF_SLEEP_IN,
    CONF_CURTAIN_OFFSET,
    CONF_HOME_CHECKS,
    CONF_AWAKE_HELPER,
    CONF_RERUN_MINUTES,
    CONF_NAP_HELPER,
    CONF_WIND_DOWN_HELPER,
    CONF_BEDROOM_DOOR,
    CONF_WIND_DOWN_NOTIFY,
    CONF_PHONE_BATTERY,
    CONF_PHONE_BATTERY_STATE,
    CONF_APNS_KEY,
    CONF_APNS_KEY_ID,
    CONF_DRIVE_SOURCE,
    CONF_MAPS_KEY,
    CONF_MAPS_KEY_ID,
    CONF_TOMTOM_KEY,
    CONF_GOOGLE_KEY,
    CONF_GOOGLE_BUDGET,
    CONF_HALLWAY_SENSORS,
    *SOURCE_KEYS,
    CONF_AWAKE_ON_MEANS,
    CONF_PARTNER_BED,
]


def _suggested_phone(hass: HomeAssistant) -> str | None:
    """The iPhone's mobile_app notify service, ahead of any Mac or iPad on the account
    (mobile_app_alexs_macbook_pro sorts before mobile_app_alexs_iphone)."""
    services = sorted(s for s in hass.services.async_services_for_domain("notify") if s.startswith("mobile_app_"))
    for hint in ("iphone", "phone"):
        if match := next((s for s in services if hint in s), None):
            return match
    return services[0] if services else None


def _suggested_battery(hass: HomeAssistant, kind: str) -> str | None:
    """The phone's companion-app battery sensor (sensor.<phone>_battery_level / _state), from
    its notify action, when it exists (v0.5.4 item 35: no one home's entity ids)."""
    phone = _suggested_phone(hass)
    entity_id = f"sensor.{phone.removeprefix('mobile_app_')}_battery_{kind}" if phone else None
    return entity_id if entity_id and hass.states.get(entity_id) else None


# Configure is a menu of these pages, each its own small form (v0.4.7 item 9). Every option is on
# exactly one page.
def _with_source(*options: str) -> list[str]:
    """Each helper option with its "Bedtime Bridge makes it / Use mine" choice first."""
    return [key for option in options for key in ((source_key(option), option) if option in MADE_HELPERS else (option,))]


PAGES: dict[str, list[str]] = {
    "you": [CONF_PRESENCE, CONF_HOME_ONLY, CONF_HOME_CHECKS],
    "wake": [CONF_WAKE_ACTION, *_with_source(CONF_SNOOZE_HELPER, CONF_STOP_HELPER, CONF_EARLY_HELPER, CONF_SLEEP_IN,
                                             CONF_AWAKE_HELPER), CONF_AWAKE_ON_MEANS],
    "bedroom": [CONF_BED_SENSOR, CONF_PARTNER_BED, CONF_BEDROOM_DOOR, CONF_HALLWAY_SENSORS, *_with_source(CONF_EXIT_LATCH),
                CONF_REARM_MINUTES, CONF_RERUN_MINUTES],
    "bedtime": [*_with_source(CONF_BEDTIME_HELPER, CONF_WIND_DOWN_HELPER, CONF_NAP_HELPER)],
    "shifts": [*_with_source(CONF_SHIFT_OVERRIDE), CONF_SHIFT_SENSOR, CONF_SHIFT_TYPES, CONF_CALENDAR, CONF_WEATHER,
               CONF_CURTAIN_OFFSET],
    # v0.5.5 item 18: the phone notifications to clear and wind-down's other device are phone things.
    "phone": [CONF_NOTIFY, CONF_PHONE_COMMANDS, CONF_REPLAN_ALERT, CONF_PHONE_BATTERY, CONF_PHONE_BATTERY_STATE, CONF_COMPLETE_MINUTES,
              CONF_CLEAR_TAGS, CONF_WIND_DOWN_NOTIFY],
    "backstops": [CONF_HOUSE_BACKSTOP, CONF_OLD_RUNGS, CONF_RESTART_AUTOMATIONS, CONF_LADDER_SHIFT_TYPES],
    "keys": [CONF_APNS_KEY, CONF_APNS_KEY_ID, CONF_DRIVE_SOURCE, CONF_MAPS_KEY, CONF_MAPS_KEY_ID, CONF_TOMTOM_KEY, CONF_GOOGLE_KEY,
             CONF_GOOGLE_BUDGET],
}
# Adding the integration asks only these; the rest start from their defaults and suggestions.
SETUP_KEYS = [CONF_PRESENCE, CONF_HOME_ONLY, CONF_NOTIFY, CONF_PHONE_COMMANDS, CONF_WAKE_ACTION, CONF_SHIFT_TYPES]


def _subset(schema: vol.Schema, keys: list[str]) -> vol.Schema:
    return vol.Schema({marker: value for marker, value in schema.schema.items() if marker.schema in keys})


def _starting_options(hass: HomeAssistant, schema: vol.Schema) -> dict[str, Any]:
    """Every option's default, and its suggestion where the entity exists, as the full form
    would have saved them."""
    options: dict[str, Any] = {}
    for marker in schema.schema:
        default = marker.default() if callable(getattr(marker, "default", None)) and marker.default is not vol.UNDEFINED else None
        suggestion = (marker.description or {}).get("suggested_value") if isinstance(marker.description, dict) else None
        value = suggestion if suggestion not in (None, "", []) else default
        if value is not None:
            options[marker.schema] = value
    return options


def _with_cleared_keys(user_input: dict[str, Any]) -> dict[str, Any]:
    """Store a cleared optional field as None, so it stays cleared. Options the entry has never
    stored (e.g. added by an update) are absent, and Configure suggests a value for them."""
    return {**{key: None for key in OPTION_KEYS}, **user_input}


def _schema(hass: HomeAssistant, current: dict[str, Any]) -> vol.Schema:
    """Same fields for setup and options. Suggests your existing entities when they exist."""

    def suggested(key: str) -> dict[str, Any]:
        if key in current:
            return {"suggested_value": current[key]} if current[key] else {}
        if key in (CONF_PHONE_BATTERY, CONF_PHONE_BATTERY_STATE):
            battery = _suggested_battery(hass, "level" if key == CONF_PHONE_BATTERY else "state")
            return {"suggested_value": battery} if battery else {}
        entity_id = SUGGESTED.get(key)
        return {"suggested_value": entity_id} if entity_id and hass.states.get(entity_id) else {}

    def entity(domains: list[str]) -> selector.EntitySelector:
        return selector.EntitySelector(selector.EntitySelectorConfig(domain=domains))

    def made_or_mine(option: str) -> tuple[vol.Required, selector.SelectSelector]:
        """v0.5.4 item 35: Bedtime Bridge makes it (the default for a new install) or Use mine."""
        key = source_key(option)
        return vol.Required(key, default=current.get(key) or SOURCE_MADE), selector.SelectSelector(
            selector.SelectSelectorConfig(options=SOURCES, translation_key="helper_source")
        )

    def helper(option: str, domains: list[str]) -> dict[Any, Any]:
        source_marker, source_selector = made_or_mine(option)
        return {source_marker: source_selector, vol.Optional(option, description=suggested(option)): entity(domains)}

    def presence_suggestion() -> dict[str, Any]:
        if CONF_PRESENCE in current:
            return {"suggested_value": current[CONF_PRESENCE]} if current[CONF_PRESENCE] else {}
        people = hass.states.async_entity_ids("person")
        return {"suggested_value": people[:1]} if people else {}

    home_only = current.get(CONF_HOME_ONLY)
    replan_alert = current.get(CONF_REPLAN_ALERT)
    phone_commands = current.get(CONF_PHONE_COMMANDS)

    def home_checks_suggestion() -> dict[str, Any]:
        if CONF_HOME_CHECKS in current:
            return {"suggested_value": current[CONF_HOME_CHECKS]} if current[CONF_HOME_CHECKS] else {}
        found = [e for e in SUGGESTED_HOME_CHECKS if hass.states.get(e)]
        return {"suggested_value": found} if found else {}
    def hallway_suggestion() -> dict[str, Any]:
        if CONF_HALLWAY_SENSORS in current:
            return {"suggested_value": current[CONF_HALLWAY_SENSORS]} if current[CONF_HALLWAY_SENSORS] else {}
        found = [e for e in SUGGESTED_HALLWAY if hass.states.get(e)]
        return {"suggested_value": found} if found else {}
    shift_names = parse_shift_types(current.get(CONF_SHIFT_TYPES) or DEFAULT_SHIFT_TYPES)

    def restart_suggestion() -> dict[str, Any]:
        # v0.5.4 item 62: only what you picked; nothing suggested by name.
        if current.get(CONF_RESTART_AUTOMATIONS):
            return {"suggested_value": current[CONF_RESTART_AUTOMATIONS]}
        return {}

    def notify_suggestion() -> dict[str, Any]:
        if CONF_NOTIFY in current:
            return {"suggested_value": current[CONF_NOTIFY]} if current[CONF_NOTIFY] else {}
        phone = _suggested_phone(hass)
        return {"suggested_value": f"notify.{phone}"} if phone else {}
    return vol.Schema(
        {
            vol.Optional(CONF_PRESENCE, description=presence_suggestion()): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["person", "device_tracker", "binary_sensor"], multiple=True)
            ),
            vol.Required(CONF_HOME_ONLY, default=True if home_only is None else home_only): selector.BooleanSelector(),
            **helper(CONF_BEDTIME_HELPER, HELPER_DOMAINS),
            vol.Required(CONF_WAKE_ACTION, default=current.get(CONF_WAKE_ACTION) or WAKE_BOTH): selector.SelectSelector(
                selector.SelectSelectorConfig(options=WAKE_ACTIONS, translation_key=CONF_WAKE_ACTION)
            ),
            **helper(CONF_SNOOZE_HELPER, HELPER_DOMAINS),
            **helper(CONF_STOP_HELPER, HELPER_DOMAINS),
            **helper(CONF_EARLY_HELPER, HELPER_DOMAINS),
            **helper(CONF_SHIFT_OVERRIDE, ["input_select", "select"]),
            vol.Optional(CONF_SHIFT_SENSOR, description=suggested(CONF_SHIFT_SENSOR)): entity(["sensor"]),
            vol.Optional(CONF_WEATHER, description=suggested(CONF_WEATHER)): entity(["weather"]),
            vol.Required(CONF_SHIFT_TYPES, default=current.get(CONF_SHIFT_TYPES) or DEFAULT_SHIFT_TYPES): selector.TextSelector(),
            vol.Optional(
                CONF_LADDER_SHIFT_TYPES, default=ladder_shift_types(current)
            ): selector.SelectSelector(selector.SelectSelectorConfig(options=shift_names, multiple=True, custom_value=True)),
            vol.Optional(CONF_CALENDAR, description=suggested(CONF_CALENDAR)): entity(["calendar"]),
            vol.Optional(CONF_RESTART_AUTOMATIONS, description=restart_suggestion()): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["automation"], multiple=True)
            ),
            # A new install (nothing stored yet) starts on the Bedtime Bridge ladder; an existing
            # one keeps the old template until it's switched (v0.5.4 item 35).
            vol.Required(
                CONF_HOUSE_BACKSTOP, default=current.get(CONF_HOUSE_BACKSTOP) or (HOUSE_OLD if current else HOUSE_BRIDGE)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[HOUSE_OLD, HOUSE_BRIDGE], translation_key=CONF_HOUSE_BACKSTOP)
            ),
            vol.Optional(CONF_OLD_RUNGS, default=current.get(CONF_OLD_RUNGS) or []): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor"], multiple=True)
            ),
            vol.Optional(CONF_NOTIFY, description=notify_suggestion()): selector.TextSelector(),
            vol.Optional(CONF_BED_SENSOR, description=suggested(CONF_BED_SENSOR)): entity(["binary_sensor"]),
            **helper(CONF_EXIT_LATCH, STATE_DOMAINS),
            vol.Required(CONF_REPLAN_ALERT, default=True if replan_alert is None else replan_alert): selector.BooleanSelector(),
            vol.Required(CONF_PHONE_COMMANDS, default=bool(phone_commands)): selector.BooleanSelector(),
            vol.Required(
                CONF_COMPLETE_MINUTES, default=current.get(CONF_COMPLETE_MINUTES) or DEFAULT_COMPLETE_MINUTES
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=30, step=1, unit_of_measurement="min")),
            vol.Optional(CONF_CLEAR_TAGS, description={"suggested_value": current.get(CONF_CLEAR_TAGS, "")}): selector.TextSelector(),
            vol.Optional(CONF_WIND_DOWN_NOTIFY, description=suggested(CONF_WIND_DOWN_NOTIFY)): selector.TextSelector(),
            vol.Required(
                CONF_REARM_MINUTES, default=current.get(CONF_REARM_MINUTES) or DEFAULT_REARM_MINUTES
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=30, step=1, unit_of_measurement="min")),
            **helper(CONF_SLEEP_IN, STATE_DOMAINS),
            **helper(CONF_AWAKE_HELPER, STATE_DOMAINS),
            vol.Required(CONF_AWAKE_ON_MEANS, default=current.get(CONF_AWAKE_ON_MEANS) or ON_MEANS_AWAKE): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[ON_MEANS_AWAKE, ON_MEANS_ASLEEP], translation_key="on_means")
            ),
            **helper(CONF_NAP_HELPER, STATE_DOMAINS),
            **helper(CONF_WIND_DOWN_HELPER, STATE_DOMAINS),
            vol.Optional(CONF_BEDROOM_DOOR, description=suggested(CONF_BEDROOM_DOOR)): entity(["binary_sensor"]),
            # Open from v0.4.6 item 2: no suggestion; only you know whose side is whose.
            vol.Optional(CONF_PARTNER_BED, description=suggested(CONF_PARTNER_BED)): entity(["binary_sensor"]),
            vol.Optional(CONF_HALLWAY_SENSORS, description=hallway_suggestion()): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["binary_sensor"], multiple=True)
            ),
            vol.Optional(CONF_PHONE_BATTERY, description=suggested(CONF_PHONE_BATTERY)): entity(["sensor"]),
            vol.Optional(CONF_PHONE_BATTERY_STATE, description=suggested(CONF_PHONE_BATTERY_STATE)): entity(["sensor"]),
            vol.Optional(CONF_APNS_KEY, description=suggested(CONF_APNS_KEY)): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True, type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_APNS_KEY_ID, description=suggested(CONF_APNS_KEY_ID)): selector.TextSelector(),
            vol.Required(CONF_DRIVE_SOURCE, default=current.get(CONF_DRIVE_SOURCE) or "phone"): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["phone", "apple", "tomtom", "google", "longest"], translation_key=CONF_DRIVE_SOURCE)
            ),
            vol.Optional(CONF_MAPS_KEY, description=suggested(CONF_MAPS_KEY)): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True, type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_MAPS_KEY_ID, description=suggested(CONF_MAPS_KEY_ID)): selector.TextSelector(),
            vol.Optional(CONF_TOMTOM_KEY, description=suggested(CONF_TOMTOM_KEY)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_GOOGLE_KEY, description=suggested(CONF_GOOGLE_KEY)): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Required(
                CONF_GOOGLE_BUDGET, default=current.get(CONF_GOOGLE_BUDGET) or DEFAULT_GOOGLE_BUDGET
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=10, max=1000, step=10, mode=selector.NumberSelectorMode.BOX)),
            vol.Required(
                CONF_RERUN_MINUTES, default=current.get(CONF_RERUN_MINUTES) or DEFAULT_RERUN_MINUTES
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=1, max=60, step=1, unit_of_measurement="min")),
            vol.Optional(CONF_HOME_CHECKS, description=home_checks_suggestion()): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["cover", "lock", "binary_sensor"], multiple=True)
            ),
            vol.Required(
                CONF_CURTAIN_OFFSET, default=current.get(CONF_CURTAIN_OFFSET) or DEFAULT_CURTAIN_OFFSET
            ): selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=120, step=5, unit_of_measurement="min")),
        }
    )


PAGE_NAMES = {
    "you": "You and home", "wake": "Wake-up", "bedroom": "Bedroom and bed", "bedtime": "Bedtime and wind-down",
    "shifts": "Shifts and calendar", "phone": "Phone", "backstops": "Backstops", "keys": "Keys",
}


def changed_text(pages: list[str]) -> str:
    """The menu's line about unsaved pages (v0.4.8 item 10)."""
    if not pages:
        return "Pick a page. Each page comes back here; **Save and close** saves your changes."
    names = ", ".join(PAGE_NAMES.get(page, page) for page in pages)
    return f"**Changed: {names}.** Not saved until you choose **Save and close**; closing with ✕ drops them."



def notify_errors(hass: HomeAssistant, user_input: dict[str, Any], current: dict[str, Any] | None = None) -> dict[str, str]:
    """v0.6.0 item 36: each notify field filled in names a notify service Home Assistant has
    in full ("notify.mobile_app_your_iphone"), as it's used. One already saved isn't checked
    again (the Mobile App integration may still be starting)."""
    errors: dict[str, str] = {}
    for key in (CONF_NOTIFY, CONF_WIND_DOWN_NOTIFY):
        value = str(user_input.get(key) or "").strip()
        if not value or value == str((current or {}).get(key) or "").strip():
            continue
        domain, _, service = value.partition(".")
        if not service or not hass.services.has_service(domain, service):
            errors[key] = "no_notify_service"
    return errors

class BedtimeBridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    # 1.2 (v0.5.4 item 35): each helper's "Bedtime Bridge makes it / Use mine" choice.
    MINOR_VERSION = 3

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._webhook_id = ""

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        full = _schema(self.hass, {})
        if user_input is not None and (errors := notify_errors(self.hass, user_input)):
            return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(_subset(full, SETUP_KEYS), user_input),
                                        errors=errors)
        if user_input is not None:
            self._options = {**_starting_options(self.hass, full), **{key: None for key in SETUP_KEYS}, **user_input}
            # v0.5.4 item 57: the ladder covers every shift type entered (an existing install
            # keeps what it saved).
            if names := parse_shift_types(self._options.get(CONF_SHIFT_TYPES) or DEFAULT_SHIFT_TYPES):
                self._options[CONF_LADDER_SHIFT_TYPES] = names
            self._webhook_id = webhook.async_generate_id()
            return await self.async_step_pair()
        return self.async_show_form(step_id="user", data_schema=_subset(full, SETUP_KEYS))

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Shows the address to paste into the app before the entry exists."""
        if user_input is not None:
            return self.async_create_entry(
                title="Bedtime Bridge", data={CONF_WEBHOOK_ID: self._webhook_id}, options=self._options
            )
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({}),
            description_placeholders=await async_placeholders(self.hass, self._webhook_id),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BedtimeBridgeOptionsFlow()


class BedtimeBridgeOptionsFlow(OptionsFlow):
    """A menu of pages (v0.4.7 item 9). A page goes back to the menu with its changes kept;
    Save and close saves every changed page at once (v0.4.8 item 10)."""

    def __init__(self) -> None:
        self._changes: dict[str, Any] = {}
        self._changed_pages: list[str] = []

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        placeholders = await async_placeholders(self.hass, self.config_entry.data[CONF_WEBHOOK_ID])
        placeholders["changed"] = changed_text(self._changed_pages)
        return self.async_show_menu(
            step_id="init",
            menu_options=[*PAGES, "save"],
            description_placeholders=placeholders,
        )

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(data={**self.config_entry.options, **self._changes})

    async def _page(self, page: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        current = {**self.config_entry.options, **self._changes}
        if user_input is not None and (errors := notify_errors(self.hass, user_input, current)):
            # v0.6.0 item 36: a notify service that doesn't exist isn't saved.
            schema = _subset(_schema(self.hass, current), PAGES[page])
            return self.async_show_form(step_id=page, data_schema=self.add_suggested_values_to_schema(schema, user_input), errors=errors)
        if user_input is not None:
            # A field cleared on this page stays cleared; other pages keep theirs.
            new = {**{key: None for key in PAGES[page]}, **user_input}
            # An option the entry never stored, left at its default, isn't a change.
            try:
                defaults = _subset(_schema(self.hass, current), PAGES[page])({})
            except vol.Invalid:
                defaults = {}
            changed = any(current.get(key, defaults.get(key)) != value for key, value in new.items())
            if changed and page not in self._changed_pages:
                self._changed_pages.append(page)
            self._changes.update(new)
            return await self.async_step_init()
        # Only what the entry has stored: cleared fields (None) stay empty, options it has never
        # had get a suggestion.
        return self.async_show_form(step_id=page, data_schema=_subset(_schema(self.hass, current), PAGES[page]))

    async def async_step_you(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._page("you", user_input)

    async def async_step_wake(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._page("wake", user_input)

    async def async_step_bedroom(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._page("bedroom", user_input)

    async def async_step_bedtime(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._page("bedtime", user_input)

    async def async_step_shifts(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._page("shifts", user_input)

    async def async_step_phone(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._page("phone", user_input)

    async def async_step_backstops(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._page("backstops", user_input)

    async def async_step_keys(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._page("keys", user_input)
