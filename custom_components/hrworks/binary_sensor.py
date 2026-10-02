"""Presence and absence status for automations."""

from datetime import date

from homeassistant.components.binary_sensor import BinarySensorEntity

from .const import CONF_SICKNESS
from .entity import HrworksEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    keys = ["clocked_in", "on_leave"]
    if entry.options.get(CONF_SICKNESS, True):
        keys.append("on_sick_leave")
    async_add_entities(HrworksBinarySensor(entry.runtime_data, key) for key in keys)


class HrworksBinarySensor(HrworksEntity, BinarySensorEntity):
    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self.key = key
        self._attr_icon = {
            "clocked_in": "mdi:briefcase-check-outline",
            "on_leave": "mdi:beach",
            "on_sick_leave": "mdi:medical-bag",
        }[key]

    @property
    def available(self) -> bool:
        data = self.coordinator.data or {}
        return super().available and (
            data.get("clocked_in") is not None
            if self.key == "clocked_in"
            else data.get("events") is not None
        )

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        if self.key == "clocked_in":
            return data.get("clocked_in")
        today = data.get("today", {}).get("date")
        if not today:
            return None
        kind = "sickness" if self.key == "on_sick_leave" else "leave"
        return any(
            event["kind"] == kind
            and event.get("approved", False)
            and date.fromisoformat(event["start"])
            <= date.fromisoformat(today)
            < date.fromisoformat(event["end"])
            for event in data.get("events", [])
        )
