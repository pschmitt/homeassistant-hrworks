"""Parts of the setup dialog that HR WORKS and Teams share.

Both repositories carry the same file, so both dialogs keep the same shape: the
integration's own fields, then a Browser Daemon section, then a credentials section.
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
SECTION_CREDENTIALS = "credentials"

USE_BROWSER_DAEMON = "use_browser_daemon"
BROWSER_DAEMON_ENTRY_ID = "browser_daemon_entry_id"
BROWSER_DAEMON_OVERRIDE = "browser_daemon_override"
BROWSER_DAEMON_URL = "browser_daemon_url"
BROWSER_DAEMON_SECRET = "browser_daemon_secret"
BROWSER_DAEMON_PROFILE = "browser_daemon_profile"
BROWSER_DAEMON_NODE_GROUP = "browser_daemon_node_group"
RBW_ENTRY_ID = "rbw_entry_id"
RBW_ITEM_ID = "rbw_item_id"


def section(fields: dict[Any, Any], *, collapsed: bool = False) -> data_entry_flow.section:
    """A titled group of fields. Conditions inside it only see the same section."""
    return data_entry_flow.section(vol.Schema(fields), {"collapsed": collapsed})


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
    hass: Any,
    defaults: Mapping[str, Any],
    *,
    profile: str,
    use_default: bool | None = None,
) -> dict[Any, Any]:
    """Which Browser Daemon integration to use, or an override, and where to run.

    With ``use_default``, a checkbox turns the rest on or off. The cluster secret is
    never prefilled.
    """
    use = [{"field": USE_BROWSER_DAEMON, "value": True}] if use_default is not None else []
    saved = defaults.get(BROWSER_DAEMON_ENTRY_ID) or None
    daemons = [
        entry.entry_id
        for entry in hass.config_entries.async_entries("browser_daemon")
        if not entry.disabled_by
    ]
    available = bool(saved or daemons)
    override = [*use, {"field": BROWSER_DAEMON_OVERRIDE, "value": True}]
    picker_default = {"default": saved or daemons[0]} if available else {}
    # Without an integration the picker stays visible and grey, next to the override.
    picker_visible = (
        [*use, {"field": BROWSER_DAEMON_OVERRIDE, "operator": "not_eq", "value": True}]
        if available
        else use
    )
    fields: dict[Any, Any] = {}
    if use_default is not None:
        fields[vol.Optional(USE_BROWSER_DAEMON, default=use_default)] = BooleanSelector()

    def when(conditions: list[dict[str, Any]]) -> dict[str, Any]:
        return {"visible": conditions} if conditions else {}

    fields.update(
        {
            data_entry_flow.Optional(
                BROWSER_DAEMON_ENTRY_ID, **picker_default, **when(picker_visible)
            ): integration_picker("browser_daemon", available=available),
            data_entry_flow.Optional(
                BROWSER_DAEMON_OVERRIDE,
                default=defaults.get(
                    BROWSER_DAEMON_OVERRIDE, bool(defaults.get(BROWSER_DAEMON_URL))
                )
                or not available,
                **when(use),
            ): BooleanSelector(),
            data_entry_flow.Optional(
                BROWSER_DAEMON_URL,
                default=defaults.get(BROWSER_DAEMON_URL) or "",
                visible=override,
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            data_entry_flow.Optional(BROWSER_DAEMON_SECRET, visible=override): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            data_entry_flow.Optional(
                BROWSER_DAEMON_PROFILE,
                default=defaults.get(BROWSER_DAEMON_PROFILE, profile),
                **when(use),
            ): TextSelector(),
            data_entry_flow.Optional(
                BROWSER_DAEMON_NODE_GROUP,
                default=defaults.get(BROWSER_DAEMON_NODE_GROUP, ""),
                **when(use),
            ): TextSelector(),
        }
    )
    return fields


def rbw_field(hass: Any, defaults: Mapping[str, Any]) -> dict[Any, Any]:
    """The rbw integration to read credentials from, greyed out while there is none."""
    saved = defaults.get(RBW_ENTRY_ID) or None
    available = bool(saved or hass.config_entries.async_entries("rbw"))
    # A suggested value, not a default: clearing the picker must turn rbw off.
    return {
        vol.Optional(RBW_ENTRY_ID, description={"suggested_value": saved}): integration_picker(
            "rbw", available=available
        )
    }


def clear_rbw_when_empty(data: dict[str, Any]) -> dict[str, Any]:
    """An empty picker means no rbw, and discards any saved item."""
    if not data.get(RBW_ENTRY_ID):
        return {**data, RBW_ENTRY_ID: "", RBW_ITEM_ID: ""}
    return data


def needs_rbw_item(data: Mapping[str, Any]) -> bool:
    """An rbw integration is chosen, and its item is asked for in the next step."""
    return bool(data.get(RBW_ENTRY_ID)) and RBW_ITEM_ID not in data


async def rbw_item_options(hass: Any, entry_id: str, saved: str | None) -> list[SelectOptionDict]:
    """The items rbw lets this integration read, by name, and the saved item.

    rbw lists nothing for a new integration, and the form then takes a typed ID,
    which rbw offers to allow through a repair.
    """
    try:
        from custom_components.rbw import async_list_items

        items = await async_list_items(hass, entry_id)
    except (ImportError, HomeAssistantError):
        items = []
    # The name is missing while the vault is locked.
    names = {item["id"]: item.get("name") or item["id"] for item in items}
    if saved:
        names.setdefault(saved, saved)
    return [
        SelectOptionDict(value=item_id, label=name)
        for item_id, name in sorted(names.items(), key=lambda pair: pair[1].casefold())
    ]


def rbw_item_schema(options: list[SelectOptionDict], saved: str | None) -> vol.Schema:
    """Pick an item by name, or type an ID that is not listed."""
    default = saved or (options[0]["value"] if len(options) == 1 else None)
    return vol.Schema(
        {
            (
                vol.Required(RBW_ITEM_ID, default=default) if default else vol.Required(RBW_ITEM_ID)
            ): (
                SelectSelector(
                    SelectSelectorConfig(
                        options=options, custom_value=True, mode=SelectSelectorMode.DROPDOWN
                    )
                )
            )
        }
    )


def saved_rbw_item(saved: Mapping[str, Any], entry_id: str) -> str | None:
    """The saved item, as long as it belongs to this rbw integration."""
    return saved.get(RBW_ITEM_ID) if saved.get(RBW_ENTRY_ID) == entry_id else None
