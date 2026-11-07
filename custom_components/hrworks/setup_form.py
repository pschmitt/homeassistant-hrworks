"""Parts of the setup dialog that HR WORKS and Teams share.

Both repositories carry the same file, so both dialogs keep the same shape: the
integration's own fields, then a Browser Daemon section, then a choice of credentials
which shows either the rbw section or the fields for manual credentials.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import probatio as vol
from homeassistant import data_entry_flow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    ConfigEntrySelector,
    ConfigEntrySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

SECTION_BROWSER_DAEMON = "browser_daemon"
SECTION_RBW = "rbw"

CREDENTIALS_PROVIDER = "credentials_provider"
PROVIDER_RBW = "rbw"
PROVIDER_MANUAL = "manual"

USE_BROWSER_DAEMON = "use_browser_daemon"
BROWSER_DAEMON_ENTRY_ID = "browser_daemon_entry_id"
BROWSER_DAEMON_OVERRIDE = "browser_daemon_override"
BROWSER_DAEMON_URL = "browser_daemon_url"
BROWSER_DAEMON_SECRET = "browser_daemon_secret"
BROWSER_DAEMON_PROFILE = "browser_daemon_profile"
BROWSER_DAEMON_NODE_GROUP = "browser_daemon_node_group"
RBW_ENTRY_ID = "rbw_entry_id"
RBW_ITEM_ID = "rbw_item_id"


def section(
    name: str,
    fields: dict[Any, Any],
    *,
    visible: dict[str, Any] | list[dict[str, Any]] | None = None,
) -> dict[Any, Any]:
    """A titled group of fields, for inside a schema.

    Conditions inside a section only see that section, and a section's own condition
    only sees the fields outside any section.
    """
    return {
        data_entry_flow.Required(name, visible=visible): data_entry_flow.section(
            vol.Schema(fields), {"collapsed": False}
        )
    }


def flatten_sections(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """The submitted values without their sections."""
    flat: dict[str, Any] = {}
    for key, value in user_input.items():
        if isinstance(value, Mapping):
            flat.update(value)
        else:
            flat[key] = value
    return flat


def integration_picker(domain: str, *, available: bool = True) -> ConfigEntrySelector:
    """Pick one of an integration's entries, shown with its name and icon.

    It is greyed out while that integration has no entry.
    """
    config = ConfigEntrySelectorConfig(integration=domain)
    if not available:
        config["read_only"] = True
    return ConfigEntrySelector(config)


def browser_daemon_fields(
    hass: Any, defaults: Mapping[str, Any], *, profile: str
) -> dict[Any, Any]:
    """Which Browser Daemon integration to use, or an override, and where to run.

    The cluster secret is never prefilled.
    """
    saved = defaults.get(BROWSER_DAEMON_ENTRY_ID) or None
    daemons = [
        entry.entry_id
        for entry in hass.config_entries.async_entries("browser_daemon")
        if not entry.disabled_by
    ]
    available = bool(saved or daemons)
    override = {"field": BROWSER_DAEMON_OVERRIDE, "value": True}
    picker = (
        data_entry_flow.Optional(
            BROWSER_DAEMON_ENTRY_ID,
            default=saved or daemons[0],
            visible={"field": BROWSER_DAEMON_OVERRIDE, "operator": "not_eq", "value": True},
        )
        if available
        # Without an integration the picker stays visible and grey, next to the override.
        else vol.Optional(BROWSER_DAEMON_ENTRY_ID)
    )
    return {
        picker: integration_picker("browser_daemon", available=available),
        vol.Optional(
            BROWSER_DAEMON_OVERRIDE,
            default=defaults.get(BROWSER_DAEMON_OVERRIDE, bool(defaults.get(BROWSER_DAEMON_URL)))
            or not available,
        ): BooleanSelector(),
        data_entry_flow.Optional(
            BROWSER_DAEMON_URL,
            default=defaults.get(BROWSER_DAEMON_URL) or "",
            visible=override,
        ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
        data_entry_flow.Optional(BROWSER_DAEMON_SECRET, visible=override): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(
            BROWSER_DAEMON_PROFILE, default=defaults.get(BROWSER_DAEMON_PROFILE, profile)
        ): TextSelector(),
        vol.Optional(
            BROWSER_DAEMON_NODE_GROUP, default=defaults.get(BROWSER_DAEMON_NODE_GROUP, "")
        ): TextSelector(),
    }


def choice(options: list[str], translation_key: str) -> SelectSelector:
    """A dropdown of labelled options, like the integration pickers next to it."""
    return SelectSelector(
        SelectSelectorConfig(
            options=options, translation_key=translation_key, mode=SelectSelectorMode.DROPDOWN
        )
    )


def credentials_provider_field(
    hass: Any,
    defaults: Mapping[str, Any],
    *,
    manual_saved: bool,
    visible: dict[str, Any] | None = None,
) -> dict[Any, Any]:
    """rbw or manual credentials, which decides the section that shows.

    It stays outside any section, because a section's condition only sees such fields.
    A new setup starts with rbw if it is there.
    """
    default = defaults.get(CREDENTIALS_PROVIDER)
    if not default:
        rbw = defaults.get(RBW_ENTRY_ID) or (
            not manual_saved and hass.config_entries.async_entries("rbw")
        )
        default = PROVIDER_RBW if rbw else PROVIDER_MANUAL
    return {
        data_entry_flow.Required(CREDENTIALS_PROVIDER, default=default, visible=visible): choice(
            [PROVIDER_RBW, PROVIDER_MANUAL], CREDENTIALS_PROVIDER
        )
    }


def rbw_fields(
    hass: Any, defaults: Mapping[str, Any], items: list[SelectOptionDict]
) -> dict[Any, Any]:
    """The rbw integration to read credentials from and its item.

    The picker is greyed out while rbw has no entry. An item can be typed if it is not
    listed, and rbw then offers to allow it through a repair.
    """
    saved = defaults.get(RBW_ENTRY_ID) or None
    entries = hass.config_entries.async_entries("rbw")
    available = bool(saved or entries)
    item = defaults.get(RBW_ITEM_ID) or (items[0]["value"] if len(items) == 1 else None)
    return {
        vol.Optional(
            RBW_ENTRY_ID, **({"default": saved or entries[0].entry_id} if available else {})
        ): integration_picker("rbw", available=available),
        vol.Optional(RBW_ITEM_ID, description={"suggested_value": item}): SelectSelector(
            SelectSelectorConfig(options=items, custom_value=True, mode=SelectSelectorMode.DROPDOWN)
        ),
    }


def resolve_credentials(data: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Turn the choice into what is saved, and say what is missing for it."""
    resolved = {key: value for key, value in data.items() if key != CREDENTIALS_PROVIDER}
    provider = data.get(CREDENTIALS_PROVIDER)
    if provider == PROVIDER_MANUAL:
        return {**resolved, RBW_ENTRY_ID: "", RBW_ITEM_ID: ""}, None
    resolved = clear_rbw_when_empty(resolved)
    if provider == PROVIDER_RBW and not (resolved[RBW_ENTRY_ID] and resolved[RBW_ITEM_ID]):
        return resolved, "rbw_incomplete"
    return resolved, None


