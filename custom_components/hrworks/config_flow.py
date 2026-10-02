"""Guided setup, MFA, reauthentication and reconfiguration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME, CONF_PASSWORD, CONF_SCAN_INTERVAL, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import WorkerClient, WorkerError
from .auth import complete_mfa, secret_settings
from .const import (
    CONF_CODE,
    CONF_COMPANY,
    CONF_FUTURE_DAYS,
    CONF_PAST_DAYS,
    CONF_PENDING,
    CONF_PROFILE,
    CONF_SICKNESS,
    CONF_SSH_HOST,
    CONF_SSH_KEY_PATH,
    CONF_SSH_KNOWN_HOSTS,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_TOTP_URI,
    CONF_WORKER_COMMAND,
    CONF_WRITES,
    DEFAULT_FUTURE_DAYS,
    DEFAULT_PAST_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .totp import InvalidTotp

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def connection_schema(defaults: dict, *, existing: bool = False) -> vol.Schema:
    """Do not prefill secrets in forms."""
    return vol.Schema(
        {
            vol.Required(CONF_SSH_HOST, default=defaults.get(CONF_SSH_HOST, "")): TextSelector(),
            vol.Required(CONF_SSH_PORT, default=defaults.get(CONF_SSH_PORT, 22)): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
            vol.Required(
                CONF_SSH_USERNAME, default=defaults.get(CONF_SSH_USERNAME, "")
            ): TextSelector(),
            vol.Required(
                CONF_SSH_KEY_PATH,
                default=defaults.get(CONF_SSH_KEY_PATH, "/config/.ssh/id_ed25519"),
            ): TextSelector(),
            vol.Required(
                CONF_SSH_KNOWN_HOSTS, default=defaults.get(CONF_SSH_KNOWN_HOSTS, "")
            ): TextSelector(TextSelectorConfig(multiline=True)),
            vol.Required(
                CONF_WORKER_COMMAND, default=defaults.get(CONF_WORKER_COMMAND, "hrworks-worker")
            ): TextSelector(),
            vol.Optional(CONF_NAME, default=defaults.get(CONF_NAME, "HR WORKS")): TextSelector(),
        }
    )


def account_schema(defaults: dict, *, existing: bool = False) -> vol.Schema:
    password = vol.Optional(CONF_PASSWORD) if existing else vol.Required(CONF_PASSWORD)
    return vol.Schema(
        {
            vol.Required(CONF_COMPANY, default=defaults.get(CONF_COMPANY, "")): TextSelector(),
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): TextSelector(),
            password: PASSWORD,
            vol.Optional(CONF_TOTP_URI): PASSWORD,
            **(
                {vol.Optional("clear_totp", default=False): BooleanSelector()}
                if defaults.get(CONF_TOTP_URI)
                else {}
            ),
        }
    )


def mfa_schema() -> vol.Schema:
    return vol.Schema({vol.Optional(CONF_CODE): PASSWORD, vol.Optional(CONF_TOTP_URI): PASSWORD})


class HrworksConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect the worker, authenticate the employee, then optionally complete MFA."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}
        self._title = "HR WORKS"
        self._client: WorkerClient | None = None
        self._entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HrworksOptionsFlow:
        return HrworksOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._connection_step("user", user_input)

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        self._entry = self._get_reconfigure_entry()
        if not self._data:
            self._data = dict(self._entry.data)
            self._title = self._entry.title
        return await self._connection_step("reconfigure", user_input)

    async def async_step_reauth(self, entry_data: dict) -> ConfigFlowResult:
        self._entry = self._get_reauth_entry()
        self._data = dict(self._entry.data)
        self._title = self._entry.title
        self._client = WorkerClient(self.hass, self._data)
        return await self.async_step_account()

    async def _connection_step(self, step: str, user_input: dict | None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            client = None
            try:
                settings = {key: value for key, value in user_input.items() if key != CONF_NAME}
                if not all(
                    str(settings.get(key, "")).strip()
                    for key in (
                        CONF_SSH_HOST,
                        CONF_SSH_USERNAME,
                        CONF_SSH_KEY_PATH,
                        CONF_SSH_KNOWN_HOSTS,
                        CONF_WORKER_COMMAND,
                    )
                ):
                    raise WorkerError("invalid_request")
                client = WorkerClient(self.hass, settings)
                await client.health()
            except ValueError:
                if client:
                    await client.close()
                errors["base"] = "invalid_request"
            except WorkerError as err:
                if client:
                    await client.close()
                errors["base"] = err.code
            else:
                if self._client:
                    await self._client.close()
                self._client = client
                self._data.update(settings)
                self._title = user_input.get(CONF_NAME) or self._title
                return await self.async_step_account()
        return self.async_show_form(
            step_id=step,
            data_schema=connection_schema(
                {**self._data, CONF_NAME: self._title}, existing=self._entry is not None
            ),
            errors=errors,
        )

    async def async_step_account(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            company = user_input[CONF_COMPANY].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input.get(CONF_PASSWORD) or self._data.get(CONF_PASSWORD)
            if (
                self._entry
                and self.source != "reconfigure"
                and (
                    company.casefold() != self._entry.data[CONF_COMPANY].casefold()
                    or username.casefold() != self._entry.data[CONF_USERNAME].casefold()
                )
            ):
                errors["base"] = "different_account"
            elif not password:
                errors["base"] = "invalid_auth"
            else:
                try:
                    settings = secret_settings(self._data, user_input)
                    result = await self._client.login(company, username, password, force=True)
                except InvalidTotp:
                    errors["base"] = "invalid_totp"
                except WorkerError as err:
                    errors["base"] = err.code
                else:
                    self._data = settings
                    self._data.update(
                        {
                            CONF_COMPANY: company,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            CONF_PROFILE: result["profile_id"],
                        }
                    )
                    if result.get("mfa_required"):
                        return await self.async_step_mfa(
                            {} if self._data.get(CONF_TOTP_URI) else None
                        )
                    return await self._finish()
        return self.async_show_form(
            step_id="account",
            data_schema=account_schema(self._data, existing=self._entry is not None),
            errors=errors,
        )

    async def async_step_mfa(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                settings = await complete_mfa(self._client, self._data, user_input)
            except InvalidTotp:
                errors["base"] = "invalid_totp"
            except WorkerError as err:
                errors["base"] = err.code
            else:
                self._data = settings
                return await self._finish()
        return self.async_show_form(
            step_id="mfa",
            data_schema=mfa_schema(),
            errors=errors,
        )

    async def _finish(self) -> ConfigFlowResult:
        """Keep one-time codes out of config entries."""
        await self._client.close()
        await self.async_set_unique_id(self._data[CONF_PROFILE])
        if self._entry:
            if self.source == "reconfigure":
                for other in self.hass.config_entries.async_entries(DOMAIN):
                    if other.entry_id != self._entry.entry_id and other.unique_id == self.unique_id:
                        return self.async_abort(reason="already_configured")
                old = self._entry.data[CONF_PROFILE]
                new = self._data[CONF_PROFILE]
                if old != new:
                    # Preserve names, history and automations when switching employee login.
                    registry = er.async_get(self.hass)
                    for entity in er.async_entries_for_config_entry(registry, self._entry.entry_id):
                        if entity.unique_id.startswith(old + "_"):
                            registry.async_update_entity(
                                entity.entity_id, new_unique_id=new + entity.unique_id[len(old) :]
                            )
                    devices = dr.async_get(self.hass)
                    for device in dr.async_entries_for_config_entry(devices, self._entry.entry_id):
                        if (DOMAIN, old) in device.identifiers:
                            devices.async_update_device(
                                device.id,
                                new_identifiers=(device.identifiers - {(DOMAIN, old)})
                                | {(DOMAIN, new)},
                            )
                    self.hass.config_entries.async_update_entry(self._entry, unique_id=new)
            else:
                self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._entry, data=self._data, title=self._title
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=self._title,
            data=self._data,
            options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
        )


class HrworksOptionsFlow(OptionsFlow):
    """Adjust polling, calendar coverage and optional time entry."""

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        options = self.config_entry.options

        def number(key: str, default: int, minimum: int, maximum: int):
            return (
                vol.Required(key, default=options.get(key, default)),
                NumberSelector(
                    NumberSelectorConfig(
                        min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX
                    )
                ),
            )

        fields = dict(
            [
                number(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, 300, 86400),
                number(CONF_PAST_DAYS, DEFAULT_PAST_DAYS, 0, 730),
                number(CONF_FUTURE_DAYS, DEFAULT_FUTURE_DAYS, 0, 730),
            ]
        )
        fields.update(
            {
                vol.Required(
                    CONF_SICKNESS, default=options.get(CONF_SICKNESS, True)
                ): BooleanSelector(),
                vol.Required(
                    CONF_PENDING, default=options.get(CONF_PENDING, False)
                ): BooleanSelector(),
                vol.Required(
                    CONF_WRITES, default=options.get(CONF_WRITES, False)
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))
