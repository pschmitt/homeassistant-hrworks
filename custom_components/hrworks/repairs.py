"""Complete employee MFA directly from the Home Assistant Repairs screen."""

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import issue_registry as ir

from .api import WorkerClient, WorkerError
from .auth import complete_mfa, secret_settings
from .config_flow import account_schema, mfa_schema
from .const import CONF_COMPANY, CONF_TOTP_URI, DOMAIN
from .coordinator import clear_issues
from .totp import InvalidTotp


class EmployeeLoginRepair(RepairsFlow):
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self.client = None
        self.settings = {}

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
                settings = secret_settings(entry.data, user_input)
                result = await self.client.login(
                    entry.data[CONF_COMPANY], entry.data[CONF_USERNAME], password, force=True
                )
            except InvalidTotp:
                errors["base"] = "invalid_totp"
            except WorkerError as err:
                errors["base"] = err.code
            else:
                # Never change the employee identity as part of a repair.
                if result["profile_id"] != entry.unique_id:
                    return self.async_abort(reason="different_account")
                self.settings = settings
                if result.get("mfa_required"):
                    return await self.async_step_mfa({} if settings.get(CONF_TOTP_URI) else None)
                return await self._finish(entry)
        return self.async_show_form(
            step_id="login",
            errors=errors,
            data_schema=vol.Schema(
                {
                    key: value
                    for key, value in account_schema(entry.data, existing=True).schema.items()
                    if str(key) not in {CONF_COMPANY, CONF_USERNAME}
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
                settings = await complete_mfa(self.client, self.settings, user_input)
            except InvalidTotp:
                errors["base"] = "invalid_totp"
            except WorkerError as err:
                errors["base"] = err.code
            else:
                self.settings = settings
                return await self._finish(entry)
        return self.async_show_form(
            step_id="mfa",
            errors=errors,
            data_schema=mfa_schema(),
        )

    async def _finish(self, entry):
        await self.client.close()
        if self.settings != entry.data:
            self.hass.config_entries.async_update_entry(entry, data=self.settings)
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


class WorkingTimeReviewRepair(RepairsFlow):
    """Acknowledge an uncertain submission only after reviewing the portal."""

    def __init__(self, issue_id: str) -> None:
        self.issue_id = issue_id

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(hass, issue_id, data):
    if issue_id.endswith("_write_uncertain"):
        return WorkingTimeReviewRepair(issue_id)
    return EmployeeLoginRepair((data or {}).get("entry_id", ""))
