"""Native Home Assistant entities for HR WORKS employee accounts."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .api import WorkerClient
from .const import DOMAIN, PLATFORMS
from .coordinator import HrworksCoordinator, clear_issues, create_issue
from .services import register_services

type HrworksConfigEntry = ConfigEntry[HrworksCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: HrworksConfigEntry) -> bool:
    if ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_write_uncertain"):
        create_issue(hass, entry, "write_uncertain")
    client = WorkerClient(hass, entry.data)
    coordinator = HrworksCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    register_services(hass)
    # The first snapshot drives a remote browser worker and can take a minute.
    # Home Assistant's startup waits for every integration's setup, so fetch it
    # in the background; entities stay unavailable until it completes. Auth
    # failures still start the reauth flow from the coordinator.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN} first refresh"
    )
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HrworksConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    clear_issues(hass, entry, include_writes=True)
