"""Passive discovery and explicit configuration of the existing HA MQTT broker."""

from __future__ import annotations

import asyncio
import re
from copy import deepcopy
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components import mqtt
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.service_info.mqtt import MqttServiceInfo

from .const import (
    BROKER_UNIQUE_ID,
    CONF_ALLOW_EXISTING_CONTROLS,
    CONF_DISCOVERY_PREFIX,
    CONF_ENABLE_CONTROLS,
    CONF_GROUP_ID,
    CONF_GROUP_IDS,
    CONF_MAX_REPORT_AGE,
    CONF_METADATA_CHILDREN,
    CONF_PROTOCOL_PROFILE,
    CONF_QUERY_INTERVAL,
    CONF_QUERY_ON_START,
    CONF_ROOT_BINDINGS,
    DEFAULT_ALLOW_EXISTING_CONTROLS,
    DEFAULT_DISCOVERY_PREFIX,
    DEFAULT_ENABLE_CONTROLS,
    DEFAULT_MAX_REPORT_AGE,
    DEFAULT_PROTOCOL_PROFILE,
    DEFAULT_QUERY_INTERVAL,
    DEFAULT_QUERY_ON_START,
    DISCOVERY_SECONDS,
    DOMAIN,
    NAME,
)
from .discovery import (
    DiscoveryCollector,
    DiscoveryResult,
    async_scan,
    native_config_hint,
    normalize_group_id,  # noqa: F401 -- public compatibility export
    normalize_groups,
    normalize_prefix,
)
from .network import normalize_root_bindings

CONF_DISCOVERY_MODE = "discovery_mode"
CONF_PROFILE_CONFIRMED = "profile_confirmed"
CONF_CONFIGURE_NETWORK = "configure_network"
CONF_NETWORK_ACTION = "network_action"
CONF_NETWORK_GROUP = "group"
CONF_REMOVE_BINDINGS = "remove_bindings"

_MAC_INPUT = re.compile(
    r"(?:[0-9a-fA-F]{12}|(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}|"
    r"(?:[0-9a-fA-F]{2}-){5}[0-9a-fA-F]{2})"
)


def _settings_schema(defaults: dict[str, Any]) -> dict:
    schema = {
        vol.Required(
            CONF_MAX_REPORT_AGE,
            default=defaults.get(CONF_MAX_REPORT_AGE, DEFAULT_MAX_REPORT_AGE),
        ): selector.NumberSelector(
            {"min": 30, "max": 86400, "step": 1, "mode": "box", "unit_of_measurement": "s"}
        ),
        vol.Required(
            CONF_QUERY_INTERVAL,
            default=defaults.get(CONF_QUERY_INTERVAL, DEFAULT_QUERY_INTERVAL),
        ): selector.NumberSelector(
            {"min": 60, "max": 86400, "step": 1, "mode": "box", "unit_of_measurement": "s"}
        ),
        vol.Required(
            CONF_QUERY_ON_START,
            default=defaults.get(CONF_QUERY_ON_START, DEFAULT_QUERY_ON_START),
        ): bool,
        vol.Required(
            CONF_ENABLE_CONTROLS,
            default=defaults.get(CONF_ENABLE_CONTROLS, DEFAULT_ENABLE_CONTROLS),
        ): bool,
        vol.Required(
            CONF_ALLOW_EXISTING_CONTROLS,
            default=defaults.get(CONF_ALLOW_EXISTING_CONTROLS, DEFAULT_ALLOW_EXISTING_CONTROLS),
        ): bool,
        vol.Required(CONF_PROFILE_CONFIRMED, default=False): bool,
        vol.Required(
            CONF_METADATA_CHILDREN, default=defaults.get(CONF_METADATA_CHILDREN, False)
        ): bool,
        vol.Required(CONF_CONFIGURE_NETWORK, default=bool(defaults.get(CONF_ROOT_BINDINGS))): bool,
    }
    return schema


