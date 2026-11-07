"""Guided setup, MFA, reauthentication and reconfiguration."""

from __future__ import annotations

from urllib.parse import urlsplit

import probatio as vol
from homeassistant import data_entry_flow
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

from .api import WorkerError, create_client
from .auth import complete_mfa, credentials, secret_settings
from .const import (
    CONF_BD_ENTRY_ID,
    CONF_BD_NODE_GROUP,
    CONF_BD_OVERRIDE,
    CONF_BD_PROFILE,
    CONF_BD_SECRET,
    CONF_BD_URL,
    CONF_CODE,
    CONF_COMPANY,
    CONF_FUTURE_DAYS,
    CONF_PAST_DAYS,
    CONF_PENDING,
    CONF_PROFILE,
    CONF_RBW_ENTRY_ID,
    CONF_RBW_ITEM_ID,
    CONF_SICKNESS,
    CONF_TOTP_URI,
    CONF_WRITES,
    DEFAULT_FUTURE_DAYS,
    DEFAULT_PAST_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .setup_form import (
    SECTION_BROWSER_DAEMON,
    SECTION_CREDENTIALS,
    browser_daemon_fields,
    clear_rbw_when_empty,
    flatten_sections,
    needs_rbw_item,
    rbw_field,
    rbw_item_options,
    rbw_item_schema,
    saved_rbw_item,
    section,
)
from .totp import InvalidTotp

PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
CONNECTION_FIELDS = (
    CONF_BD_ENTRY_ID,
    CONF_BD_OVERRIDE,
    CONF_BD_URL,
    CONF_BD_SECRET,
    CONF_BD_PROFILE,
    CONF_BD_NODE_GROUP,
)
# Never shown again when a form comes back with an error.
SECRET_FIELDS = (CONF_PASSWORD, CONF_TOTP_URI, CONF_CODE, CONF_BD_SECRET)


def credential_fields(defaults: dict, hass) -> dict:
    """Credentials from rbw, or a password and TOTP URI when no rbw integration is chosen."""
    direct = {"field": CONF_RBW_ENTRY_ID, "operator": "not_exists"}
    return {
        **rbw_field(hass, defaults),
        data_entry_flow.Optional(CONF_PASSWORD, visible=direct): PASSWORD,
        data_entry_flow.Optional(CONF_TOTP_URI, visible=direct): PASSWORD,
        data_entry_flow.Optional(CONF_CODE, visible=direct): PASSWORD,
        **(
            {
                data_entry_flow.Optional(
                    "clear_totp", default=False, visible=direct
                ): BooleanSelector()
            }
            if defaults.get(CONF_TOTP_URI)
            else {}
        ),
    }


def setup_schema(defaults: dict, hass) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(CONF_NAME, default=defaults.get(CONF_NAME, "HR WORKS")): TextSelector(),
            vol.Required(CONF_COMPANY, default=defaults.get(CONF_COMPANY, "")): TextSelector(),
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): TextSelector(),
            vol.Required(SECTION_BROWSER_DAEMON): section(
                browser_daemon_fields(hass, defaults, profile="ha-hrworks")
            ),
            vol.Required(SECTION_CREDENTIALS): section(credential_fields(defaults, hass)),
        }
    )


def mfa_schema() -> vol.Schema:
    return vol.Schema({vol.Optional(CONF_CODE): PASSWORD, vol.Optional(CONF_TOTP_URI): PASSWORD})


