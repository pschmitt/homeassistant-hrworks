"""Explicit working-time actions with previews and no automatic write retries."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import WorkerError
from .const import CONF_WRITES, DOMAIN
from .coordinator import create_issue


def register_services(hass) -> None:
    if hass.services.has_service(DOMAIN, "record_working_time"):
        return

    async def record(call):
        candidates = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state == ConfigEntryState.LOADED
        ]
        entry_id = call.data.get("config_entry_id")
        if entry_id:
            candidates = [e for e in candidates if e.entry_id == entry_id]
        if len(candidates) != 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="select_account"
            )
        entry = candidates[0]
        if not call.data["dry_run"] and not entry.options.get(CONF_WRITES, False):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="writes_disabled"
            )
        try:
            result = await entry.runtime_data.client.record(
                {key: value for key, value in call.data.items() if key != "config_entry_id"}
            )
        except WorkerError as err:
            if err.code in {"cannot_connect", "write_uncertain"} and not call.data["dry_run"]:
                create_issue(hass, entry, "write_uncertain")
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="record_failed",
                translation_placeholders={"reason": err.code},
            ) from err
        if result.get("saved"):
            await entry.runtime_data.async_request_refresh()
        return result

    hass.services.async_register(
        DOMAIN,
        "record_working_time",
        record,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Required("start"): cv.string,
                vol.Required("end"): cv.string,
                vol.Optional("type", default="working_time"): vol.In(
                    [
                        "working_time",
                        "doctors_appointment",
                        "business_errand",
                        "education_and_training",
                    ]
                ),
                vol.Optional("comment", default=""): vol.All(cv.string, vol.Length(max=500)),
                vol.Optional("dry_run", default=True): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
