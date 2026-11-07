"""Coordinated employee portal updates and actionable repair notices."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import WorkerError
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
from .hrworks.browser_daemon import TAB_GONE
from .totp import InvalidTotp
from .workdays import days_to_read, overview, prune

LOGGER = logging.getLogger(__name__)
TRANSIENT_ERRORS = {"browser_unavailable", "cannot_connect"}
TRANSIENT_RETRY_DELAYS = (3, 6, 10)
# After a rejected automatic login: seconds until the next attempt. Keeps retrying without
# hammering the account, so a temporary rejection heals itself.
AUTH_RETRY_BACKOFF = (900, 3600, 21600)
# Failed refreshes before an outage of browser-daemon is worth a repair.
OUTAGE_REPAIR_AFTER = 10
RETRY_INTERVAL = timedelta(seconds=60)
DAY_READS_TIMEOUT = 120
LOGIN_RETRY_DELAYS = (60, 300, 900, 1800)

ISSUES = (
    "session_expired",
    "worker_auth",
    "reconfigure_required",
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

    def __init__(self, hass, entry, client) -> None:
        self.entry = entry
        self.client = client
        self.last_error: str | None = None
        self.last_error_stage: str | None = None
        self.last_error_detail: str | None = None
        self.last_attempt: datetime | None = None
        self._auth_recovery_blocked = False
        self._days_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.days")
        self._days: dict | None = None
        self._snapshot_store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.snapshot")
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
    def _auth_recovery_blocked(self) -> bool:
        """Whether automatic logins wait, after a rejected one."""
        return time.monotonic() < getattr(self, "_auth_blocked_until", 0.0)

    @_auth_recovery_blocked.setter
    def _auth_recovery_blocked(self, blocked: bool) -> None:
        if not blocked:
            self._auth_blocked_until = 0.0
            self._auth_block_count = 0
            return
        count = getattr(self, "_auth_block_count", 0)
        delay = AUTH_RETRY_BACKOFF[min(count, len(AUTH_RETRY_BACKOFF) - 1)]
        self._auth_blocked_until = time.monotonic() + delay
        self._auth_block_count = count + 1

    @property
    def next_update(self) -> datetime | None:
        if self.last_finished is None or self.update_interval is None:
            return None
        return self.last_finished + self.update_interval

    async def async_restore_snapshot(self) -> bool:
        """Show the last known snapshot until the first refresh completes (e.g. after a restart)."""
        try:
            saved = await self._snapshot_store.async_load()
        except Exception as err:  # corrupt storage must never block setup
            LOGGER.warning("HR WORKS could not read the saved snapshot: %r", err)
            return False
        if not isinstance(saved, dict) or not saved.get("today"):
            return False
        self.data = saved
        return True

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
            # Do not keep the browser open between polls: "remember me" (or the saved TOTP)
            # restores the login on the next one.
            await self.client.end_session()
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
                    # Backoff gives a restarting browser daemon time to return. Only a stuck
                    # browser is stopped, not a healthy one the daemon could not reach or place.
                    if err.code == "browser_unavailable" and err.stage == "browser_operation":
                        await self._async_recover_browser_session()
                    await asyncio.sleep(delay)
        except WorkerError as err:
            self.last_error = err.code
            self.last_error_stage = err.stage or (
                "worker_transport"
                if err.code in {"cannot_connect", "worker_auth"}
                else "worker_response"
            )
            self.last_error_detail = err.detail
            self.update_interval = (
                min(self._normal_update_interval, RETRY_INTERVAL)
                if err.code in TRANSIENT_ERRORS
                else self._normal_update_interval
            )
            if err.code in {
                "session_expired",
                "mfa_required",
                "invalid_auth",
                "invalid_code",
                "login_timeout",
                "rbw_unavailable",
                "rbw_incomplete",
                "different_account",
            }:
                LOGGER.warning(
                    "HR WORKS refresh still requires authentication (error=%s); raising repair",
                    err.code,
                )
                create_issue(self.hass, self.entry, "session_expired")
                raise UpdateFailed(err.code) from err
            key = (
                "portal_changed"
                if err.code == "portal_changed"
                else (err.code if err.code == "worker_auth" else "worker_unavailable")
            )
            if key == "worker_unavailable" and err.code in TRANSIENT_ERRORS:
                # An outage heals itself once browser-daemon is back: only a long one needs
                # attention. consecutive_failures does not count this failure yet.
                if self.consecutive_failures + 1 >= OUTAGE_REPAIR_AFTER:
                    create_issue(self.hass, self.entry, key)
            else:
                create_issue(self.hass, self.entry, key)
            raise UpdateFailed(err.code) from err
        self.last_error = None
        self.last_error_stage = None
        self.last_error_detail = None
        self._auth_recovery_blocked = False
        self.consecutive_failures = 0
        self.update_interval = self._normal_update_interval
        clear_issues(self.hass, self.entry)
        data["work_days"] = await self._work_days(data)
        if store := getattr(self, "_snapshot_store", None):
            store.async_delay_save(lambda: data, 1)
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
        """Recover an expired login, retrying only timed-out portal logins."""
        try:
            try:
                return await self.client.snapshot(payload)
            except WorkerError as err:
                if err.detail != TAB_GONE:
                    raise
                # The browser went away, for example with a restart of browser-daemon. The
                # profile's cookies usually still hold the login: no need for a new one.
                LOGGER.debug("HR WORKS browser tab is gone; retrying in a new one")
                return await self.client.snapshot(payload)
        except WorkerError as err:
            if err.code not in {"session_expired", "mfa_required"}:
                raise
            if self._auth_recovery_blocked:
                LOGGER.debug("HR WORKS automatic login retries exhausted; waiting for Repairs")
                raise
            if not self.entry.data.get(CONF_TOTP_URI) and not self.entry.data.get("rbw_entry_id"):
                self._auth_recovery_blocked = True
                LOGGER.warning(
                    "HR WORKS session requires authentication (error=%s); "
                    "automatic recovery skipped because neither TOTP nor rbw is configured",
                    err.code,
                )
                raise
            LOGGER.debug(
                "HR WORKS session requires authentication (error=%s); "
                "trying automatic recovery with the configured credential source",
                err.code,
            )
        for attempt in range(len(LOGIN_RETRY_DELAYS) + 1):
            try:
                await authenticate(self.client, self.entry.data, force=True, hass=self.hass)
                break
            except InvalidTotp:
                self._auth_recovery_blocked = True
                LOGGER.warning("HR WORKS automatic recovery could not parse the saved TOTP URI")
                raise WorkerError("invalid_auth") from None
            except WorkerError as err:
                if err.code != "login_timeout" or attempt == len(LOGIN_RETRY_DELAYS):
                    if err.code in {
                        "invalid_auth",
                        "invalid_code",
                        "mfa_required",
                        "unsupported_mfa",
                        "session_expired",
                        "login_timeout",
                        "rbw_unavailable",
                        "rbw_incomplete",
                        "different_account",
                    }:
                        self._auth_recovery_blocked = True
                    stage = (
                        "MFA verification"
                        if err.code in {"invalid_code", "mfa_required", "unsupported_mfa"}
                        else "employee login"
                    )
                    LOGGER.warning(
                        "HR WORKS automatic recovery failed during %s (error=%s)",
                        stage,
                        err.code,
                    )
                    raise
                delay = LOGIN_RETRY_DELAYS[attempt]
                LOGGER.warning(
                    "HR WORKS login response timed out; retrying in %ss (attempt %s/%s)",
                    delay,
                    attempt + 2,
                    len(LOGIN_RETRY_DELAYS) + 1,
                )
                await asyncio.sleep(delay)
        self.reauthentications += 1
        try:
            return await self.client.snapshot(payload)
        except WorkerError as err:
            if err.code in {"session_expired", "mfa_required", "invalid_auth", "invalid_code"}:
                self._auth_recovery_blocked = True
            LOGGER.warning(
                "HR WORKS automatic recovery completed but data refresh failed (error=%s)",
                err.code,
            )
            raise

    async def _async_recover_browser_session(self) -> bool:
        """Recover from browser_unavailable by terminating hung hrworks sessions in browser_daemon."""
        try:
            session = await self.client.stop_profile_browser()
            if session:
                LOGGER.info(
                    "Terminated hung hrworks session %s via browser-daemon to unblock worker",
                    session,
                )
            return bool(session)
        except Exception as err:
            LOGGER.debug("Could not recover hrworks browser session via browser-daemon: %s", err)
            return False