class HrworksConfigFlow(ConfigFlow, domain=DOMAIN):
    """One form: the employee, Browser Daemon and credentials. Then the rbw item, if any."""

    VERSION = 2
    MINOR_VERSION = 1

    def __init__(self) -> None:
        self._data: dict = {}
        self._title = "HR WORKS"
        self._client = None
        self._entry: ConfigEntry | None = None
        self._mfa_pending = False
        self._account_input: dict = {}
        self._form_values: dict = {}
        self._step = "user"

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HrworksOptionsFlow:
        return HrworksOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._setup_step("user", user_input)

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        self._entry = self._get_reconfigure_entry()
        if not self._data:
            self._data = dict(self._entry.data)
            self._title = self._entry.title
        return await self._setup_step("reconfigure", user_input)

    async def async_step_reauth(self, entry_data: dict) -> ConfigFlowResult:
        self._entry = self._get_reauth_entry()
        self._data = dict(self._entry.data)
        self._title = self._entry.title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._setup_step("reauth_confirm", user_input)

    async def _setup_step(self, step: str, user_input: dict | None) -> ConfigFlowResult:
        self._step = step
        if user_input is None:
            return self._show_form({})
        submitted = flatten_sections(user_input)
        self._form_values = {
            key: value for key, value in submitted.items() if key not in SECRET_FIELDS
        }
        if not self._mfa_pending:
            if needs_rbw_item(submitted):
                self._account_input = submitted
                return await self.async_step_rbw_item()
            if errors := await self._connect(submitted):
                return self._show_form(errors)
        return await self._login(submitted)

    def _show_form(self, errors: dict) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=self._step,
            data_schema=setup_schema(
                {**self._data, CONF_NAME: self._title, **self._form_values}, self.hass
            ),
            errors=errors,
        )

    async def _connect(self, submitted: dict) -> dict:
        """Reach browser-daemon. Returns the form errors."""
        client = None
        try:
            settings = {key: submitted[key] for key in CONNECTION_FIELDS if key in submitted}
            settings[CONF_BD_URL] = str(settings.get(CONF_BD_URL) or "").strip()
            settings.setdefault(CONF_BD_PROFILE, self._data.get(CONF_BD_PROFILE, "ha-hrworks"))
            required = ()
            # Hidden fields are not submitted, so stale values are cleared here.
            if settings.pop(CONF_BD_OVERRIDE, bool(settings[CONF_BD_URL])):
                settings[CONF_BD_ENTRY_ID] = ""
                # A blank secret keeps the saved one (reconfigure).
                settings[CONF_BD_SECRET] = settings.get(CONF_BD_SECRET) or self._data.get(
                    CONF_BD_SECRET
                )
                required += (CONF_BD_URL, CONF_BD_SECRET)
                if urlsplit(settings[CONF_BD_URL]).scheme not in {"http", "https"}:
                    raise WorkerError("invalid_request")
            else:
                # The Browser Daemon integration's connection: nothing to keep here.
                settings[CONF_BD_ENTRY_ID] = settings.get(CONF_BD_ENTRY_ID) or ""
                settings[CONF_BD_URL] = ""
                settings[CONF_BD_SECRET] = None
            if not all(str(settings.get(key) or "").strip() for key in required):
                raise WorkerError("invalid_request")
            client = create_client(self.hass, settings)
            await client.health()
        except ValueError:
            if client:
                await client.close()
            return {"base": "invalid_request"}
        except WorkerError as err:
            if client:
                await client.close()
            return {"base": err.code}
        if self._client:
            await self._client.close()
        self._client = client
        self._data.update(settings)
        self._title = submitted.get(CONF_NAME) or self._title
        return {}

    async def _login(self, user_input: dict) -> ConfigFlowResult:
        errors = {}
        user_input = clear_rbw_when_empty(user_input)
        if self._mfa_pending:
            try:
                self._data = await complete_mfa(
                    self._client, self._data, user_input, hass=self.hass
                )
            except InvalidTotp:
                errors["base"] = "invalid_totp"
            except WorkerError as err:
                errors["base"] = err.code
            else:
                self._mfa_pending = False
                return await self._finish()
        else:
            company = user_input[CONF_COMPANY].strip()
            username = user_input[CONF_USERNAME].strip()
            selected = {
                **self._data,
                **{
                    key: user_input[key]
                    for key in (CONF_RBW_ENTRY_ID, CONF_RBW_ITEM_ID)
                    if key in user_input
                },
                CONF_COMPANY: company,
                CONF_USERNAME: username,
            }
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
            elif bool(selected.get(CONF_RBW_ENTRY_ID)) != bool(selected.get(CONF_RBW_ITEM_ID)):
                errors["base"] = "rbw_incomplete"
            elif not password and not selected.get(CONF_RBW_ENTRY_ID):
                errors["base"] = "invalid_auth"
            else:
                try:
                    settings = secret_settings(selected, user_input)
                    resolved = await credentials(self.hass, {**settings, CONF_PASSWORD: password})
                    result = await self._client.login(
                        company, resolved[CONF_USERNAME], resolved[CONF_PASSWORD], force=True
                    )
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
                            **(
                                {CONF_PASSWORD: password}
                                if not settings.get(CONF_RBW_ENTRY_ID)
                                else {}
                            ),
                            CONF_PROFILE: result["profile_id"],
                        }
                    )
                    if settings.get(CONF_RBW_ENTRY_ID):
                        self._data.pop(CONF_PASSWORD, None)
                        self._data.pop(CONF_TOTP_URI, None)
                    if result.get("mfa_required"):
                        self._mfa_pending = True
                        if (
                            not user_input.get(CONF_CODE)
                            and not self._data.get(CONF_TOTP_URI)
                            and not self._data.get(CONF_RBW_ENTRY_ID)
                        ):
                            errors["base"] = "mfa_required"
                        else:
                            try:
                                self._data = await complete_mfa(
                                    self._client, self._data, user_input, hass=self.hass
                                )
                            except InvalidTotp:
                                errors["base"] = "invalid_totp"
                            except WorkerError as err:
                                errors["base"] = err.code
                            else:
                                self._mfa_pending = False
                                return await self._finish()
                    else:
                        return await self._finish()
        return self._show_form(errors)

    async def async_step_rbw_item(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Choose the item once the rbw integration is known."""
        if user_input is not None:
            return await self._setup_step(self._step, {**self._account_input, **user_input})
        vault = self._account_input[CONF_RBW_ENTRY_ID]
        saved = saved_rbw_item(self._data, vault)
        options = await rbw_item_options(self.hass, vault, saved)
        return self.async_show_form(step_id="rbw_item", data_schema=rbw_item_schema(options, saved))

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
            else:
                self._abort_if_unique_id_mismatch()
            # Loaded entries reload through their update listener. An unchanged
            # login or an entry which failed setup still needs an explicit reload.
            needs_reload = not self._entry.update_listeners or (
                self._entry.data == self._data
                and self._entry.title == self._title
                and self._entry.unique_id == self._data[CONF_PROFILE]
            )
            result = self.async_update_and_abort(
                self._entry, data=self._data, title=self._title, unique_id=self._data[CONF_PROFILE]
            )
            if needs_reload:
                self.hass.config_entries.async_schedule_reload(self._entry.entry_id)
            return result
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
