"""Numeric working-time and vacation values with explicit reporting periods."""

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime

from .entity import HrworksEntity
from .workdays import WINDOW_DAYS

TIME_SENSORS = {
    "monthly_worked": "mdi:briefcase-clock-outline",
    "monthly_target": "mdi:clock-check-outline",
    "monthly_balance": "mdi:clock-plus-outline",
    "carryover": "mdi:calendar-arrow-right",
    "total_balance": "mdi:timer-sand",
    "absence_deduction": "mdi:calendar-minus",
    "time_credit": "mdi:clock-edit-outline",
    "today_worked": "mdi:briefcase-clock",
    "today_target": "mdi:clock-check-outline",
    "today_balance": "mdi:clock-plus-outline",
}
DESCRIPTIONS = tuple(
    SensorEntityDescription(
        key=key,
        translation_key=key,
        icon=icon,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=key not in {"absence_deduction", "time_credit"},
    )
    for key, icon in TIME_SENSORS.items()
) + (
    SensorEntityDescription(
        key="vacation_remaining",
        translation_key="vacation_remaining",
        icon="mdi:beach",
        native_unit_of_measurement="d",
    ),
    SensorEntityDescription(
        key="vacation_approved",
        translation_key="vacation_approved",
        icon="mdi:calendar-check",
        native_unit_of_measurement="d",
    ),
    SensorEntityDescription(
        key="last_updated",
        translation_key="last_updated",
        icon="mdi:cloud-check-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)

SECONDS = dict(
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.SECONDS,
    state_class=SensorStateClass.MEASUREMENT,
    suggested_display_precision=1,
    entity_category=EntityCategory.DIAGNOSTIC,
)
COUNTER = dict(
    state_class=SensorStateClass.TOTAL_INCREASING, entity_category=EntityCategory.DIAGNOSTIC
)
TEXT = dict(entity_category=EntityCategory.DIAGNOSTIC)
DIAGNOSTIC_DESCRIPTIONS = (
    SensorEntityDescription(
        key="update_duration",
        translation_key="update_duration",
        icon="mdi:timer-outline",
        **SECONDS,
    ),
    SensorEntityDescription(
        key="worker_duration",
        translation_key="worker_duration",
        icon="mdi:timer-cog-outline",
        **SECONDS,
    ),
    SensorEntityDescription(
        key="browser_connect_duration",
        translation_key="browser_connect_duration",
        icon="mdi:lan-connect",
        entity_registry_enabled_default=False,
        **SECONDS,
    ),
    SensorEntityDescription(
        key="next_update",
        translation_key="next_update",
        icon="mdi:calendar-clock",
        device_class=SensorDeviceClass.TIMESTAMP,
        **TEXT,
    ),
    SensorEntityDescription(
        key="browser_session", translation_key="browser_session", icon="mdi:tab", **TEXT
    ),
    SensorEntityDescription(
        key="browser_version",
        translation_key="browser_version",
        icon="mdi:google-chrome",
        entity_registry_enabled_default=False,
        **TEXT,
    ),
    SensorEntityDescription(
        key="last_error", translation_key="last_error", icon="mdi:alert-circle-outline", **TEXT
    ),
    SensorEntityDescription(
        key="last_attempt",
        translation_key="last_attempt",
        icon="mdi:clock-alert-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        **TEXT,
    ),
    SensorEntityDescription(
        key="last_error_stage",
        translation_key="last_error_stage",
        icon="mdi:debug-step-over",
        **TEXT,
    ),
    SensorEntityDescription(
        key="last_error_detail",
        translation_key="last_error_detail",
        icon="mdi:information-outline",
        **TEXT,
    ),
    SensorEntityDescription(
        key="consecutive_failures",
        translation_key="consecutive_failures",
        icon="mdi:numeric-negative-1",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SensorEntityDescription(
        key="update_count", translation_key="update_count", icon="mdi:counter", **COUNTER
    ),
    SensorEntityDescription(
        key="failed_updates",
        translation_key="failed_updates",
        icon="mdi:close-circle-multiple-outline",
        **COUNTER,
    ),
    SensorEntityDescription(
        key="reauthentications",
        translation_key="reauthentications",
        icon="mdi:login-variant",
        **COUNTER,
    ),
    SensorEntityDescription(
        key="event_count",
        translation_key="event_count",
        icon="mdi:calendar-multiple",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)
DIAGNOSTIC_KEYS = {d.key for d in DIAGNOSTIC_DESCRIPTIONS}
# Reported even while updates fail: they are what explains the failure.
ALWAYS_AVAILABLE = {
    "update_duration",
    "next_update",
    "last_error",
    "last_attempt",
    "last_error_stage",
    "last_error_detail",
    "consecutive_failures",
    "update_count",
    "failed_updates",
    "reauthentications",
}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities(
        [
            *(
                HrworksSensor(entry.runtime_data, description)
                for description in DESCRIPTIONS + DIAGNOSTIC_DESCRIPTIONS
            ),
            HrworksWorkDaysSensor(entry.runtime_data),
        ]
    )


class HrworksWorkDaysSensor(HrworksEntity, SensorEntity):
    """Worked weekdays of the last 30 days; the per-day rows feed dashboard heatmaps."""

    _attr_icon = "mdi:calendar-month-outline"
    _attr_native_unit_of_measurement = "d"
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The day list changes every update and is not useful history.
    _unrecorded_attributes = frozenset({"days"})

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "work_days")

    @property
    def _rows(self) -> list[dict] | None:
        return (self.coordinator.data or {}).get("work_days")

    @property
    def available(self) -> bool:
        return super().available and self._rows is not None

    @property
    def native_value(self) -> int | None:
        rows = self._rows
        return None if rows is None else sum(row["status"] == "worked" for row in rows)

    @property
    def extra_state_attributes(self) -> dict:
        rows = self._rows or []
        return {
            "window_days": WINDOW_DAYS,
            "worked_hours": round(sum(row["worked"] or 0 for row in rows), 2),
            "days": rows,
        }


class HrworksSensor(HrworksEntity, SensorEntity):
    def __init__(self, coordinator, description: SensorEntityDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        if self.entity_description.key in ALWAYS_AVAILABLE:
            return self.native_value is not None
        return super().available and self.native_value is not None

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        key = self.entity_description.key
        if key == "last_updated":
            value = data.get("fetched_at")
            return datetime.fromisoformat(value) if value else None
        if key in DIAGNOSTIC_KEYS:
            return self._diagnostic(key, data)
        value = data.get("metrics", {}).get(key)
        if value is None:
            return None
        return round(value / 60, 4) if key in TIME_SENSORS else value

    def _diagnostic(self, key: str, data: dict):
        coordinator = self.coordinator
        report = data.get("diagnostics") or {}
        match key:
            case "update_duration":
                return coordinator.last_duration
            case "worker_duration":
                return report.get("worker_seconds")
            case "browser_connect_duration":
                return report.get("connect_seconds")
            case "next_update":
                return coordinator.next_update
            case "browser_session":
                return report.get("browser_session")
            case "browser_version":
                return report.get("browser_version")
            case "last_error":
                return coordinator.last_error or "none"
            case "last_attempt":
                return coordinator.last_attempt
            case "last_error_stage":
                return coordinator.last_error_stage or "none"
            case "last_error_detail":
                return coordinator.last_error_detail or (
                    "not_reported" if coordinator.last_error else "none"
                )
            case "consecutive_failures":
                return coordinator.consecutive_failures
            case "update_count":
                return coordinator.update_count
            case "failed_updates":
                return coordinator.failed_updates
            case "reauthentications":
                return coordinator.reauthentications
            case "event_count":
                return len(data["events"]) if "events" in data else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        key = self.entity_description.key
        if key in DIAGNOSTIC_KEYS:
            return self._diagnostic_attributes(key, data)
        attrs = {"source": "HR WORKS employee portal"}
        if key in TIME_SENSORS:
            minutes = data.get("metrics", {}).get(key)
            if minutes is not None:
                sign = "-" if minutes < 0 else ""
                hours, remainder = divmod(abs(round(minutes)), 60)
                attrs.update({"minutes": minutes, "formatted": f"{sign}{hours}:{remainder:02d}"})
            attrs.update(data.get("today" if key.startswith("today_") else "account", {}))
        elif key.startswith("vacation_"):
            attrs["year"] = data.get("vacation_year")
        return attrs

    def _diagnostic_attributes(self, key: str, data: dict) -> dict:
        report = data.get("diagnostics") or {}
        if key == "worker_duration":
            return {f"{step}_seconds": value for step, value in (data.get("timings") or {}).items()}
        if key == "browser_session":
            attrs = {
                attr: report.get(source)
                for attr, source in {
                    "backend": "backend",
                    "endpoint": "browser_endpoint",
                    "browser_version": "browser_version",
                    "connections": "connections",
                }.items()
                if report.get(source) is not None
            }
            bd_data = self.hass.data.get("browser_daemon")
            if isinstance(bd_data, dict):
                for coord in bd_data.values():
                    if hasattr(coord, "data") and isinstance(coord.data, dict):
                        local_node = coord.data.get("local_node") or {}
                        if "name" in local_node:
                            attrs["browser_daemon_node"] = local_node["name"]
                        break
            return attrs
        if key == "last_error":
            return {
                "last_update_success": self.coordinator.last_update_success,
                "stage": self.coordinator.last_error_stage,
                "detail": self.coordinator.last_error_detail,
            }
        if key == "last_error_stage":
            descriptions = {
                "browser_connect": "Opening the remote Chromium DevTools connection",
                "browser_operation": "Using the browser after connecting",
                "worker_transport": "Connecting to or starting the SSH worker",
                "worker_response": "The worker returned an error without a more specific stage",
            }
            return {"description": descriptions.get(self.native_value, "No error stage reported")}
        if key == "last_error_detail":
            descriptions = {
                "http_401": "The browser endpoint rejected authentication",
                "http_403": "The browser endpoint denied the connection",
                "http_429": "The browser endpoint rate limited the connection",
                "http_5xx": "The browser endpoint returned a server error",
                "timeout": "The operation timed out",
                "connection_failed": "The browser connection failed without an HTTP status",
                "not_reported": "The worker version returned only an error code",
            }
            return {"description": descriptions.get(self.native_value, "No detail reported")}
        return {}
