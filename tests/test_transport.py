"""Protocol sanitization, serialization and uncertain-write guarantees."""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from hrworks.transport import (
    OpenSshTransport,
    SshTransport,
    Transport,
    WorkerError,
    openssh_command,
)
from playwright.async_api import TimeoutError as PlaywrightTimeout

from hrworks_worker.portal import EmployeePortal, PortalError, ensure_viewport


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

    async def test_browser_failure_discards_worker_without_replaying_request(self):
        for path, payload, expected in [
            ("profiles/test/snapshot", {}, "browser_unavailable"),
            ("profiles/test/record", {"dry_run": True}, "browser_unavailable"),
            ("profiles/test/record", {"dry_run": False}, "write_uncertain"),
        ]:
            transport = FakeTransport()
            exchange = transport.exchange

            async def failed(message, exchange=exchange):
                await exchange(message)
                return '{"error":"browser_unavailable"}'

            transport.exchange = AsyncMock(side_effect=failed)
            transport.close = AsyncMock()
            with self.assertRaises(WorkerError) as error:
                await transport.request(path, payload)
            self.assertEqual(error.exception.code, expected)
            transport.close.assert_awaited_once()
            transport.exchange.assert_awaited_once()

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

    def test_openssh_command_defers_host_details_to_ssh_config(self):
        self.assertEqual(
            openssh_command({"ssh_host": "alias", "worker_command": "hrworks-worker-x"}),
            ["ssh", "-q", "-o", "BatchMode=yes", "--", "alias", "hrworks-worker-x --stdio"],
        )
        # A multi-word worker command stays multi-word for the remote shell.
        self.assertEqual(
            openssh_command({"ssh_host": "alias"})[-1],
            "hrworks worker --stdio",
        )
        self.assertEqual(
            openssh_command({"ssh_host": "alias", "worker_command": "hrworks worker --x 1"})[-1],
            "hrworks worker --x 1 --stdio",
        )
        self.assertEqual(
            openssh_command(
                {
                    "ssh_host": "h",
                    "ssh_port": 2222,
                    "ssh_username": "u",
                    "ssh_key_path": "/synthetic/key",
                }
            ),
            [
                "ssh", "-q", "-o", "BatchMode=yes", "-p", "2222", "-l", "u",
                "-i", "/synthetic/key", "--", "h", "hrworks worker --stdio",
            ],
        )  # fmt: skip

    def test_openssh_command_rejects_option_like_or_missing_hosts(self):
        for host in (None, "", "-oProxyCommand=synthetic"):
            with self.assertRaises(WorkerError) as error:
                openssh_command({"ssh_host": host})
            self.assertEqual(error.exception.code, "invalid_config")

    async def test_openssh_transport_round_trips_and_maps_ssh_failure(self):
        settings = {"ssh_host": "synthetic"}
        for argv, expected in [
            (["sh", "-c", "read l; echo '{\"ok\": 1}'"], {"ok": 1}),
            (["sh", "-c", "read l; exit 255"], "cannot_connect"),
            (["sh", "-c", "read l; exit 3"], "incompatible_worker"),
        ]:
            with patch("hrworks.transport.openssh_command", return_value=argv):
                transport = OpenSshTransport(settings, timeout=10)
            try:
                if isinstance(expected, dict):
                    self.assertEqual(await transport.request("/x"), expected)
                else:
                    with self.assertRaises(WorkerError) as error:
                        await transport.request("/x")
                    self.assertEqual(error.exception.code, expected)
            finally:
                await transport.close()

    async def test_viewport_is_forced_when_the_browser_ignores_emulation(self):
        for size, overrides in [([800, 600], 1), ([1600, 1000], 0)]:
            page = MagicMock()
            page.evaluate = AsyncMock(return_value=size)
            session = MagicMock()
            session.send = AsyncMock()
            page.context.new_cdp_session = AsyncMock(return_value=session)
            await ensure_viewport(page)
            self.assertEqual(session.send.await_count, overrides)
            if overrides:
                session.send.assert_awaited_with(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
                )

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
