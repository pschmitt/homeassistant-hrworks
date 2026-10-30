"""Day reads against a synthetic, offline working-times page served through routing."""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path

from playwright.async_api import async_playwright

from hrworks_worker.portal import EmployeePortal, PortalError

# Like the portal, the open week lists only days with a target or recorded time.
WORKING_TIMES = """<!doctype html><body><div class="m-portlet">
<select><option>2025</option><option selected>2026</option></select>
<select>{months}</select>
<div class="m-accordion__item-head"><span class="m-accordion__item-title">
CW 43 (19.10. - 25.10.)</span></div>
<div><div class="hrw-me-working-time-day-header-row"><div>Fr, 23.10.26</div>
<span class="hrw-me-working-time-day-header-span">-04:00 hours</span></div></div>
<div><div class="hrw-me-working-time-day-header-row"><div>Mo, 19.10.26</div>
<span class="hrw-me-working-time-day-header-span">+00:00 hours</span></div></div>
</div></body>"""
MONTHS = "".join(
    f"<option{' selected' if month == 10 else ''}>{month}</option>" for month in range(1, 13)
)


class WorkingDayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        endpoint = os.environ.get("HRWORKS_TEST_CDP")
        self.browser = (
            await self.driver.chromium.connect_over_cdp(endpoint, no_defaults=True)
            if endpoint
            else await self.driver.chromium.launch()
        )
        self.directory = tempfile.TemporaryDirectory()
        self.portal = EmployeePortal(self.browser, Path(self.directory.name), "d" * 64)
        await self.portal.open()
        await self.portal.context.route("**/*", self.serve)

    async def asyncTearDown(self):
        await self.portal.close()
        await self.driver.stop()
        self.directory.cleanup()

    async def serve(self, route):
        await route.fulfill(content_type="text/html", body=WORKING_TIMES.format(months=MONTHS))

    async def test_listed_day_is_read(self):
        metrics, entries = await self.portal.working_day(date(2026, 10, 23))
        self.assertEqual(metrics, {"today_balance": -240})
        self.assertEqual(entries, [])

    async def test_day_missing_from_open_week_has_no_target_or_time(self):
        for day in (date(2026, 10, 22), date(2026, 10, 25)):
            with self.subTest(day=day):
                self.assertEqual(
                    await self.portal.working_day(day, reuse=True),
                    ({"today_target": 0, "today_worked": 0, "today_balance": 0}, []),
                )

    async def test_day_outside_listed_weeks_fails_visibly(self):
        with self.assertRaises(PortalError) as error:
            await self.portal.working_day(date(2026, 10, 12))
        self.assertEqual(error.exception.code, "portal_changed")

    async def test_save_on_a_day_without_row_is_rejected_before_writing(self):
        with self.assertRaises(PortalError) as error:
            await self.portal.record(
                {
                    "start": "2026-10-22T08:00:00+02:00",
                    "end": "2026-10-22T12:00:00+02:00",
                    "dry_run": False,
                }
            )
        self.assertEqual(error.exception.code, "invalid_interval")
