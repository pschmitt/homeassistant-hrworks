"""Explicit working-time actions with previews and no automatic write retries."""

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import WorkerError
from .auth import authenticate
from .const import CONF_WRITES, DOMAIN
from .coordinator import create_issue
from .hrworks.models import TYPES


def register_services(hass) -> None:

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
                vol.Optional("type", default="working_time"): vol.In(TYPES),
                vol.Optional("comment", default=""): vol.All(cv.string, vol.Length(max=500)),
                vol.Optional("dry_run", default=True): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def restart_browser_session(call):
        candidates = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state == ConfigEntryState.LOADED
        ]
        entry_id = call.data.get("config_entry_id")
        if entry_id:
            candidates = [e for e in candidates if e.entry_id == entry_id]
        if not candidates:
            return
        entry = candidates[0]
        try:
            from custom_components.browser_daemon import async_get_client

            client = async_get_client(hass)
            if client:
                sessions = await client.async_list_sessions()
                for s in sessions:
                    if s.get("profile") == "hrworks":
                        await client.async_delete_session(s["id"])
        except Exception:
            pass
        await entry.runtime_data.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        "restart_browser_session",
        restart_browser_session,
        schema=vol.Schema({vol.Optional("config_entry_id"): cv.string}),
    )

    async def _calendar(call, method: str):
        candidates = [
            e
            for e in hass.config_entries.async_entries(DOMAIN)
            if e.state == ConfigEntryState.LOADED
        ]
        entry_id = call.data.get("config_entry_id")
        if entry_id:
            candidates = [e for e in candidates if e.entry_id == entry_id]
        if not candidates:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="select_account"
            )
        entry = candidates[0]
        coord = entry.runtime_data
        opts = {key: value for key, value in call.data.items() if key != "config_entry_id"}
        fetch = getattr(coord.client, method)
        try:
            return await fetch(opts)
        except WorkerError as err:
            if err.code == "session_expired":
                try:
                    await authenticate(coord.client, entry.data, force=True, hass=hass)
                    return await fetch(opts)
                except Exception as rec_err:
                    raise HomeAssistantError(
                        f"calendar_failed: auth_recovery_failed ({rec_err})"
                    ) from rec_err
            raise HomeAssistantError(
                f"calendar_failed: {err.code} (stage={err.stage}, detail={err.detail})"
            ) from err

    async def get_team_absences(call):
        return await _calendar(call, "team_calendar")

    async def get_company_absences(call):
        return await _calendar(call, "company_absences")

    hass.services.async_register(
        DOMAIN,
        "get_team_absences",
        get_team_absences,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("organizational_unit"): cv.string,
                vol.Optional("date"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        "get_company_absences",
        get_company_absences,
        schema=vol.Schema(
            {
                vol.Optional("config_entry_id"): cv.string,
                vol.Optional("units"): vol.All(cv.ensure_list, [cv.string]),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
