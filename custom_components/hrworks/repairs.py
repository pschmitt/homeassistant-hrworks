"""Custom Repairs flows for HR WORKS issues outside config-entry reauth."""

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN


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


class WorkerUnavailableRepair(RepairsFlow):
    """Reload the integration to restart the worker and re-establish the connection."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.entry_id)
            if not entry:
                return self.async_abort(reason="entry_removed")
            await self.hass.config_entries.async_reload(self.entry_id)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(hass, issue_id, data):
    entry_id = (data or {}).get("entry_id", "")
    if issue_id.endswith("_write_uncertain"):
        return WorkingTimeReviewRepair(issue_id)
    if issue_id.endswith("_worker_unavailable"):
        return WorkerUnavailableRepair(entry_id)
    return None
