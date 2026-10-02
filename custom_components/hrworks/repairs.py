"""Complete employee MFA directly from the Home Assistant Repairs screen."""

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import WorkerClient, WorkerError
from .const import CONF_CODE, CONF_COMPANY, DOMAIN
from .coordinator import clear_issues


class EmployeeLoginRepair(RepairsFlow):
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self.client = None
        self.password = None

    async def async_step_init(self, user_input=None):
        return await self.async_step_login(user_input)

    async def async_step_login(self, user_input=None):
        errors = {}
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry:
            return self.async_abort(reason="entry_removed")
        if user_input is not None:
            if self.client:
                await self.client.close()
            self.client = WorkerClient(self.hass, entry.data)
            password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
            try:
                result = await self.client.login(
                    entry.data[CONF_COMPANY], entry.data[CONF_USERNAME], password
                )
            except WorkerError as err:
                errors["base"] = err.code
            else:
                # Never change the employee identity as part of a repair.
                if result["profile_id"] != entry.unique_id:
                    return self.async_abort(reason="different_account")
                self.password = password
                if result.get("mfa_required"):
                    return await self.async_step_mfa()
                return await self._finish(entry)
        return self.async_show_form(
            step_id="login",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
        )

    async def async_step_mfa(self, user_input=None):
        errors = {}
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if not entry:
            return self.async_abort(reason="entry_removed")
        if user_input is not None:
            try:
                await self.client.mfa(user_input[CONF_CODE])
            except WorkerError as err:
                errors["base"] = err.code
            else:
                return await self._finish(entry)
        return self.async_show_form(
            step_id="mfa",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CODE): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
        )

    async def _finish(self, entry):
        await self.client.close()
        if self.password and self.password != entry.data[CONF_PASSWORD]:
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_PASSWORD: self.password}
            )
        clear_issues(self.hass, entry)
        # Abort the matching reauth flow so the Repairs and Integrations screens agree.
        for flow in self.hass.config_entries.flow.async_progress():
            if (
                flow["handler"] == DOMAIN
                and flow["context"].get("source") == "reauth"
                and flow["context"].get("entry_id") == entry.entry_id
            ):
                self.hass.config_entries.flow.async_abort(flow["flow_id"])
        await self.hass.config_entries.async_reload(entry.entry_id)
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(hass, issue_id, data):
    return EmployeeLoginRepair((data or {}).get("entry_id", ""))
