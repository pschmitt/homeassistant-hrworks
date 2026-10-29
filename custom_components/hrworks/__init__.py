"""Native Home Assistant entities for HR WORKS employee accounts."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .api import WorkerClient
from .const import DOMAIN, PLATFORMS
from .coordinator import HrworksCoordinator, clear_issues, create_issue
from .services import register_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_URL = f"/{DOMAIN}/work-heatmap-card.js"
CARD_PATH = Path(__file__).parent / "frontend" / "work-heatmap-card.js"
LEGACY_CARD_URL = f"/local/{CARD_PATH.name}"

type HrworksConfigEntry = ConfigEntry[HrworksCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the Lovelace card and register it as a dashboard resource."""
    del config
    if CARD_PATH.exists():
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)]
        )
        digest = await hass.async_add_executor_job(
            lambda: hashlib.sha256(CARD_PATH.read_bytes()).hexdigest()[:10]
        )
        await _async_register_card(hass, f"{CARD_URL}?v={digest}")
    return True


async def _async_register_card(hass: HomeAssistant, url: str) -> None:
    """Keep exactly one Lovelace resource pointing at the current card."""
    lovelace = hass.data.get(LOVELACE_DATA)
    if lovelace is None or lovelace.resource_mode != "storage":
        add_extra_js_url(hass, url)
        return
    resources = lovelace.resources
    await resources.async_get_info()

    # Remove legacy /local/ resource if present
    legacy = [
        item for item in resources.async_items() if item["url"].split("?")[0] == LEGACY_CARD_URL
    ]
    for old in legacy:
        await resources.async_delete_item(old["id"])

    mine = [item for item in resources.async_items() if item["url"].split("?")[0] == CARD_URL]
    for duplicate in mine[1:]:
        await resources.async_delete_item(duplicate["id"])
    if not mine:
        _LOGGER.info("Adding Work Heatmap card as a dashboard resource")
        await resources.async_create_item({"res_type": "module", "url": url})
    elif mine[0]["url"] != url:
        await resources.async_update_item(mine[0]["id"], {"res_type": "module", "url": url})


async def async_setup_entry(hass: HomeAssistant, entry: HrworksConfigEntry) -> bool:
    # Remove the legacy duplicate login repair; ConfigEntryAuthFailed owns reauth now.
    ir.async_delete_issue(hass, DOMAIN, f"{entry.entry_id}_session_expired")
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
    entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN} first refresh")
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
