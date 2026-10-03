"""Share useful operational diagnostics without personal HR data."""


async def async_get_config_entry_diagnostics(hass, config_entry) -> dict:
    coordinator = getattr(config_entry, "runtime_data", None)
    data = coordinator.data if coordinator and coordinator.data else {}
    return {
        "version": "6",
        "options": dict(config_entry.options),
        "last_error": coordinator.last_error if coordinator else None,
        "last_update_success": coordinator.last_update_success if coordinator else False,
        "metric_keys": sorted(data.get("metrics", {})),
        "event_count": len(data.get("events", [])),
        "has_current_month": data.get("account", {}).get("current_month"),
        "balance_basis": data.get("account", {}).get("balance_basis"),
    }
