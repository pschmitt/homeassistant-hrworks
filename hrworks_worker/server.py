"""JSON-line browser worker intended to run as an SSH child process."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from . import PROTOCOL_VERSION
from .portal import EmployeePortal, PortalError, profile_id

LOGGER = logging.getLogger(__name__)


class Worker:
    def __init__(self, cdp_url: str, state_dir: Path, enable_writes: bool) -> None:
        self.cdp_url = cdp_url
        self.state_dir = state_dir
        self.enable_writes = enable_writes
        self.playwright = None
        self.browser = None
        self.profiles: dict[str, EmployeePortal] = {}

    async def connect(self) -> None:
        if self.browser and self.browser.is_connected():
            return
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.connect_over_cdp(
            self.cdp_url, no_defaults=True, timeout=15000
        )
        for portal in self.profiles.values():
            portal.browser = self.browser
            portal.context = portal.page = None

    async def portal(self, identity: str, *, create: bool = False) -> EmployeePortal:
        if not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise PortalError("invalid_request")
        await self.connect()
        if identity not in self.profiles:
            if not create and not (self.state_dir / f"{identity}.json").exists():
                raise PortalError("session_expired", 403)
            if len(self.profiles) >= 16:
                raise PortalError("busy", 503)
            self.profiles[identity] = EmployeePortal(self.browser, self.state_dir, identity)
        return self.profiles[identity]

    async def close(self) -> None:
        try:
            for portal in self.profiles.values():
                if self.browser and self.browser.is_connected():
                    await portal.close()
        finally:
            # Disconnect our driver; never close Chromium or other users' contexts.
            if self.playwright:
                await self.playwright.stop()

    async def dispatch(self, path: str, data: dict | None) -> dict:
        if path == "health":
            await self.connect()
            return {"protocol_version": PROTOCOL_VERSION, "writes_enabled": self.enable_writes}
        if not isinstance(data, dict):
            raise PortalError("invalid_request")
        if path == "login":
            if any(
                not isinstance(data.get(k), str) or not data[k].strip()
                for k in ("company_id", "username", "password")
            ):
                raise PortalError("invalid_request")
            identity = profile_id(data["company_id"].strip(), data["username"].strip())
            portal = await self.portal(identity, create=True)
            async with portal.lock:
                return await portal.login(
                    data["company_id"].strip(), data["username"].strip(), data["password"]
                )
        match = re.fullmatch(r"profiles/([a-f0-9]{64})/(mfa|snapshot|record)", path)
        if not match:
            raise PortalError("not_found", 404)
        portal = await self.portal(match[1])
        async with portal.lock:
            match match[2]:
                case "mfa":
                    if not isinstance(data.get("code"), str):
                        raise PortalError("invalid_request")
                    return await portal.mfa(data["code"].strip())
                case "snapshot":
                    for key in ("calendar_past_days", "calendar_future_days"):
                        if type(data.get(key)) is not int or not 0 <= data[key] <= 730:
                            raise PortalError("invalid_request")
                    for key in ("include_sickness", "include_pending"):
                        if type(data.get(key)) is not bool:
                            raise PortalError("invalid_request")
                    return await portal.snapshot(data)
                case "record":
                    if type(data.get("dry_run", True)) is not bool:
                        raise PortalError("invalid_request")
                    if not data.get("dry_run", True) and not self.enable_writes:
                        raise PortalError("writes_disabled", 403)
                    return await portal.record(data)
        raise PortalError("invalid_request")


async def serve(worker: Worker) -> None:
    """Bound requests, sanitize errors and reserve stdout for protocol messages."""
    reader = asyncio.StreamReader(limit=16 * 1024)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin)
    try:
        while True:
            try:
                # An abandoned HA setup flow must not leave a remote process running forever.
                async with asyncio.timeout(1800):
                    line = await reader.readline()
            except (ValueError, TimeoutError):
                break
            if not line:
                break
            try:
                async with asyncio.timeout(160):
                    message = json.loads(line)
                    if not isinstance(message, dict) or not isinstance(message.get("path"), str):
                        raise PortalError("invalid_request")
                    result = await worker.dispatch(message["path"], message.get("payload"))
            except PortalError as err:
                result = {"error": err.code}
            except (ValueError, TypeError):
                result = {"error": "invalid_request"}
            except (PlaywrightError, TimeoutError):
                result = {"error": "browser_unavailable"}
            except Exception:
                LOGGER.error("Worker operation failed; portal content omitted")
                result = {"error": "portal_changed"}
            sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    finally:
        transport.close()
        await worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="HR WORKS employee browser worker over SSH")
    parser.add_argument("--stdio", action="store_true", required=True)
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".local/state/hrworks-worker"
    )
    parser.add_argument("--enable-writes", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    asyncio.run(serve(Worker(args.cdp_url, args.state_dir, args.enable_writes)))


if __name__ == "__main__":
    main()
