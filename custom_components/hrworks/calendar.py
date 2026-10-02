"""Native calendars with exclusive end dates and range-aware fetching."""

from datetime import date, datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.util import dt as dt_util

from .const import CONF_SICKNESS
from .entity import HrworksEntity


def calendar_event(item: dict) -> CalendarEvent:
    return CalendarEvent(
        start=date.fromisoformat(item["start"]),
        end=date.fromisoformat(item["end"]),
        summary=item["summary"],
        description=item.get("description"),
        uid=item["uid"],
    )


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    keys = ["leave"]
    if entry.options.get(CONF_SICKNESS, True):
        keys.append("sickness")
    async_add_entities(HrworksCalendar(entry.runtime_data, key) for key in keys)


class HrworksCalendar(HrworksEntity, CalendarEntity):
    """A cached, read-only employee calendar."""

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self.key = key
        self._attr_icon = "mdi:calendar-heart" if key == "leave" else "mdi:calendar-alert"

    @property
    def event(self) -> CalendarEvent | None:
        today = date.fromisoformat(
            (self.coordinator.data or {})
            .get("today", {})
            .get("date", dt_util.now().date().isoformat())
        )
        candidates = sorted(
            (
                e
                for e in (self.coordinator.data or {}).get("events", [])
                if e["kind"] == self.key and date.fromisoformat(e["end"]) > today
            ),
            key=lambda e: (e["start"], e["uid"]),
        )
        return calendar_event(candidates[0]) if candidates else None

    async def async_get_events(
        self, hass, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Serve the explicitly configured calendar window without hidden extra requests."""
        if not self.available:
            return []
        start = dt_util.as_local(start_date).date()
        end = dt_util.as_local(end_date).date()
        if dt_util.as_local(end_date).time().isoformat() != "00:00:00":
            from datetime import timedelta

            end += timedelta(days=1)
        return [
            calendar_event(e)
            for e in (self.coordinator.data or {}).get("events", [])
            if e["kind"] == self.key
            and date.fromisoformat(e["start"]) < end
            and date.fromisoformat(e["end"]) > start
        ]

    @property
    def extra_state_attributes(self) -> dict:
        return {"coverage": (self.coordinator.data or {}).get("calendar_window", {})}
