"""Freshness, private storage, invalidation and concurrent read/write ordering."""

import asyncio
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hrworks.cache import CachedReads, ReadCache
from hrworks.transport import WorkerError


class CacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        env = patch.dict(os.environ, {"HRWORKS_CACHE_DIR": str(self.root / "cache")})
        env.start()
        self.addCleanup(env.stop)
        self.settings = {"timezone": "Europe/Berlin", "profile_id": "a" * 64}
        self.cache = ReadCache(self.root / "config.toml", "default", self.settings)
        self.client = SimpleNamespace(
            day=AsyncMock(return_value={"entries": [{"end": "17:00"}]}),
            account=AsyncMock(return_value={"metrics": {"total_balance": 42}}),
            snapshot=AsyncMock(return_value={"metrics": {}, "events": []}),
            health=AsyncMock(return_value={"protocol_version": 1}),
            record=AsyncMock(return_value={"saved": True}),
        )
        self.reads = CachedReads(self.client, self.cache, self.settings)
        self.reads.today = date(2026, 10, 3)

    async def test_persisted_hit_does_not_contact_worker(self):
        result = await self.reads.account()
        reopened = CachedReads(
            self.client,
            ReadCache(self.root / "config.toml", "default", self.settings),
            self.settings,
        )
        reopened.today = self.reads.today
        self.assertEqual(await reopened.account(), result)
        self.client.account.assert_awaited_once()
        self.assertEqual(self.cache.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.cache.path.parent.stat().st_mode & 0o777, 0o700)

    async def test_no_cache_fetches_and_refreshes_data(self):
        await self.reads.account()
        self.client.account.return_value = {"metrics": {"total_balance": 99}}
        self.reads.bypass = True
        fresh = await self.reads.account()
        self.reads.bypass = False
        self.assertEqual(await self.reads.account(), fresh)
        self.assertEqual(self.client.account.await_count, 2)

    async def test_age_sensitive_ttls(self):
        for day, ttl in [
            ("2026-10-03", 60),
            ("2026-10-02", 900),
            ("2026-08-01", 86400),
            ("2026-10-04", 300),
        ]:
            with self.subTest(day=day):
                with patch("hrworks.cache.time.time", return_value=100000):
                    await self.reads.day(day)
                with patch("hrworks.cache.time.time", return_value=100000 + ttl - 1):
                    await self.reads.day(day, reuse=True)
                self.assertEqual(self.client.day.await_count, 1)
                with patch("hrworks.cache.time.time", return_value=100000 + ttl):
                    await self.reads.day(day)
                self.assertEqual(self.client.day.await_count, 2)
                self.client.day.reset_mock()

    async def test_open_intervals_expire_quickly_even_on_old_days(self):
        self.client.day.return_value = {"entries": [{"end": None}]}
        self.reads.ttl = 86400
        with patch("hrworks.cache.time.time", return_value=100000):
            await self.reads.day("2026-08-01")
        with patch("hrworks.cache.time.time", return_value=100031):
            await self.reads.day("2026-08-01")
        self.assertEqual(self.client.day.await_count, 2)

    async def test_account_and_snapshot_keys_roll_over_at_midnight(self):
        await self.reads.account()
        await self.reads.snapshot({"include_pending": False})
        self.reads.today += timedelta(days=1)
        await self.reads.account()
        await self.reads.snapshot({"include_pending": False})
        self.assertEqual(self.client.account.await_count, 2)
        self.assertEqual(self.client.snapshot.await_count, 2)

    async def test_different_snapshot_options_never_share_results(self):
        await self.reads.snapshot({"include_pending": False})
        await self.reads.snapshot({"include_pending": True})
        await self.reads.snapshot({"include_pending": False})
        self.assertEqual(self.client.snapshot.await_count, 2)

    async def test_write_evicts_affected_day_and_aggregates_only(self):
        await self.reads.day("2026-10-01")
        await self.reads.day("2026-10-02")
        await self.reads.account()
        await self.reads.snapshot({})
        self.cache.invalidate("2026-10-01")
        await self.reads.day("2026-10-02")
        self.assertEqual(self.client.day.await_count, 2)
        await self.reads.day("2026-10-01")
        await self.reads.account()
        await self.reads.snapshot({})
        self.assertEqual(self.client.day.await_count, 3)
        self.assertEqual(self.client.account.await_count, 2)
        self.assertEqual(self.client.snapshot.await_count, 2)

    async def test_in_flight_read_cannot_repopulate_invalidated_data(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow():
            entered.set()
            await release.wait()
            return {"metrics": {}}

        self.client.account.side_effect = slow
        request = asyncio.create_task(self.reads.account())
        await entered.wait()
        self.cache.invalidate("2026-10-01")
        release.set()
        await request
        self.assertIsNone(self.cache.lookup("account", {"today": self.reads.today.isoformat()})[0])

    async def test_profiles_accounts_and_endpoints_are_isolated(self):
        await self.reads.account()
        for name, settings in [
            ("other", self.settings),
            ("default", {**self.settings, "profile_id": "b" * 64}),
            ("default", {**self.settings, "cdp_url": "http://other:9222"}),
        ]:
            other = ReadCache(self.root / "config.toml", name, settings)
            self.assertIsNone(other.lookup("account", {"today": self.reads.today.isoformat()})[0])
        # Secret rotation must not create unreachable cache partitions.
        secrets = ReadCache(
            self.root / "config.toml",
            "default",
            {**self.settings, "password": "secret", "totp_uri": "secret"},
        )
        self.assertEqual(secrets.scope, self.cache.scope)

    async def test_failed_reads_are_never_cached(self):
        self.client.account.side_effect = WorkerError("browser_unavailable")
        for _ in range(2):
            with self.assertRaises(WorkerError):
                await self.reads.account()
        self.assertEqual(self.client.account.await_count, 2)

    async def test_custom_ttl_applies_to_existing_entries_and_zero_bypasses(self):
        with patch("hrworks.cache.time.time", return_value=100000):
            await self.reads.account()
        self.reads.ttl = 5
        with patch("hrworks.cache.time.time", return_value=100006):
            await self.reads.account()
        self.assertEqual(self.client.account.await_count, 2)
        self.reads.ttl = 0
        await self.reads.account()
        await self.reads.account()
        self.assertEqual(self.client.account.await_count, 4)

    async def test_live_operations_are_not_cached(self):
        for _ in range(2):
            await self.reads.health()
            await self.reads.record({"dry_run": True})
        self.assertEqual(self.client.health.await_count, 2)
        self.assertEqual(self.client.record.await_count, 2)

    async def test_clock_rollback_and_corrupt_rows_are_cache_misses(self):
        with patch("hrworks.cache.time.time", return_value=100000):
            await self.reads.account()
        with patch("hrworks.cache.time.time", return_value=99999):
            self.assertIsNone(
                self.cache.lookup("account", {"today": self.reads.today.isoformat()})[0]
            )
        with self.cache.connection() as connection:
            connection.execute("UPDATE reads SET data='not-json'")
        with patch("hrworks.cache.time.time", return_value=100001):
            self.assertIsNone(
                self.cache.lookup("account", {"today": self.reads.today.isoformat()})[0]
            )

    async def test_clear_all_invalidates_other_scopes(self):
        await self.reads.account()
        other = ReadCache(self.root / "config.toml", "other", self.settings)
        _, generation = other.lookup("account", {})
        other.store("account", {}, {"metrics": {}}, 60, generation)
        self.cache.invalidate(all_profiles=True)
        self.assertIsNone(other.lookup("account", {})[0])
        other.store("account", {}, {"metrics": {}}, 60, generation)
        self.assertIsNone(other.lookup("account", {})[0])

    async def test_profile_clear_removes_previous_account_partitions(self):
        await self.reads.account()
        old = ReadCache(self.root / "config.toml", "default", {**self.settings, "profile_id": None})
        _, generation = old.lookup("account", {})
        old.store("account", {}, {"metrics": {}}, 60, generation)
        self.cache.invalidate()
        self.assertIsNone(old.lookup("account", {})[0])
        self.assertIsNone(self.cache.lookup("account", {"today": self.reads.today.isoformat()})[0])

    async def test_write_invalidates_reads_in_other_account_partitions(self):
        old = ReadCache(self.root / "config.toml", "default", {**self.settings, "profile_id": None})
        _, generation = old.lookup("day", {"date": "2026-10-01"})
        self.cache.invalidate("2026-10-01")
        old.store("day", {"date": "2026-10-01"}, {"entries": []}, 60, generation)
        self.assertIsNone(old.lookup("day", {"date": "2026-10-01"})[0])
