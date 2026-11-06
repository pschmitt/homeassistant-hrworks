"""Refresh the employee data on demand."""

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory

from .entity import HrworksEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        [
            HrworksRefreshButton(entry.runtime_data, "refresh"),
            HrworksRestartBrowserSessionButton(entry.runtime_data, "restart_browser_session"),
        ]
    )


class HrworksRefreshButton(HrworksEntity, ButtonEntity):
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        await self.coordinator.async_request_refresh()


class HrworksRestartBrowserSessionButton(HrworksEntity, ButtonEntity):
    """Restart the active HR WORKS browser session on browser-daemon."""

    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        try:
            await self.coordinator.client.stop_profile_browser()
        except Exception:
            pass
        await self.coordinator.async_request_refresh()
