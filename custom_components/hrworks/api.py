"""Run the Nix-packaged browser process over an authenticated SSH channel."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from typing import Any

import asyncssh
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .const import (
    CONF_PROFILE,
    CONF_SSH_HOST,
    CONF_SSH_KEY_PATH,
    CONF_SSH_KNOWN_HOSTS,
    CONF_SSH_PORT,
    CONF_SSH_USERNAME,
    CONF_WORKER_COMMAND,
)


class WorkerError(Exception):
    """A fixed error code; never include remote output or employee data."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WorkerClient:
    """Keep one SSH process alive across login, MFA and subsequent updates."""

    def __init__(self, hass, settings: dict) -> None:
        self.hass = hass
        self.settings = dict(settings)
        self.profile = settings.get(CONF_PROFILE)
        self.connection = None
        self.process = None
        self.lock = asyncio.Lock()
        self._remove_stop = None

    async def close(self, _event=None) -> None:
        remove_stop = self._remove_stop
        self._remove_stop = None
        if remove_stop and _event is None:
            remove_stop()
        if self.process:
            self.process.close()
            self.process = None
        if self.connection:
            self.connection.close()
            try:
                async with asyncio.timeout(5):
                    await self.connection.wait_closed()
            except (TimeoutError, OSError, asyncssh.Error):
                pass
            self.connection = None

    async def _connect(self) -> None:
        if (
            self.connection
            and not self.connection.is_closed()
            and self.process
            and self.process.exit_status is None
        ):
            return
        await self.close()
        try:
            known_hosts = asyncssh.import_known_hosts(
                self.settings[CONF_SSH_KNOWN_HOSTS].strip() + "\n"
            )
        except (ValueError, asyncssh.Error) as err:
            raise WorkerError("ssh_host_key") from err
        try:
            self.connection = await asyncssh.connect(
                self.settings[CONF_SSH_HOST],
                port=int(self.settings.get(CONF_SSH_PORT, 22)),
                username=self.settings[CONF_SSH_USERNAME],
                client_keys=[self.settings[CONF_SSH_KEY_PATH]],
                known_hosts=known_hosts,
                agent_path=None,
                connect_timeout=15,
                keepalive_interval=30,
                keepalive_count_max=3,
            )
            # Quote the executable as one argument; never interpolate employee credentials.
            command = shlex.join(
                [self.settings.get(CONF_WORKER_COMMAND, "hrworks-worker"), "--stdio"]
            )
            self.process = await self.connection.create_process(command, stderr=asyncssh.DEVNULL)
            self._remove_stop = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self.close
            )
        except asyncssh.HostKeyNotVerifiable as err:
            await self.close()
            raise WorkerError("ssh_host_key") from err
        except (asyncssh.PermissionDenied, asyncssh.KeyImportError) as err:
            await self.close()
            raise WorkerError("worker_auth") from err
        except (asyncssh.Error, OSError, ValueError) as err:
            await self.close()
            raise WorkerError("cannot_connect") from err

    async def request(self, path: str, payload: dict | None = None) -> dict[str, Any]:
        """Use JSON lines over SSH. A submitted request is never replayed."""
        async with self.lock:
            try:
                async with asyncio.timeout(180):
                    await self._connect()
                    self.process.stdin.write(json.dumps({"path": path, "payload": payload}) + "\n")
                    await self.process.stdin.drain()
                    line = await self.process.stdout.readline()
                    if not line or len(line) > 2_000_000:
                        raise WorkerError("incompatible_worker")
                    try:
                        body = json.loads(line)
                    except (ValueError, TypeError) as err:
                        raise WorkerError("incompatible_worker") from err
                    if not isinstance(body, dict):
                        raise WorkerError("incompatible_worker")
                    if "error" in body:
                        allowed = {
                            "invalid_auth",
                            "mfa_required",
                            "invalid_code",
                            "unsupported_mfa",
                            "session_expired",
                            "portal_changed",
                            "browser_unavailable",
                            "writes_disabled",
                            "overlap",
                            "invalid_interval",
                            "write_uncertain",
                            "not_found",
                            "busy",
                            "invalid_request",
                        }
                        raise WorkerError(
                            body["error"] if body["error"] in allowed else "cannot_connect"
                        )
                    return body
            except (asyncssh.Error, OSError, TimeoutError) as err:
                await self.close()
                raise WorkerError("cannot_connect") from err
            except WorkerError as err:
                if err.code == "incompatible_worker":
                    await self.close()
                raise

    async def health(self) -> dict:
        result = await self.request("health")
        if result.get("protocol_version") != 1:
            raise WorkerError("incompatible_worker")
        return result

    async def login(
        self, company: str, username: str, password: str, *, force: bool = False
    ) -> dict:
        if force and "fresh_login" not in (await self.health()).get("capabilities", []):
            raise WorkerError("incompatible_worker")
        result = await self.request(
            "login",
            {"company_id": company, "username": username, "password": password, "force": force},
        )
        identity = result.get("profile_id")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise WorkerError("incompatible_worker")
        self.profile = identity
        return result

    async def mfa(self, code: str) -> dict:
        return await self.request(f"profiles/{self.profile}/mfa", {"code": code})

    async def snapshot(self, options: dict) -> dict:
        return await self.request(f"profiles/{self.profile}/snapshot", options)

    async def record(self, data: dict) -> dict:
        return await self.request(f"profiles/{self.profile}/record", data)
