"""Safety checks against a fake day reader; no HR WORKS writes."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from hrworks_worker.portal import BERLIN, EmployeePortal, PortalError, dates, minutes


class RecordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.portal = EmployeePortal(None, Path(self.directory.name), "a" * 64)
        self.portal.working_day = AsyncMock(return_value=({}, []))
        self.day = (datetime.now(BERLIN) - timedelta(days=2)).date().isoformat()
        self.data = {
            "start": f"{self.day}T08:00:00+02:00",
            "end": f"{self.day}T12:00:00+02:00",
            "dry_run": True,
        }

    async def assert_error(self, data, code):
        with self.assertRaises(PortalError) as context:
            await self.portal.record(data)
        self.assertEqual(context.exception.code, code)

    async def test_preview_preserves_split_intervals(self):
        self.portal.working_day.return_value = (
            {},
            [{"start": "13:00", "end": "17:00", "type": "Working time"}],
        )
        self.assertEqual((await self.portal.record(self.data))["duration_minutes"], 240)

    async def test_touching_intervals_are_allowed(self):
        self.portal.working_day.return_value = (
            {},
            [{"start": "12:00", "end": "17:00", "type": "Working time"}],
        )
        self.assertFalse((await self.portal.record(self.data))["saved"])

    async def test_overlapping_interval_is_rejected(self):
        self.portal.working_day.return_value = (
            {},
            [{"start": "11:59", "end": "17:00", "type": "Working time"}],
        )
        await self.assert_error(self.data, "overlap")

    async def test_open_existing_interval_is_rejected(self):
        self.portal.working_day.return_value = (
            {},
            [{"start": "11:00", "end": "", "type": "Working time"}],
        )
        await self.assert_error(self.data, "overlap")

    async def test_exact_duplicate_does_not_submit_again(self):
        self.portal.working_day.return_value = (
            {},
            [{"start": "08:00", "end": "12:00", "type": "Working time", "comment": ""}],
        )
        self.assertTrue((await self.portal.record(self.data))["already_exists"])

    async def test_different_type_cannot_replace_existing_interval(self):
        self.portal.working_day.return_value = (
            {},
            [{"start": "08:00", "end": "12:00", "type": "Doctor's appointment"}],
        )
        await self.assert_error(self.data, "overlap")

    async def test_comment_change_cannot_replace_existing_interval(self):
        self.portal.working_day.return_value = (
            {},
            [{"start": "08:00", "end": "12:00", "type": "Working time", "comment": "Existing"}],
        )
        await self.assert_error(self.data, "overlap")

    async def test_bad_intervals_fail_before_reading_portal(self):
        tomorrow = (datetime.now(BERLIN) + timedelta(days=2)).date().isoformat()
        cases = [
            {"start": "not a timestamp"},
            {"start": f"{self.day}T08:00:00"},
            {"start": f"{self.day}T12:00:00+02:00"},
            {"end": f"{self.day}T07:59:00+02:00"},
            {"start": f"{self.day}T08:00:01+02:00"},
            {"start": f"{tomorrow}T08:00:00+02:00", "end": f"{tomorrow}T12:00:00+02:00"},
            {"end": f"{self.day}T00:00:00+02:00"},
            {"type": "unknown"},
            {"comment": "x" * 501},
        ]
        for change in cases:
            with self.subTest(change=change):
                await self.assert_error({**self.data, **change}, "invalid_interval")
        self.portal.working_day.assert_not_awaited()

    def test_time_and_date_parser(self):
        self.assertEqual(minutes("-08:30 hours"), -510)
        self.assertIsNone(minutes("08:60 hours"))
        self.assertEqual(dates("01.10.2026 - 02.10.26")[0].year, 2026)
