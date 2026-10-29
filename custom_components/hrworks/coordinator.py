"""Coordinated employee portal updates and actionable repair notices."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

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
from .workdays import days_to_read, overview, prune

LOGGER = logging.getLogger(__name__)
TRANSIENT_ERRORS = {"browser_unavailable", "cannot_connect"}
TRANSIENT_RETRY_DELAYS = (3, 6, 10)
AUTO_RELOAD_COOLDOWN = 300
RETRY_INTERVAL = timedelta(seconds=60)
DAY_READS_TIMEOUT = 120

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
        is_fixable=key in {"session_expired", "write_uncertain", "worker_unavailable"},
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

    last_duration: float | None = None
    last_finished: datetime | None = None
    update_count: int = 0
    failed_updates: int = 0
    consecutive_failures: int = 0
    reauthentications: int = 0

    def __init__(self, hass, entry, client: WorkerClient) -> None:
        self.entry = entry
        self.client = client
        self.last_error: str | None = None
        self.last_error_stage: str | None = None
        self.last_error_detail: str | None = None
        self.last_attempt: datetime | None = None
        self._days_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.days")
        self._days: dict | None = None
        # Operational counters for the diagnostic sensors (kept while HA runs).
        self.last_duration: float | None = None
        self.last_finished: datetime | None = None
        self.update_count = 0
        self.failed_updates = 0
        self.consecutive_failures = 0
        self.reauthentications = 0
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

    @property
    def next_update(self) -> datetime | None:
        if self.last_finished is None or self.update_interval is None:
            return None
        return self.last_finished + self.update_interval

    async def _async_update_data(self) -> dict:
        started = time.monotonic()
        self.last_attempt = dt_util.utcnow()
        self.update_count += 1
        try:
            return await self._update()
        except BaseException:
            self.failed_updates += 1
            self.consecutive_failures += 1
            raise
        finally:
            self.last_duration = round(time.monotonic() - started, 2)
            self.last_finished = dt_util.utcnow()

    async def _update(self) -> dict:
        options = self.entry.options
        try:
            payload = {
                CONF_PAST_DAYS: int(options.get(CONF_PAST_DAYS, DEFAULT_PAST_DAYS)),
                CONF_FUTURE_DAYS: int(options.get(CONF_FUTURE_DAYS, DEFAULT_FUTURE_DAYS)),
                CONF_SICKNESS: options.get(CONF_SICKNESS, True),
                CONF_PENDING: options.get(CONF_PENDING, False),
            }
            for attempt in range(len(TRANSIENT_RETRY_DELAYS) + 1):
                try:
                    data = await self._snapshot_with_auth(payload)
                    break
                except WorkerError as err:
                    if err.code not in TRANSIENT_ERRORS or attempt == len(TRANSIENT_RETRY_DELAYS):
                        raise
                    delay = TRANSIENT_RETRY_DELAYS[attempt]
                    LOGGER.warning(
                        "HR WORKS read failed transiently (error=%s); retrying in %ss "
                        "(attempt %s/%s)",
                        err.code,
                        delay,
                        attempt + 2,
                        len(TRANSIENT_RETRY_DELAYS) + 1,
                    )
                    # The transport discarded the failed worker under its request
                    # lock. Backoff gives a restarting browser daemon time to return.
                    await asyncio.sleep(delay)
        except WorkerError as err:
            self.last_error = err.code
            self.last_error_stage = err.stage or (
                "worker_transport"
                if err.code in {"cannot_connect", "worker_auth", "ssh_host_key"}
                else "worker_response"
            )
            self.last_error_detail = err.detail
            self.update_interval = (
                min(self._normal_update_interval, RETRY_INTERVAL)
                if err.code in TRANSIENT_ERRORS
                else self._normal_update_interval
            )
            if err.code in {"session_expired", "mfa_required", "invalid_auth", "invalid_code"}:
                LOGGER.warning(
                    "HR WORKS refresh still requires authentication (error=%s); raising repair",
                    err.code,
                )
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
            if key == "worker_unavailable" and err.code in TRANSIENT_ERRORS:
                domain_data = getattr(self.hass, "data", None)
                reloads = (
                    domain_data.setdefault(DOMAIN, {})
                    if isinstance(domain_data, dict)
                    else getattr(self, "_reloads", {})
                )
                self._reloads = reloads
                entry_id = getattr(self.entry, "entry_id", "default")
                reload_key = f"{entry_id}_last_auto_reload"
                last_reload = reloads.get(reload_key)
                now = time.monotonic()
                if last_reload is None or (now - last_reload > AUTO_RELOAD_COOLDOWN):
                    reloads[reload_key] = now
                    LOGGER.warning(
                        "HR WORKS worker unavailable (%s); scheduling automatic integration reload to recover",
                        err.code,
                    )
                    self.hass.config_entries.async_schedule_reload(entry_id)
                else:
                    create_issue(self.hass, self.entry, key)
            else:
                create_issue(self.hass, self.entry, key)
            raise UpdateFailed(err.code) from err
        self.last_error = None
        self.last_error_stage = None
        self.last_error_detail = None
        self.consecutive_failures = 0
        self.update_interval = self._normal_update_interval
        entry_id = getattr(self.entry, "entry_id", "default")
        domain_data = getattr(self.hass, "data", None)
        if isinstance(domain_data, dict) and DOMAIN in domain_data:
            domain_data[DOMAIN].pop(f"{entry_id}_last_auto_reload", None)
        if hasattr(self, "_reloads"):
            self._reloads.pop(f"{entry_id}_last_auto_reload", None)
        clear_issues(self.hass, self.entry)
        data["work_days"] = await self._work_days(data)
        return data

    async def _work_days(self, data: dict) -> list[dict]:
        """Read a few past days per update; a failed day read never fails the update."""
        if self._days is None:
            self._days = (await self._days_store.async_load()) or {}
        today = date.fromisoformat(data["today"]["date"])
        events = data.get("events", [])
        try:
            async with asyncio.timeout(DAY_READS_TIMEOUT):
                for index, day in enumerate(days_to_read(today, self._days, events)):
                    result = await self.client.day(day.isoformat(), reuse=index > 0)
                    metrics = result.get("metrics", {})
                    self._days[day.isoformat()] = {
                        "worked": metrics.get("today_worked"),
                        "target": metrics.get("today_target"),
                        "fetched": today.isoformat(),
                    }
        except (TimeoutError, WorkerError) as err:
            LOGGER.debug("HR WORKS day history read stopped early: %r", err)
        self._days = prune(today, self._days)
        await self._days_store.async_save(self._days)
        return overview(today, self._days, events, data.get("metrics", {}))

    async def _snapshot_with_auth(self, payload: dict) -> dict:
        """Recover an expired login once, including after worker replacement."""
        try:
            return await self.client.snapshot(payload)
        except WorkerError as err:
            if err.code not in {"session_expired", "mfa_required"}:
                raise
            if not self.entry.data.get(CONF_TOTP_URI):
                LOGGER.warning(
                    "HR WORKS session requires authentication (error=%s); "
                    "automatic recovery skipped because no TOTP URI is saved",
                    err.code,
                )
                raise
            LOGGER.debug(
                "HR WORKS session requires authentication (error=%s); "
                "trying automatic recovery with saved TOTP",
                err.code,
            )
        try:
            await authenticate(self.client, self.entry.data, force=True)
        except InvalidTotp:
            LOGGER.warning("HR WORKS automatic recovery could not parse the saved TOTP URI")
            raise WorkerError("invalid_auth") from None
        except WorkerError as err:
            stage = (
                "MFA verification"
                if err.code in {"invalid_code", "mfa_required", "unsupported_mfa"}
                else "employee login"
            )
            LOGGER.warning(
                "HR WORKS automatic recovery failed during %s (error=%s)", stage, err.code
            )
            raise
        self.reauthentications += 1
        try:
            return await self.client.snapshot(payload)
        except WorkerError as err:
            LOGGER.warning(
                "HR WORKS automatic recovery completed but data refresh failed (error=%s)",
                err.code,
            )
            raise
