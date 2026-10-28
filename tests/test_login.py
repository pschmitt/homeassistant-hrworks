"""Login outcomes against a synthetic, offline portal served through request routing."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from hrworks_worker.portal import EmployeePortal, PortalError

LOGIN_PAGE = "https://login.hrworks.de/login"
FORM = """<!doctype html><html><body>
<input name="company"><input name="login"><input type="password">
<button onclick="{action}">Log in to HR WORKS</button>
<p id="message"></p></body></html>"""


class RoutedBrowser:
    """Serve every context's requests from the test, never from the network."""

    def __init__(self, browser, handler):
        self.browser = browser
        self.handler = handler

    async def new_context(self, **options):
        context = await self.browser.new_context(**options)
        await context.route("**/*", self.handler)
        return context


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        endpoint = os.environ.get("HRWORKS_TEST_CDP")
        self.browser = (
            await self.driver.chromium.connect_over_cdp(endpoint, no_defaults=True)
            if endpoint
            else await self.driver.chromium.launch()
        )
        self.directory = tempfile.TemporaryDirectory()
        self.portal = EmployeePortal(
            RoutedBrowser(self.browser, self.serve), Path(self.directory.name), "c" * 64
        )
        self.authenticated = False
        self.after_submit = "/2fa-totp"
        self.factors = ["Authenticator"]
        # Start on the login host: requests after a fulfilled redirect bypass routing.
        for name, value in [("LOGIN_URL", LOGIN_PAGE), ("LOGIN_STEP_TIMEOUT", 1500)]:
            patch = mock.patch(f"hrworks_worker.portal.{name}", value)
            patch.start()
            self.addCleanup(patch.stop)

    async def asyncTearDown(self):
        await self.portal.close()
        await self.driver.stop()
        self.directory.cleanup()

    async def serve(self, route):
        url = urlsplit(route.request.url)
        if url.hostname == "ssl6.hrworks.de" and self.authenticated:
            body = '<div class="m-portlet">Synthetic dashboard</div>'
        elif url.path == "/login":
            action = (
                "document.getElementById('message').textContent='Login failed'"
                if self.after_submit == "rejected"
                else f"location.href='{self.after_submit}'"
            )
            body = FORM.format(action=action)
        elif url.path == "/2fa-selection":
            body = "".join(
                f"<button onclick=\"location.href='/2fa-totp'\">{name}</button>"
                if name == "Authenticator"
                else f"<button>{name}</button>"
                for name in self.factors
            )
        elif url.path == "/2fa-totp":
            self.authenticated = True
            body = """<input><button onclick="if (document.querySelector('input').value
                === '123456') location.href='https://ssl6.hrworks.de/o/dashboard'">
                Unlock</button>"""
        elif url.path == "/2fa-setup":
            body = "<p>Set up a second factor</p>"
        else:
            body = "<p>Please wait</p>"
        await route.fulfill(content_type="text/html", body=f"<!doctype html><body>{body}</body>")

    async def login(self):
        return await self.portal.login("synthetic-co", "synthetic-user", "synthetic-pw", force=True)

    async def assert_login_error(self, code):
        with self.assertRaises(PortalError) as error:
            await self.login()
        self.assertEqual(error.exception.code, code)

    async def test_totp_login_still_completes(self):
        self.assertTrue((await self.login())["mfa_required"])
        self.assertEqual(
            await self.portal.mfa("123456"), {"profile_id": "c" * 64, "authenticated": True}
        )

    async def test_selection_chooses_authenticator(self):
        self.after_submit = "/2fa-selection"
        self.factors = ["E-mail", "Authenticator"]
        self.assertTrue((await self.login())["mfa_required"])

    async def test_selection_without_authenticator_is_unsupported_mfa(self):
        self.after_submit = "/2fa-selection"
        self.factors = ["E-mail"]
        await self.assert_login_error("unsupported_mfa")

    async def test_other_second_factor_step_is_unsupported_mfa(self):
        self.after_submit = "/2fa-setup"
        await self.assert_login_error("unsupported_mfa")

    async def test_rejected_credentials_are_invalid_auth(self):
        self.after_submit = "rejected"
        await self.assert_login_error("invalid_auth")

    async def test_unknown_wait_is_a_timeout_not_invalid_auth(self):
        self.after_submit = "/pending"
        await self.assert_login_error("login_timeout")
