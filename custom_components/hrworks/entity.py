"""One service device for an employee's HR WORKS account."""

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PROFILE, DOMAIN
from .coordinator import HrworksCoordinator


class HrworksEntity(CoordinatorEntity[HrworksCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HrworksCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.data[CONF_PROFILE]}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_PROFILE])},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
            model="Employee account",
            configuration_url="https://login.hrworks.de/",
        )