def _settings(values: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Validate even direct API callers before trusted entry data is created."""
    if values.get(CONF_PROFILE_CONFIRMED) is not True:
        raise ValueError("profile_not_confirmed")
    try:
        settings = vol.Schema(_settings_schema(defaults))(values)
        for key in (CONF_MAX_REPORT_AGE, CONF_QUERY_INTERVAL):
            if isinstance(values.get(key), bool) or not settings[key].is_integer():
                raise vol.Invalid("Expected whole seconds")
            settings[key] = int(settings[key])
    except vol.Invalid as err:
        raise ValueError("invalid_settings") from err
    settings.pop(CONF_PROFILE_CONFIRMED)
    settings[CONF_PROTOCOL_PROFILE] = DEFAULT_PROTOCOL_PROFILE
    return settings


class _NetworkForms:
    """Edit a private draft using ordinary HA forms, shared by setup and options."""

    _draft: dict[str, Any]
    _bindings: dict[str, dict[str, str]]
    _groups: list[str]
    _network_group: str
    _network_requested: bool

    async def _async_prepare_draft(self, settings, groups, prefix, defaults) -> FlowResult:
        self._network_requested = settings.pop(CONF_CONFIGURE_NETWORK)
        self._groups = groups
        self._bindings = deepcopy(defaults.get(CONF_ROOT_BINDINGS, {}))
        self._draft = (
            deepcopy(defaults)
            | settings
            | {
                CONF_GROUP_IDS: groups,
                CONF_DISCOVERY_PREFIX: prefix,
                CONF_ROOT_BINDINGS: self._bindings,
            }
        )
        if set(self._bindings) - set(groups):
            return await self.async_step_remove_bound_groups()
        return await self._async_after_binding_confirmation()

    async def _async_after_binding_confirmation(self) -> FlowResult:
        if self._network_requested or (self._draft[CONF_METADATA_CHILDREN] and not self._bindings):
            return await self.async_step_network()
        return await self._async_save_draft()

    async def _async_save_draft(self) -> FlowResult:
        try:
            self._draft[CONF_ROOT_BINDINGS] = normalize_root_bindings(self._bindings, self._groups)
            if self._draft[CONF_METADATA_CHILDREN] and not self._bindings:
                raise ValueError("network_required")
        except ValueError as err:
            return self._show_network({"base": str(err)})
        return await self._async_finish_entry()

    async def async_step_remove_bound_groups(self, user_input=None) -> FlowResult:
        """Removing a mesh must explicitly remove its existing network binding."""
        removed = sorted(set(self._bindings) - set(self._groups))
        errors = {}
        if user_input is not None:
            if user_input.get(CONF_REMOVE_BINDINGS) is not True:
                errors[CONF_REMOVE_BINDINGS] = "confirmation_required"
            else:
                for group in removed:
                    self._bindings.pop(group)
                return await self._async_after_binding_confirmation()
        return self.async_show_form(
            step_id="remove_bound_groups",
            data_schema=vol.Schema({vol.Required(CONF_REMOVE_BINDINGS, default=False): bool}),
            description_placeholders={"groups": ", ".join(removed)},
            errors=errors,
        )

    def _show_network(self, errors=None) -> FlowResult:
        return self.async_show_form(
            step_id="network",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NETWORK_ACTION, default="add_edit"): selector.SelectSelector(
                        {
                            "options": ["add_edit", "remove", "finish"],
                            "translation_key": "network_action",
                            "mode": "dropdown",
                        }
                    ),
                    vol.Required(
                        CONF_METADATA_CHILDREN, default=self._draft[CONF_METADATA_CHILDREN]
                    ): bool,
                }
            ),
            description_placeholders={"groups": ", ".join(self._bindings) or "—"},
            errors=errors or {},
        )

    async def async_step_network(self, user_input=None) -> FlowResult:
        if user_input is not None:
            action = user_input.get(CONF_NETWORK_ACTION)
            child_reads = user_input.get(
                CONF_METADATA_CHILDREN, self._draft[CONF_METADATA_CHILDREN]
            )
            if type(child_reads) is not bool:
                return self._show_network({"base": "invalid_settings"})
            self._draft[CONF_METADATA_CHILDREN] = child_reads
            if action == "add_edit":
                return await self.async_step_network_select()
            if action == "remove":
                if not self._bindings:
                    return self._show_network({"base": "no_root_bindings"})
                return await self.async_step_network_remove()
            if action == "finish":
                return await self._async_save_draft()
            return self._show_network({"base": "invalid_settings"})
        return self._show_network()

    async def async_step_network_select(self, user_input=None) -> FlowResult:
        errors = {}
        if user_input is not None:
            group = user_input.get(CONF_NETWORK_GROUP)
            if group not in self._groups:
                errors[CONF_NETWORK_GROUP] = "invalid_group_id"
            else:
                self._network_group = group
                return await self.async_step_network_binding()
        return self.async_show_form(
            step_id="network_select",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NETWORK_GROUP, default=self._groups[0]
                    ): selector.SelectSelector({"options": self._groups, "mode": "dropdown"})
                }
            ),
            errors=errors,
        )

    async def async_step_network_binding(self, user_input=None) -> FlowResult:
        errors = {}
        defaults = self._bindings.get(self._network_group, {})
        if user_input is not None:
            defaults = dict(user_input)
            try:
                if set(user_input) != {"host", "root_mac"}:
                    raise ValueError("invalid_root_bindings")
                host, root = user_input.get("host"), user_input.get("root_mac")
                if not isinstance(root, str) or not _MAC_INPUT.fullmatch(root.strip()):
                    errors["root_mac"] = "invalid_root_mac"
                elif not isinstance(host, str):
                    errors["host"] = "invalid_root_host"
                else:
                    binding = {
                        "host": host.strip(),
                        "root_mac": re.sub("[:-]", "", root.strip()).upper(),
                    }
                    candidate = self._bindings | {self._network_group: binding}
                    self._bindings = normalize_root_bindings(candidate, self._groups)
                    return await self.async_step_network()
            except ValueError:
                errors["base"] = "invalid_root_bindings"
        return self.async_show_form(
            step_id="network_binding",
            data_schema=vol.Schema(
                {
                    vol.Required("host", default=defaults.get("host", "")): str,
                    vol.Required("root_mac", default=defaults.get("root_mac", "")): str,
                }
            ),
            description_placeholders={"group": self._network_group},
            errors=errors,
        )

    async def async_step_network_remove(self, user_input=None) -> FlowResult:
        if not self._bindings:
            return self._show_network({"base": "no_root_bindings"})
        errors = {}
        if user_input is not None:
            group = user_input.get(CONF_NETWORK_GROUP)
            if group not in self._bindings:
                errors[CONF_NETWORK_GROUP] = "invalid_group_id"
            else:
                del self._bindings[group]
                return await self.async_step_network()
        return self.async_show_form(
            step_id="network_remove",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NETWORK_GROUP, default=next(iter(self._bindings))
                    ): selector.SelectSelector(
                        {"options": list(self._bindings), "mode": "dropdown"}
                    )
                }
            ),
            errors=errors,
        )


class LanbonConfigFlow(_NetworkForms, config_entries.ConfigFlow, domain=DOMAIN):
    """One entry for HA's configured MQTT broker, containing selected meshes."""

    VERSION = 2

    def __init__(self) -> None:
        self._prefix = DEFAULT_DISCOVERY_PREFIX
        self._groups: list[str] = []
        self._collector: DiscoveryCollector | None = None
        self._scan_task: asyncio.Task[DiscoveryResult] | None = None
        self._scan_result = DiscoveryResult(())
        self._mqtt_resume = "user"

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> LanbonOptionsFlow:
        return LanbonOptionsFlow()

    async def _async_broker_identity(self) -> None:
        await self.async_set_unique_id(BROKER_UNIQUE_ID)
        self._abort_if_unique_id_configured()

    def _mqtt_problem(self) -> str | None:
        """Use the native integration without collecting or duplicating credentials."""
        if not self.hass.config_entries.async_has_entries(
            "mqtt", include_disabled=False, include_ignore=False
        ):
            return "mqtt_not_configured"
        if not mqtt.mqtt_config_entry_enabled(self.hass):
            return "mqtt_not_configured"
        try:
            return None if mqtt.is_connected(self.hass) else "mqtt_unavailable"
        except KeyError, AttributeError:
            # A configured MQTT client can be starting or concurrently unloaded.
            return "mqtt_unavailable"

    async def async_step_mqtt_setup(self, user_input=None) -> FlowResult:
        problem = self._mqtt_problem()
        if user_input is not None and problem is None:
            if self._mqtt_resume == "finish":
                return await self._async_finish_entry()
            return await self.async_step_user()
        return self.async_show_form(
            step_id="mqtt_setup",
            data_schema=vol.Schema({}),
            errors={"base": problem} if problem else {},
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Choose automatic passive discovery or enter known mesh IDs."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        await self._async_broker_identity()
        if self._mqtt_problem():
            return await self.async_step_mqtt_setup()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._prefix = normalize_prefix(user_input[CONF_DISCOVERY_PREFIX])
                mode = user_input[CONF_DISCOVERY_MODE]
                if mode not in {"automatic", "manual"}:
                    raise ValueError("invalid_settings")
            except (ValueError, KeyError) as err:
                errors["base"] = str(err) if isinstance(err, ValueError) else "invalid_settings"
            else:
                if mode == "manual":
                    return await self.async_step_manual()
                return await self.async_step_scan()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DISCOVERY_PREFIX, default=self._prefix): str,
                    vol.Required(CONF_DISCOVERY_MODE, default="automatic"): selector.SelectSelector(
                        {"options": ["automatic", "manual"], "translation_key": "discovery_mode"}
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_mqtt(self, discovery_info: MqttServiceInfo) -> FlowResult:
        """Only a qualified Lanbon config in its native namespace is a hint."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        payload = discovery_info.payload
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif isinstance(payload, bytearray):
            payload = bytes(payload)
        hint = native_config_hint(discovery_info.topic, payload)
        if hint is None:
            return self.async_abort(reason="invalid_discovery_info")
        await self._async_broker_identity()
        self._prefix = hint[CONF_DISCOVERY_PREFIX]
        self._collector = DiscoveryCollector(self._prefix)
        self._collector.accept(discovery_info.topic, payload, retained=discovery_info.retain)
        return self.async_show_form(step_id="discovery_confirm")

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Allow the user to begin a passive scan from a discovered hint."""
        if user_input is None:
            return self.async_show_form(step_id="discovery_confirm")
        return await self.async_step_scan()

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Keep scanning off the flow request, with flow-owned cancellation."""
        if self._scan_task is None:
            self._scan_task = self.hass.async_create_background_task(
                async_scan(
                    self.hass,
                    self._prefix,
                    seconds=DISCOVERY_SECONDS,
                    collector=self._collector,
                ),
                "Lanbon passive MQTT discovery",
            )
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan", progress_action="scan", progress_task=self._scan_task
            )
        try:
            self._scan_result = self._scan_task.result()
        except asyncio.CancelledError, Exception:
            self._scan_result = DiscoveryResult((), "mqtt_unavailable")
        return self.async_show_progress_done(
            next_step_id="select" if self._scan_result.candidates else "manual"
        )

    @callback
    def async_remove(self) -> None:
        """Release temporary subscriptions when the user closes this flow."""
        if self._scan_task is not None and not self._scan_task.done():
            self._scan_task.cancel()

    async def async_step_select(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select current or cached candidates, with manual IDs also accepted."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._groups = normalize_groups(user_input.get(CONF_GROUP_IDS, []))
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return await self.async_step_confirm()
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_GROUP_IDS, default=[]): selector.SelectSelector(
                        {
                            "options": [
                                {"value": item.group_id, "label": item.label}
                                for item in self._scan_result.candidates
                            ],
                            "multiple": True,
                            "custom_value": True,
                        }
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Do not equate a quiet broker with an empty installation."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                self._groups = normalize_groups(user_input.get(CONF_GROUP_IDS, ""))
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return await self.async_step_confirm()
        elif self._scan_result.error:
            errors["base"] = self._scan_result.error
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_GROUP_IDS, default="\n".join(self._groups)
                    ): selector.TextSelector({"multiline": True})
                }
            ),
            errors=errors,
        )

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Explicit acceptance creates configuration permitting fixed queries."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                settings = _settings(user_input, {})
                groups = normalize_groups(self._groups)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return await self._async_prepare_draft(settings, groups, self._prefix, {})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(_settings_schema(user_input or {})),
            description_placeholders={"groups": ", ".join(self._groups)},
            errors=errors,
        )

    async def _async_finish_entry(self) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        await self._async_broker_identity()
        if self._mqtt_problem():
            self._mqtt_resume = "finish"
            return await self.async_step_mqtt_setup()
        return self.async_create_entry(title=NAME, data=self._draft)


class LanbonOptionsFlow(_NetworkForms, config_entries.OptionsFlow):
    """Save options; the integration's update listener owns the actual reload."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        defaults = dict(self.config_entry.data) | dict(self.config_entry.options)
        groups = defaults.get(CONF_GROUP_IDS, [defaults.get(CONF_GROUP_ID, "")])
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                accepted_groups = normalize_groups(user_input.get(CONF_GROUP_IDS, ""))
                prefix = normalize_prefix(user_input.get(CONF_DISCOVERY_PREFIX))
                settings = _settings(
                    {
                        key: value
                        for key, value in user_input.items()
                        if key not in {CONF_GROUP_IDS, CONF_DISCOVERY_PREFIX}
                    },
                    defaults,
                )
            except ValueError as err:
                errors["base"] = str(err)
            else:
                return await self._async_prepare_draft(settings, accepted_groups, prefix, defaults)
        shown = defaults | (user_input or {})
        schema = {
            vol.Required(
                CONF_GROUP_IDS,
                default=shown.get(CONF_GROUP_IDS)
                if isinstance(shown.get(CONF_GROUP_IDS), str)
                else "\n".join(groups),
            ): selector.TextSelector({"multiline": True}),
            vol.Required(
                CONF_DISCOVERY_PREFIX,
                default=shown.get(CONF_DISCOVERY_PREFIX, DEFAULT_DISCOVERY_PREFIX),
            ): str,
        } | _settings_schema(shown)
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema), errors=errors)

    async def _async_finish_entry(self) -> FlowResult:
        return self.async_create_entry(title="", data=self._draft)
