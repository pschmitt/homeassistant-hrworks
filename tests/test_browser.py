"""Offline synthetic DOM checks using isolated Chromium contexts."""

import os
import tempfile
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from hrworks_worker.portal import EmployeePortal, PortalError, normalize


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        endpoint = os.environ.get("HRWORKS_TEST_CDP")
        self.browser = (
            await self.driver.chromium.connect_over_cdp(endpoint, no_defaults=True)
            if endpoint
            else await self.driver.chromium.launch()
        )
        self.directory = tempfile.TemporaryDirectory()
        self.portal = EmployeePortal(self.browser, Path(self.directory.name), "a" * 64)
        self.page = await self.portal.open()
        await self.page.set_content((Path(__file__).parent / "fixtures/editor.html").read_text())

    async def asyncTearDown(self):
        await self.portal.close()
        await self.driver.stop()
        self.directory.cleanup()

    async def test_label_controls_and_separate_intervals(self):
        entries = await self.portal.editor_entries(self.page.locator("#day"))
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            entries[0],
            {
                "start": "08:00",
                "end": "12:00",
                "type": "Working time",
                "comment": "Invented morning",
            },
        )
        self.assertEqual(normalize(entries[1]["type"]), "doctor's appointment")

    async def test_regenerated_ids_and_changed_container_classes(self):
        await self.page.evaluate("""() => {
            for (const label of document.querySelectorAll('label[for]')) {
                const input = label.control; input.id += '-new'; label.htmlFor = input.id;
                label.parentElement.className = 'redesigned';
            }
        }""")
        entries = await self.portal.editor_entries(self.page.locator("#day"))
        self.assertEqual(
            [(e["start"], e["end"]) for e in entries], [("08:00", "12:00"), ("13:00", "17:00")]
        )

    async def test_german_labels_and_open_entry(self):
        await self.page.evaluate("""() => {
            const translations = {'Start time:':' Beginn: ', 'Start time':'Beginn',
                'End time':'Ende:', 'Working time type':'Arbeitszeitart', 'Comment':'Kommentar'};
            for (const label of document.querySelectorAll('label')) label.textContent = translations[label.textContent] || label.textContent;
            document.getElementById('second-end').value = '';
        }""")
        entries = await self.portal.editor_entries(self.page.locator("#day"))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1]["end"], "")

    async def test_force_login_does_not_reuse_cookies(self):
        await self.portal.context.add_cookies(
            [{"name": "synthetic", "value": "test", "url": "https://ssl6.hrworks.de"}]
        )
        await self.portal.save_session()
        await self.portal.close()
        await self.portal.open(fresh=True)
        self.assertEqual(await self.portal.context.cookies(), [])

    async def test_unknown_labels_fail_visibly(self):
        await self.page.locator("label").evaluate_all(
            "labels => labels.forEach(label => label.textContent='Changed meaning')"
        )
        with self.assertRaises(PortalError) as error:
            await self.portal.editor_entries(self.page.locator("#day"))
        self.assertEqual(error.exception.code, "portal_changed")

    async def test_hidden_labels_in_visible_groups_are_read(self):
        await self.page.locator("label").evaluate_all(
            "labels => labels.forEach(label => label.style.display='none')"
        )
        entries = await self.portal.editor_entries(self.page.locator("#day"))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["start"], "08:00")
