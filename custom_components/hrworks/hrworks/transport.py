"""Bounded JSON-line transports. Never replay requests or expose remote errors."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from abc import ABC, abstractmethod
from pathlib import Path

import asyncssh

ERRORS = {
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


class WorkerError(Exception):
    """Fixed error codes only; never employee data, stderr, or credentials."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Transport(ABC):
    def __init__(self, *, timeout: float = 180):
        self.timeout = timeout
        self.lock = asyncio.Lock()
        self.submitted = False

    @abstractmethod
    async def exchange(self, message: str) -> str: ...

    @abstractmethod
    async def close(self): ...

    async def request(self, path: str, payload: dict | None = None) -> dict:
        async with self.lock:
            self.submitted = False
            write = (
                path.endswith("/record")
                and payload is not None
                and payload.get("dry_run", True) is False
            )
            try:
                async with asyncio.timeout(self.timeout):
                    line = await self.exchange(
                        json.dumps({"path": path, "payload": payload}) + "\n"
                    )
                    if not line or len(line) > 2_000_000:
                        raise WorkerError("incompatible_worker")
                    try:
                        body = json.loads(line)
                    except (ValueError, TypeError):
                        raise WorkerError("incompatible_worker") from None
                    if not isinstance(body, dict):
                        raise WorkerError("incompatible_worker")
                    if "error" in body:
                        code = body["error"]
                        raise WorkerError(
                            code if isinstance(code, str) and code in ERRORS else "cannot_connect"
                        )
                    return body
            except (asyncssh.Error, OSError, TimeoutError, UnicodeError):
                await self.close()
                raise WorkerError(
                    "write_uncertain" if write and self.submitted else "cannot_connect"
                ) from None
            except WorkerError as err:
                if err.code == "incompatible_worker":
                    await self.close()
                    if write and self.submitted:
                        raise WorkerError("write_uncertain") from None
                raise
            except asyncio.CancelledError:
                await self.close()
                raise


class SshTransport(Transport):
    """Strict host verification and SSH keys; no shell credential interpolation."""

    def __init__(self, settings: dict, **kwargs):
        super().__init__(**kwargs)
        self.settings = dict(settings)
        if self.settings.get("ssh_key_path"):
            self.settings["ssh_key_path"] = os.path.expanduser(self.settings["ssh_key_path"])
        self.connection = self.process = None

    async def connect(self):
        if (
            self.connection
            and not self.connection.is_closed()
            and self.process
            and self.process.exit_status is None
        ):
            return
        await self.close()
        try:
            hosts = self.settings.get("ssh_known_hosts")
            known_hosts = (
                asyncssh.import_known_hosts(hosts.strip() + "\n")
                if hosts
                else str(Path.home() / ".ssh/known_hosts")
            )
        except (ValueError, asyncssh.Error):
            raise WorkerError("ssh_host_key") from None
        try:
            key = self.settings.get("ssh_key_path")
            self.connection = await asyncssh.connect(
                self.settings["ssh_host"],
                port=int(self.settings.get("ssh_port", 22)),
                username=self.settings.get("ssh_username"),
                client_keys=[key] if key else None,
                known_hosts=known_hosts,
                agent_path=None if key else (),
                connect_timeout=15,
                keepalive_interval=30,
                keepalive_count_max=3,
            )
            command = shlex.join([self.settings.get("worker_command", "hrworks-worker"), "--stdio"])
            self.process = await self.connection.create_process(command, stderr=asyncssh.DEVNULL)
        except asyncssh.HostKeyNotVerifiable:
            await self.close()
            raise WorkerError("ssh_host_key") from None
        except (asyncssh.PermissionDenied, asyncssh.KeyImportError):
            await self.close()
            raise WorkerError("worker_auth") from None
        except (asyncssh.Error, OSError, ValueError, KeyError):
            await self.close()
            raise WorkerError("cannot_connect") from None

    async def exchange(self, message: str) -> str:
        await self.connect()
        self.submitted = True
        self.process.stdin.write(message)
        await self.process.stdin.drain()
        return await self.process.stdout.readline()

    async def close(self):
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


class LocalTransport(Transport):
    """Launch an installed worker locally. stderr is intentionally discarded."""

    def __init__(self, command: list[str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.command = command or ["hrworks-worker", "--stdio"]
        self.process = None

    async def exchange(self, message: str) -> str:
        if self.process is None or self.process.returncode is not None:
            await self.close()
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=2_000_001,
            )
        self.submitted = True
        self.process.stdin.write(message.encode())
        await self.process.stdin.drain()
        return (await self.process.stdout.readline()).decode()

    async def close(self):
        process, self.process = self.process, None
        if process and process.returncode is None:
            process.stdin.close()
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
