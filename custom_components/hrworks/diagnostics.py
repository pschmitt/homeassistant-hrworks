"""Share useful operational diagnostics without personal HR data."""


async def async_get_config_entry_diagnostics(hass, config_entry) -> dict:
    coordinator = getattr(config_entry, "runtime_data", None)
    data = coordinator.data if coordinator and coordinator.data else {}
    return {
        "version": "9.2.0",
        "options": dict(config_entry.options),
        "last_error": coordinator.last_error if coordinator else None,
        "last_error_stage": coordinator.last_error_stage if coordinator else None,
        "last_error_detail": coordinator.last_error_detail if coordinator else None,
        "last_attempt": coordinator.last_attempt if coordinator else None,
        "last_update_success": coordinator.last_update_success if coordinator else False,
        "update_duration_seconds": coordinator.last_duration if coordinator else None,
        "update_count": coordinator.update_count if coordinator else 0,
        "failed_updates": coordinator.failed_updates if coordinator else 0,
        "consecutive_failures": coordinator.consecutive_failures if coordinator else 0,
        "reauthentications": coordinator.reauthentications if coordinator else 0,
        "timings": data.get("timings"),
        "browser": {
            k: v for k, v in (data.get("diagnostics") or {}).items() if k != "browser_endpoint"
        },
        "metric_keys": sorted(data.get("metrics", {})),
        "event_count": len(data.get("events", [])),
        "has_current_month": data.get("account", {}).get("current_month"),
        "balance_basis": data.get("account", {}).get("balance_basis"),
    }