def clear_rbw_when_empty(data: dict[str, Any]) -> dict[str, Any]:
    """An empty picker means no rbw and discards any saved item.

    A submitted form has no item when it was left blank, which must not bring back the
    saved one.
    """
    if not data.get(RBW_ENTRY_ID):
        return {**data, RBW_ENTRY_ID: "", RBW_ITEM_ID: ""}
    return {RBW_ITEM_ID: "", **data}


async def rbw_item_options(hass: Any, saved: Mapping[str, Any]) -> list[SelectOptionDict]:
    """The items rbw lets this integration read, by name, and the saved item.

    rbw lists nothing for a new integration. With several rbw integrations, the label
    says which one an item comes from.
    """
    try:
        from custom_components.rbw import async_list_items
    except ImportError:
        return _with_saved_item({}, saved)
    entries = hass.config_entries.async_entries("rbw")
    labels: dict[str, str] = {}
    # One after the other: rbw recognises its caller by the call stack, which a task loses.
    for entry in entries:
        try:
            items = await async_list_items(hass, entry.entry_id)
        except HomeAssistantError:
            continue
        for item in items:
            # The name is missing while the vault is locked.
            name = item.get("name") or item["id"]
            labels[item["id"]] = f"{name} ({entry.title})" if len(entries) > 1 else name
    return _with_saved_item(labels, saved)


def _with_saved_item(labels: dict[str, str], saved: Mapping[str, Any]) -> list[SelectOptionDict]:
    if item := saved.get(RBW_ITEM_ID):
        labels.setdefault(item, item)
    return [
        SelectOptionDict(value=value, label=label)
        for value, label in sorted(labels.items(), key=lambda pair: pair[1].casefold())
    ]
