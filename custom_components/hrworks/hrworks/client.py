"""Asynchronous employee API, independent of Home Assistant and presentation."""

from __future__ import annotations

import re
from typing import Any

from .transport import SshTransport, Transport, WorkerError


class WorkerClient:
    """A serialized worker session. Use as an async context manager."""

    def __init__(self, settings: dict, *, transport: Transport | None = None):
        self.settings = dict(settings)
        self.profile = settings.get("profile_id")
        self.capabilities = None
        self.transport = transport or SshTransport(settings)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    async def close(self):
        self.capabilities = None
        await self.transport.close()

    async def request(self, path: str, payload: dict | None = None) -> dict[str, Any]:
        return await self.transport.request(path, payload)

    def _path(self, operation: str) -> str:
        if not self.profile:
            raise WorkerError("session_expired")
        return f"profiles/{self.profile}/{operation}"

    async def health(self) -> dict:
        result = await self.request("health")
        if result.get("protocol_version") != 1:
            raise WorkerError("incompatible_worker")
        self.capabilities = result.get("capabilities", [])
        if not isinstance(self.capabilities, list) or not all(
            isinstance(value, str) for value in self.capabilities
        ):
            raise WorkerError("incompatible_worker")
        return result

    async def _require_feature(self, feature: str):
        if self.capabilities is None:
            await self.health()
        if feature not in self.capabilities:
            raise WorkerError("incompatible_worker")

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
        return await self.request(self._path("mfa"), {"code": code})

    async def snapshot(self, options: dict) -> dict:
        return await self.request(self._path("snapshot"), options)

    async def record(self, data: dict) -> dict:
        return await self.request(self._path("record"), data)

    async def day(self, day: str, *, reuse: bool = False) -> dict:
        path = self._path("day")
        await self._require_feature("day")
        return await self.request(path, {"date": day, "reuse": reuse})

    async def logout(self) -> dict:
        if not self.profile:
            return {"logged_out": True}
        await self._require_feature("logout")
        return await self.request(self._path("logout"), {})

    async def account(self) -> dict:
        path = self._path("account")
        await self._require_feature("account")
        return await self.request(path, {})
