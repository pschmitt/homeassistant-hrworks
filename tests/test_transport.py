"""Protocol sanitization, serialization and uncertain-write guarantees."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from hrworks.transport import SshTransport, Transport, WorkerError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from hrworks_worker.portal import EmployeePortal, PortalError


class FakeTransport(Transport):
    async def exchange(self, message):
        self.submitted = True
        return "{}"

    async def close(self):
        pass


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_and_remote_errors_do_not_leak_data(self):
        for line in ["secret cookies", "[]", '{"error":["secret"]}', '{"error":"secret password"}']:
            transport = FakeTransport()
            transport.exchange = AsyncMock(return_value=line)
            with self.assertRaises(WorkerError) as error:
                await transport.request("health")
            self.assertNotIn("secret", str(error.exception))

    async def test_failed_submitted_write_is_uncertain(self):
        for output in [None, "not-json", ""]:
            transport = FakeTransport()

            async def exchange(message, output=output, transport=transport):
                transport.submitted = True
                if output is None:
                    raise OSError("sensitive details")
                return output

            transport.exchange = exchange
            with self.assertRaises(WorkerError) as error:
                await transport.request("profiles/test/record", {"dry_run": False})
            self.assertEqual(error.exception.code, "write_uncertain")

    async def test_reads_and_previews_are_not_uncertain_writes(self):
        transport = FakeTransport()
        transport.exchange = AsyncMock(side_effect=OSError("secret"))
        with self.assertRaises(WorkerError) as error:
            await transport.request("profiles/test/record", {"dry_run": True})
        self.assertEqual(error.exception.code, "cannot_connect")

    async def test_timeout(self):
        transport = FakeTransport(timeout=0.01)

        async def slow(message):
            await asyncio.sleep(0.1)
            return "{}"

        transport.exchange = slow
        with self.assertRaises(WorkerError):
            await transport.request("health")

    async def test_concurrent_requests_are_serialized(self):
        transport = FakeTransport()
        active = peak = 0

        async def exchange(message):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return "{}"

        transport.exchange = exchange
        await asyncio.gather(*(transport.request("health") for _ in range(5)))
        self.assertEqual(peak, 1)

    async def test_late_login_redirect_is_session_expired(self):
        page = MagicMock()
        page.url = "https://ssl6.hrworks.de/o/time-management/working-times"
        page.goto = AsyncMock()
        page.locator.return_value.first.count = AsyncMock(return_value=0)
        page.locator.return_value.wait_for = AsyncMock()

        async def redirect(**kwargs):
            page.url = "https://login.hrworks.de/"
            raise PlaywrightTimeout("synthetic redirect")

        page.locator.return_value.first.wait_for = redirect
        portal = MagicMock()
        portal.open = AsyncMock(return_value=page)
        with self.assertRaises(PortalError) as error:
            await EmployeePortal.navigate(portal, "working-times")
        self.assertEqual(error.exception.code, "session_expired")

    async def test_optional_ssh_key_keeps_default_key_and_agent_discovery(self):
        for settings, expected in [
            ({"ssh_host": "synthetic"}, ()),
            ({"ssh_host": "synthetic", "ssh_key_path": "/synthetic/key"}, ["/synthetic/key"]),
        ]:
            connection = MagicMock()
            connection.create_process = AsyncMock(return_value=MagicMock())
            connection.wait_closed = AsyncMock()
            transport = SshTransport(settings)
            with patch(
                "hrworks.transport.asyncssh.connect", AsyncMock(return_value=connection)
            ) as connect:
                await transport.connect()
                self.assertEqual(connect.call_args.kwargs["client_keys"], expected)
                self.assertEqual(
                    connect.call_args.kwargs["agent_path"], () if expected == () else None
                )
            await transport.close()

    async def test_month_reads_reuse_only_the_requested_portal_route(self):
        page = MagicMock()
        page.url = "https://ssl6.hrworks.de/o/time-management/working-times"
        page.goto = AsyncMock()
        page.locator.return_value.first.wait_for = AsyncMock()
        page.locator.return_value.first.count = AsyncMock(return_value=1)
        page.locator.return_value.first.click = AsyncMock()
        portal = MagicMock()
        portal.open = AsyncMock(return_value=page)
        portal.settle = AsyncMock()
        await EmployeePortal.navigate(portal, "working-times", reuse=True)
        page.goto.assert_not_awaited()
        page.locator.return_value.first.click.assert_not_awaited()
        await EmployeePortal.navigate(portal, "working-time-months", reuse=True)
        page.locator.return_value.first.click.assert_awaited_once()
