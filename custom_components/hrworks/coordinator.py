"""Coordinated employee portal updates and actionable repair notices."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WorkerClient, WorkerError
from .auth import authenticate
from .const import (
    CONF_FUTURE_DAYS,
    CONF_PAST_DAYS,
    CONF_PENDING,
    CONF_SICKNESS,
    CONF_TOTP_URI,
    DEFAULT_FUTURE_DAYS,
    DEFAULT_PAST_DAYS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .totp import InvalidTotp

LOGGER = logging.getLogger(__name__)
TRANSIENT_ERRORS = {"browser_unavailable", "cannot_connect"}
RETRY_INTERVAL = timedelta(seconds=60)

ISSUES = (
    "session_expired",
    "worker_auth",
    "ssh_host_key",
    "worker_unavailable",
    "portal_changed",
    "write_uncertain",
)


def create_issue(hass, entry, key: str) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{entry.entry_id}_{key}",
        is_fixable=key in {"session_expired", "write_uncertain"},
        severity=ir.IssueSeverity.WARNING,
        translation_key=key,
        translation_placeholders={"name": entry.title},
        data={"entry_id": entry.entry_id},
    )


def clear_issues(hass, entry, *, include_writes: bool = False) -> None:
    for key in ISSUES:
        if key != "write_uncertain" or include_writes:
            ir.async_delete_issue(hass, DOMAIN, f"{entry.entry_id}_{key}")


class HrworksCoordinator(DataUpdateCoordinator[dict]):
    """One snapshot updates all sensors and calendars."""

    def __init__(self, hass, entry, client: WorkerClient) -> None:
        self.entry = entry
        self.client = client
        self.last_error: str | None = None
        self._normal_update_interval = timedelta(
            seconds=int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
        )
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=self._normal_update_interval,
        )

    async def _async_update_data(self) -> dict:
        options = self.entry.options
        try:
            payload = {
                CONF_PAST_DAYS: int(options.get(CONF_PAST_DAYS, DEFAULT_PAST_DAYS)),
                CONF_FUTURE_DAYS: int(options.get(CONF_FUTURE_DAYS, DEFAULT_FUTURE_DAYS)),
                CONF_SICKNESS: options.get(CONF_SICKNESS, True),
                CONF_PENDING: options.get(CONF_PENDING, False),
            }
            try:
                data = await self._snapshot_with_auth(payload)
            except WorkerError as err:
                if err.code not in TRANSIENT_ERRORS:
                    raise
                # The transport discarded the failed worker under its request lock.
                # Only reads get one immediate attempt against a fresh worker.
                data = await self._snapshot_with_auth(payload)
        except WorkerError as err:
            self.last_error = err.code
            self.update_interval = (
                min(self._normal_update_interval, RETRY_INTERVAL)
                if err.code in TRANSIENT_ERRORS
                else self._normal_update_interval
            )
            if err.code in {"session_expired", "mfa_required", "invalid_auth", "invalid_code"}:
                create_issue(self.hass, self.entry, "session_expired")
                raise ConfigEntryAuthFailed("Employee session needs authentication") from err
            key = (
                "portal_changed"
                if err.code == "portal_changed"
                else (
                    err.code
                    if err.code in {"worker_auth", "ssh_host_key"}
                    else "worker_unavailable"
                )
            )
            create_issue(self.hass, self.entry, key)
            raise UpdateFailed(err.code) from err
        self.last_error = None
        self.update_interval = self._normal_update_interval
        clear_issues(self.hass, self.entry)
        return data

    async def _snapshot_with_auth(self, payload: dict) -> dict:
        """Recover an expired login once, including after worker replacement."""
        try:
            return await self.client.snapshot(payload)
        except WorkerError as err:
            if err.code not in {"session_expired", "mfa_required"} or not self.entry.data.get(
                CONF_TOTP_URI
            ):
                raise
        try:
            await authenticate(self.client, self.entry.data, force=True)
        except InvalidTotp:
            raise WorkerError("invalid_auth") from None
        return await self.client.snapshot(payload)
