"""Private CLI read cache with bounded freshness and write invalidation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .transport import WorkerError

SCHEMA = 1


class ReadCache:
    """SQLite transactions prevent an in-flight read undoing invalidation."""

    def __init__(self, config: Path, profile: str, settings: dict):
        directory = Path(
            os.environ.get(
                "HRWORKS_CACHE_DIR",
                str(Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hrworks"),
            )
        ).expanduser()
        self.path = directory / "reads.sqlite3"
        # Include account and endpoint identity, but never passwords or TOTP seeds.
        identity = {
            key: settings.get(key)
            for key in (
                "profile_id",
                "company_id",
                "username",
                "rbw_entry",
                "transport",
                "worker_command",
                "ssh_host",
                "ssh_port",
                "ssh_username",
                "cdp_url",
                "state_dir",
                "timezone",
            )
        }
        owner = {"config": str(config.resolve()), "profile": profile, "schema": SCHEMA}
        self.owner = hashlib.sha256(json.dumps(owner, sort_keys=True).encode()).hexdigest()
        identity.update(owner)
        self.scope = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @contextmanager
    def connection(self):
        connection = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path.parent.chmod(0o700)
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            connection = sqlite3.connect(self.path, timeout=5)
            with connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS generations (scope TEXT PRIMARY KEY, owner TEXT, value INTEGER)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS reads (scope TEXT, operation TEXT, key TEXT, "
                    "data TEXT, created REAL, expires REAL, PRIMARY KEY (scope, operation, key))"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO generations VALUES (?, ?, 0)", (self.scope, self.owner)
                )
                connection.commit()
                yield connection
        except (OSError, sqlite3.Error):
            raise WorkerError("cache_unavailable") from None
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def key(payload: dict) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def lookup(
        self, operation: str, payload: dict, max_age: float | None = None
    ) -> tuple[dict | None, int]:
        with self.connection() as connection:
            generation = connection.execute(
                "SELECT value FROM generations WHERE scope=?", (self.scope,)
            ).fetchone()[0]
            row = connection.execute(
                "SELECT data FROM reads WHERE scope=? AND operation=? AND key=? AND expires>? AND created>? AND created<=?",
                (
                    self.scope,
                    operation,
                    self.key(payload),
                    time.time(),
                    time.time() - max_age if max_age is not None else 0,
                    time.time(),
                ),
            ).fetchone()
        try:
            value = json.loads(row[0]) if row else None
            return (value if isinstance(value, dict) else None), generation
        except (ValueError, TypeError):
            return None, generation

    def store(self, operation: str, payload: dict, data: dict, ttl: float, generation: int):
        with self.connection() as connection:
            # A write or cache clear since this read started makes its result obsolete.
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT value FROM generations WHERE scope=?", (self.scope,)
            ).fetchone()[0]
            connection.execute("DELETE FROM reads WHERE expires<=?", (time.time(),))
            if current == generation and ttl <= 0:
                connection.execute(
                    "DELETE FROM reads WHERE scope=? AND operation=? AND key=?",
                    (self.scope, operation, self.key(payload)),
                )
            if current == generation and ttl > 0:
                connection.execute(
                    "INSERT OR REPLACE INTO reads VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.scope,
                        operation,
                        self.key(payload),
                        json.dumps(data),
                        time.time(),
                        time.time() + ttl,
                    ),
                )

    def invalidate(self, day: str | None = None, *, all_profiles: bool = False):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if all_profiles:
                connection.execute("UPDATE generations SET value=value+1")
                connection.execute("DELETE FROM reads")
            else:
                connection.execute(
                    "UPDATE generations SET value=value+1 WHERE owner=?", (self.owner,)
                )
                if day is None:
                    connection.execute(
                        "DELETE FROM reads WHERE scope IN (SELECT scope FROM generations WHERE owner=?)",
                        (self.owner,),
                    )
                else:
                    connection.execute(
                        "DELETE FROM reads WHERE scope IN (SELECT scope FROM generations WHERE owner=?) "
                        "AND (operation!='day' OR key=?)",
                        (self.owner, self.key({"date": day})),
                    )


class CachedReads:
    """Cache read data only; authentication, health and submissions stay live."""

    def __init__(self, client, cache: ReadCache, settings: dict, *, bypass=False, ttl=None):
        self.client = client
        self.cache = cache
        self.bypass = bypass
        self.ttl = ttl
        self.today = datetime.now(ZoneInfo(settings["timezone"])).date()

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def read(self, operation: str, payload: dict, fetch, ttl: float):
        data, generation = self.cache.lookup(operation, payload, self.ttl)
        if data is not None and not self.bypass and self.ttl != 0:
            return data
        data = await fetch()
        lifetime = ttl if self.ttl is None else self.ttl
        # Open intervals can change even on an older day.
        if any(not entry.get("end") for entry in data.get("entries", [])):
            lifetime = min(lifetime, 30)
        self.cache.store(operation, payload, data, lifetime, generation)
        return data

    async def account(self):
        return await self.read(
            "account", {"today": self.today.isoformat()}, self.client.account, 60
        )

    async def snapshot(self, options: dict):
        return await self.read(
            "snapshot",
            {**options, "today": self.today.isoformat()},
            lambda: self.client.snapshot(options),
            60,
        )

    async def day(self, day: str, *, reuse=False):
        age = (self.today - date.fromisoformat(day)).days
        ttl = 60 if age == 0 else 300 if age < 0 else 900 if age <= 31 else 86400
        return await self.read("day", {"date": day}, lambda: self.client.day(day, reuse=reuse), ttl)
