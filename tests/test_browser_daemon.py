"""The browser-daemon backend against a fake daemon: no network, no HR WORKS."""

import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from browser_daemon_client import BrowserDaemonError, RunError, RunResult, RunTimeout
from hrworks import browser_daemon
from hrworks.auth import authenticate, complete_mfa
from hrworks.browser_daemon import BERLIN, BrowserDaemonClient, profile_id, script
from hrworks.errors import WorkerError

SETTINGS = {
    "backend": "browser_daemon",
    "browser_daemon_url": "http://daemon.invalid:3001",
    "browser_daemon_secret": "cluster-secret",
    "browser_daemon_profile": "hrworks",
}
PASSWORD = "very-secret-password"
URI = "otpauth://totp/Example:test?secret=JBSWY3DPEHPK3PXP"
IDENTITY = profile_id("example", "employee")


def ok(result=None, **fields):
    return RunResult(ok=True, result=result, **fields)


def failed(message, *, timed_out=False):
    run = RunResult(ok=False, error=message, timed_out=timed_out)
    return (RunTimeout if timed_out else RunError)(run, 422)


class StopProfileBrowserTests(unittest.IsolatedAsyncioTestCase):
    def client(self, profile, found):
        daemon = AsyncMock()
        daemon.find_profile.return_value = found
        settings = {"browser_daemon_profile": profile}
        return BrowserDaemonClient(settings, daemon=daemon), daemon

    async def test_stops_the_profiles_session_on_the_node_it_runs_on(self):
        client, daemon = self.client("ha-hrworks", {"session_id": "s1", "node_name": "rofl-14"})
        client._tab = "old-tab"
        self.assertEqual(await client.stop_profile_browser(), "s1")
        daemon.find_profile.assert_awaited_once_with("ha-hrworks")
        daemon.delete_session.assert_awaited_once_with("s1", node="rofl-14")
        self.assertIsNone(client._tab)

    async def test_an_idle_profile_stops_nothing(self):
        client, daemon = self.client("ha-hrworks", {"node_name": "rofl-14"})
        self.assertIsNone(await client.stop_profile_browser())
        daemon.delete_session.assert_not_awaited()

    async def test_a_throwaway_browser_stops_nothing(self):
        client, daemon = self.client("", None)
        self.assertIsNone(await client.stop_profile_browser())
        daemon.find_profile.assert_not_awaited()

    async def test_it_does_not_wait_for_a_stuck_operation(self):
        client, daemon = self.client("p", {"session_id": "s1", "node_name": None})
        async with client._lock:
            self.assertEqual(await client.stop_profile_browser(), "s1")


class FakeDaemon:
    """Answers each run from a per-operation handler; remembers what it was asked."""

    def __init__(self):
        self.handlers = {}
        self.calls = []
        self.local_node = "entry-node"
        self.close_tab = AsyncMock()
        self.create_session = AsyncMock(return_value={"id": "throwaway"})
        self.delete_session = AsyncMock()
        self._json = AsyncMock()
        self.find_profile = AsyncMock(return_value={"name": "hrworks", "session_id": "s-1"})
        self.tabs = AsyncMock(return_value=[])

    def on(self, operation, handler):
        self.handlers[operation] = handler

    async def run(self, **kwargs):
        operation = next(n for n in browser_daemon.OPERATIONS if script(n) == kwargs["script"])
        self.calls.append((operation, kwargs))
        handler = self.handlers[operation]
        if isinstance(handler, Exception):
            raise handler
        return handler(kwargs) if callable(handler) else handler


def make(daemon, **kwargs):
    return BrowserDaemonClient({**SETTINGS, "profile_id": IDENTITY}, daemon=daemon, **kwargs)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = FakeDaemon()
        self.client = BrowserDaemonClient(SETTINGS, daemon=self.daemon)

    async def test_group_routes_login_and_cleanup_to_selected_node(self):
        self.daemon._json = AsyncMock(return_value={"node": "worker"})
        self.client = BrowserDaemonClient(
            {**SETTINGS, "browser_daemon_node_group": " cloud "}, daemon=self.daemon
        )
        self.daemon.on("login", ok({"authenticated": True}, page="TAB1"))
        self.daemon.on("logout", ok({"logged_out": True}))
        await self.client.login("example", "employee", PASSWORD)
        self.assertEqual(self.daemon.calls[0][1]["node"], "worker")
        self.daemon._json.assert_awaited_once_with(
            "POST",
            "/api/mesh/placement",
            json={"profile": "hrworks", "group": "cloud", "engine": "chromium", "create": True},
            timeout=180,
        )
        await self.client.logout()
        self.daemon.close_tab.assert_awaited_once_with("TAB1", node="worker")

    async def test_profile_move_discards_tab_from_previous_node(self):
        self.daemon._json = AsyncMock(side_effect=[{"node": "first"}, {"node": "second"}])
        self.client = BrowserDaemonClient(
            {**SETTINGS, "browser_daemon_node_group": "cloud"}, daemon=self.daemon
        )
        self.daemon.on("login", ok({"authenticated": True}, page="OLD_TAB"))
        await self.client.login("example", "employee", PASSWORD)
        self.daemon.on("login", ok({"authenticated": True}, page="NEW_TAB"))
        await self.client.login("example", "employee", PASSWORD)
        self.assertNotIn("tab", self.daemon.calls[-1][1])
        self.assertEqual(self.daemon.calls[-1][1]["node"], "second")
        self.assertEqual(self.client._tab, "NEW_TAB")

    async def test_group_placement_precedes_tab_adoption(self):
        self.daemon._json = AsyncMock(return_value={"node": "worker"})
        self.client = BrowserDaemonClient(
            {**SETTINGS, "browser_daemon_node_group": "cloud", "profile_id": IDENTITY},
            daemon=self.daemon,
        )
        self.daemon.find_profile.return_value = {
            "name": "hrworks",
            "session_id": "s-1",
            "node_name": "worker",
        }

        async def tabs(**kwargs):
            self.daemon._json.assert_awaited_once()
            return [{"id": "ADOPTED", "sessionId": "s-1", "url": "https://example.hrworks.de"}]

        self.daemon.tabs.side_effect = tabs
        self.daemon.on("account", ok({"metrics": {}, "period": None, "balance_basis": None}))
        await self.client.account()
        self.assertEqual(self.daemon.calls[-1][1]["tab"], "ADOPTED")
        self.assertEqual(self.daemon.calls[-1][1]["node"], "worker")

    async def test_group_refusal_does_not_run_on_another_node(self):
        self.daemon._json = AsyncMock(side_effect=BrowserDaemonError("no matching node", 409))
        self.client = BrowserDaemonClient(
            {**SETTINGS, "browser_daemon_node_group": "cloud"}, daemon=self.daemon
        )
        with self.assertRaises(WorkerError):
            await self.client.login("example", "employee", PASSWORD)
        self.assertEqual(self.daemon.calls, [])

    async def test_missing_named_profile_is_created_before_first_run(self):
        self.daemon.find_profile.return_value = None
        self.client = BrowserDaemonClient(
            {**SETTINGS, "browser_daemon_profile": "ha-hrworks"}, daemon=self.daemon
        )
        self.daemon.on("probe", ok())
        await self.client.health()
        self.daemon._json.assert_awaited_once_with(
            "POST",
            "/api/profiles",
            node=None,
            json={"name": "ha-hrworks", "engine": "chromium"},
        )
        self.assertEqual(self.daemon.calls[0][1]["profile"], "ha-hrworks")

    async def test_throwaway_login_and_mfa_keep_same_session_and_cleanup(self):
        self.client = BrowserDaemonClient(
            {**SETTINGS, "browser_daemon_profile": ""}, daemon=self.daemon
        )
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        await self.client.login("example", "employee", PASSWORD)
        await self.client.end_session()
        self.daemon.delete_session.assert_not_awaited()
        self.daemon.on("mfa", ok({"authenticated": True}, page="TAB1"))
        await self.client.mfa("123456")
        for _, call in self.daemon.calls:
            self.assertEqual(call["session_id"], "throwaway")
            self.assertNotIn("profile", call)
        self.daemon.create_session.assert_awaited_once()
        await self.client.end_session()
        self.daemon.delete_session.assert_awaited_once_with("throwaway", node=None)

    async def test_identity_is_a_hash_of_company_and_user(self):
        self.daemon.on("login", ok({"authenticated": True}))
        result = await self.client.login(" example ", "Employee", PASSWORD)
        self.assertEqual(result, {"profile_id": IDENTITY, "authenticated": True})
        self.assertEqual(self.client.profile, IDENTITY)

    async def test_credentials_travel_in_args_never_in_the_script(self):
        self.daemon.on("login", ok({"authenticated": True}))
        await self.client.login("example", "employee", PASSWORD, force=True)
        _, call = self.daemon.calls[0]
        self.assertEqual(
            call["args"],
            {"company": "example", "username": "employee", "password": PASSWORD, "force": True},
        )
        self.assertNotIn(PASSWORD, call["script"])
        self.assertEqual(call["profile"], "hrworks")
        self.assertTrue(call["keep_open"])

    async def test_pending_totp_continues_in_the_same_tab(self):
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        self.daemon.on("mfa", ok({"authenticated": True}))
        result = await self.client.login("example", "employee", PASSWORD)
        self.assertTrue(result["mfa_required"])
        self.assertEqual(
            await self.client.mfa("123456"), {"profile_id": IDENTITY, "authenticated": True}
        )
        _, call = self.daemon.calls[1]
        self.assertEqual((call["tab"], call["args"]), ("TAB1", {"code": "123456"}))
        with self.assertRaises(WorkerError) as err:  # the challenge is used up
            await self.client.mfa("123456")
        self.assertEqual(err.exception.code, "invalid_code")

    async def test_bad_codes_never_reach_the_portal(self):
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        await self.client.login("example", "employee", PASSWORD)
        for code in ("12345", "abcdef", ""):
            with self.assertRaises(WorkerError) as err:
                await self.client.mfa(code)
            self.assertEqual(err.exception.code, "invalid_code")
        self.assertEqual([c[0] for c in self.daemon.calls], ["login"])

    async def test_rejected_code_keeps_the_tab_for_another_try(self):
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        self.daemon.on("mfa", failed("hrworks:invalid_code"))
        await self.client.login("example", "employee", PASSWORD)
        with self.assertRaises(WorkerError) as err:
            await self.client.mfa("123456")
        self.assertEqual(err.exception.code, "invalid_code")
        self.daemon.on("mfa", ok({"authenticated": True}))
        await self.client.mfa("654321")
        self.assertEqual(self.daemon.calls[-1][1]["tab"], "TAB1")

    async def test_challenge_expires_after_ten_minutes(self):
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        await self.client.login("example", "employee", PASSWORD)
        self.client._mfa_started -= 601
        with self.assertRaises(WorkerError) as err:
            await self.client.mfa("123456")
        self.assertEqual(err.exception.code, "invalid_code")

    async def test_a_new_login_reuses_the_portal_tab(self):
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        await self.client.login("example", "employee", PASSWORD)
        await self.client.login("example", "employee", PASSWORD, force=True)
        self.assertEqual(self.daemon.calls[1][1]["tab"], "TAB1")
        self.daemon.close_tab.assert_not_awaited()

    async def test_the_tab_that_signed_in_serves_every_later_run(self):
        self.daemon.on("login", ok({"authenticated": True}, page="TAB1"))
        self.daemon.on("snapshot", ok(raw_snapshot(datetime.now(BERLIN).date())))
        self.daemon.on("day", ok({"metrics": {}, "entries": []}))
        self.daemon.on("account", ok({"metrics": {}, "period": None, "balance_basis": None}))
        await self.client.login("example", "employee", PASSWORD)
        await self.client.snapshot(ReadTests.options)
        await self.client.day("2026-03-12")
        await self.client.account()
        for operation, call in self.daemon.calls[1:]:
            self.assertEqual((call["tab"], call["keep_open"]), ("TAB1", True), operation)
        self.daemon.close_tab.assert_not_awaited()
        await self.client.close()  # the logged-in tab is the point: it stays
        self.daemon.close_tab.assert_not_awaited()

    async def test_a_failed_login_leaves_no_tab_behind(self):
        self.daemon.on("login", failed("hrworks:invalid_auth"))
        with self.assertRaises(WorkerError):
            await self.client.login("example", "employee", PASSWORD)
        self.assertIsNone(self.client._tab)

    async def test_a_vanished_tab_expires_the_session_and_login_makes_a_new_one(self):
        self.daemon.on("login", ok({"authenticated": True}, page="TAB1"))
        await self.client.login("example", "employee", PASSWORD)
        self.daemon.on("snapshot", failed("tab TAB1 not found in the session"))
        with self.assertRaises(WorkerError) as err:
            await self.client.snapshot(ReadTests.options)
        self.assertEqual(err.exception.code, "session_expired")
        self.assertIsNone(self.client._tab)
        self.daemon.on("login", ok({"authenticated": True}, page="TAB2"))
        await self.client.login("example", "employee", PASSWORD, force=True)
        self.assertNotIn("tab", self.daemon.calls[-1][1])
        self.assertEqual(self.client._tab, "TAB2")

    async def test_login_replaces_a_stale_tab_itself(self):
        self.daemon.on("login", ok({"authenticated": True}, page="TAB1"))
        await self.client.login("example", "employee", PASSWORD)
        answers = [
            failed("tab TAB1 not found in the session"),
            ok({"authenticated": True}, page="TAB2"),
        ]

        def login(_kwargs):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        self.daemon.on("login", login)
        await self.client.login("example", "employee", PASSWORD, force=True)
        self.assertEqual(self.client._tab, "TAB2")
        self.assertEqual(len(answers), 0)

    async def test_logout_closes_the_portal_tab(self):
        self.daemon.on("login", ok({"authenticated": True}, page="TAB1"))
        self.daemon.on("logout", ok({"logged_out": True}))
        await self.client.login("example", "employee", PASSWORD)
        await self.client.logout()
        self.daemon.close_tab.assert_awaited_once_with("TAB1", node=None)
        self.assertIsNone(self.client._tab)

    async def test_after_a_restart_the_profiles_portal_tab_is_taken_over(self):
        self.daemon.tabs = AsyncMock(
            return_value=[
                {"id": "X", "sessionId": "s-1", "type": "page", "url": "https://example.org/"},
                {
                    "id": "P",
                    "sessionId": "s-1",
                    "type": "page",
                    "url": "https://ssl6.hrworks.de/o/x",
                },
            ]
        )
        self.daemon.on("day", ok({"metrics": {}, "entries": []}))
        client = make(self.daemon)
        await client.day("2026-03-12")
        self.assertEqual(self.daemon.calls[0][1]["tab"], "P")

    async def test_a_vanished_tab_means_the_login_must_restart(self):
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        self.daemon.on("mfa", failed("tab TAB1 not found in the session"))
        await self.client.login("example", "employee", PASSWORD)
        with self.assertRaises(WorkerError) as err:
            await self.client.mfa("123456")
        self.assertEqual(err.exception.code, "session_expired")

    async def test_saved_totp_completes_the_login_through_the_shared_helper(self):
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        self.daemon.on("mfa", ok({"authenticated": True}))
        settings = {
            **SETTINGS,
            "company_id": "example",
            "username": "employee",
            "password": PASSWORD,
            "totp_uri": URI,
        }
        result = await authenticate(self.client, settings, force=True)
        self.assertTrue(result["authenticated"])
        code = self.daemon.calls[1][1]["args"]["code"]
        self.assertTrue(code.isdigit() and len(code) == 6)

    async def test_manual_code_completes_through_complete_mfa(self):
        self.daemon.on("login", ok({"mfa_required": True}, page="TAB1"))
        self.daemon.on("mfa", ok({"authenticated": True}))
        await self.client.login("example", "employee", PASSWORD)
        await complete_mfa(self.client, SETTINGS, {"code": " 123456 "})
        self.assertEqual(self.daemon.calls[1][1]["args"], {"code": "123456"})

    async def test_blank_credentials_are_refused_before_any_run(self):
        for values in (("", "u", "p"), ("c", " ", "p"), ("c", "u", "")):
            with self.assertRaises(WorkerError) as err:
                await self.client.login(*values)
            self.assertEqual(err.exception.code, "invalid_request")
        self.assertEqual(self.daemon.calls, [])

    async def test_an_unexpected_login_answer_is_a_portal_change(self):
        self.daemon.on("login", ok({}))
        with self.assertRaises(WorkerError) as err:
            await self.client.login("example", "employee", PASSWORD)
        self.assertEqual(err.exception.code, "portal_changed")


class ErrorTests(unittest.IsolatedAsyncioTestCase):
    async def code(self, error, *, write=False):
        daemon = FakeDaemon()
        daemon.on("account", error)
        client = make(daemon)
        with self.assertRaises(WorkerError) as err:
            await client._run("account", write=write)
        return err.exception.code

    async def test_portal_codes_pass_through_and_unknown_ones_do_not(self):
        self.assertEqual(await self.code(failed("hrworks:session_expired")), "session_expired")
        self.assertEqual(await self.code(failed("hrworks:invalid_auth")), "invalid_auth")
        self.assertEqual(await self.code(failed("hrworks:secret leak")), "portal_changed")

    async def test_browser_failures_are_transient(self):
        self.assertEqual(
            await self.code(failed("locator.click: Timeout 20000ms exceeded.")),
            "browser_unavailable",
        )
        self.assertEqual(
            await self.code(failed("timed out", timed_out=True)), "browser_unavailable"
        )

    async def test_script_bugs_are_not_retried_as_outages(self):
        self.assertEqual(
            await self.code(failed("TypeError: x is not a function")), "portal_changed"
        )

    async def test_daemon_refusals(self):
        self.assertEqual(await self.code(BrowserDaemonError("unreachable")), "cannot_connect")
        self.assertEqual(await self.code(BrowserDaemonError("no", 401)), "worker_auth")
        self.assertEqual(await self.code(BrowserDaemonError("no", 403)), "worker_auth")
        self.assertEqual(
            await self.code(BrowserDaemonError("no profile", 404)), "browser_unavailable"
        )
        self.assertEqual(await self.code(BrowserDaemonError("no runs", 501)), "incompatible_worker")

    async def test_error_text_never_reaches_the_code(self):
        self.assertEqual(
            await self.code(failed("hrworks:browser PASSWORD=hunter2")), "portal_changed"
        )

    async def test_a_write_which_may_have_started_is_uncertain(self):
        self.assertEqual(
            await self.code(BrowserDaemonError("unreachable"), write=True), "write_uncertain"
        )
        self.assertEqual(
            await self.code(failed("timed out", timed_out=True), write=True), "write_uncertain"
        )
        self.assertEqual(
            await self.code(failed("hrworks:write_uncertain"), write=True), "write_uncertain"
        )

    async def test_a_refused_write_is_not_uncertain(self):
        self.assertEqual(await self.code(BrowserDaemonError("no", 403), write=True), "worker_auth")
        self.assertEqual(await self.code(failed("hrworks:overlap"), write=True), "overlap")


def raw_snapshot(today, **changes):
    day = today.isoformat()
    event = lambda uid, start, end, approved=True, status="Approved": {  # noqa: E731
        "uid": uid,
        "kind": "leave",
        "summary": "Vacation",
        "start": start,
        "end": end,
        "approved": approved,
        "status": status,
        "half_day": False,
        "description": status,
    }
    far = (today + timedelta(days=900)).isoformat()
    raw = {
        "account": {
            "metrics": {"monthly_worked": 600, "total_balance": 90},
            "period": today.strftime("%Y-%m"),
            "balance_basis": "end_of_month",
        },
        "lists": [
            {
                "year": today.year,
                "kind": "leave",
                "vacation": {"vacation_remaining": 12.5},
                "events": [
                    event("hrworks-leave-1", day, day),
                    event("hrworks-leave-2", day, day, False, "Applied for"),
                    event("hrworks-leave-3", day, day, False, "Rejected"),
                    event("hrworks-leave-4", far, far),
                ],
            },
            {
                "year": today.year - 1,
                "kind": "leave",
                "vacation": {"vacation_remaining": 3},
                "events": [],
            },
        ],
        "today": {
            "metrics": {"today_worked": 120},
            "entries": [{"start": "08:00", "end": "", "type": "Working time", "comment": "x"}],
        },
        "clock": None,
        "timings": {"account": 1.234, "events": 2.0, "today": 0.5},
        "browser_version": "150.0",
    }
    raw.update(changes)
    return raw


class ReadTests(unittest.IsolatedAsyncioTestCase):
    options = {
        "calendar_past_days": 30,
        "calendar_future_days": 30,
        "include_sickness": True,
        "include_pending": False,
    }

    def setUp(self):
        self.daemon = FakeDaemon()
        self.client = make(self.daemon)
        self.today = datetime.now(BERLIN).date()

    async def test_snapshot_matches_the_workers_shape(self):
        self.daemon.on("snapshot", ok(raw_snapshot(self.today)))
        data = await self.client.snapshot(self.options)
        self.assertEqual([e["uid"] for e in data["events"]], ["hrworks-leave-1"])
        self.assertEqual(
            data["metrics"],
            {
                "monthly_worked": 600,
                "total_balance": 90,
                "vacation_remaining": 12.5,
                "today_worked": 120,
            },
        )
        self.assertEqual(data["today"], {"date": self.today.isoformat()})
        self.assertTrue(data["clocked_in"])
        self.assertEqual(data["today_entries"], [{"start": "08:00", "type": "Working time"}])
        self.assertEqual(data["account"]["current_month"], True)
        self.assertEqual(data["timings"]["account"], 1.23)
        self.assertIn("total", data["timings"])
        report = data["diagnostics"]
        self.assertEqual(report["backend"], "browser-daemon")
        self.assertEqual(report["browser_session"], "s-1")
        self.assertEqual(report["browser_endpoint"], "daemon.invalid:3001")
        self.assertEqual(report["browser_version"], "150.0")
        self.assertNotIn("cluster-secret", json.dumps(data))
        _, call = self.daemon.calls[0]
        self.assertEqual(
            call["args"]["years"],
            sorted(
                {
                    self.today.year,
                    (self.today - timedelta(days=30)).year,
                    (self.today + timedelta(days=31)).year,
                }
            ),
        )
        self.assertEqual(call["args"]["kinds"], ["leave", "sickness"])

    async def test_pending_leave_only_when_asked_for(self):
        self.daemon.on("snapshot", ok(raw_snapshot(self.today)))
        data = await self.client.snapshot({**self.options, "include_pending": True})
        self.assertEqual([e["uid"] for e in data["events"]], ["hrworks-leave-1", "hrworks-leave-2"])

    async def test_the_clock_button_overrides_the_entries(self):
        self.daemon.on("snapshot", ok(raw_snapshot(self.today, clock="in_available")))
        self.assertFalse((await self.client.snapshot(self.options))["clocked_in"])
        self.daemon.on("snapshot", ok(raw_snapshot(self.today, clock="out_available")))
        self.assertTrue((await self.client.snapshot(self.options))["clocked_in"])

    async def test_sickness_can_be_left_out(self):
        self.daemon.on("snapshot", ok(raw_snapshot(self.today)))
        await self.client.snapshot({**self.options, "include_sickness": False})
        self.assertEqual(self.daemon.calls[0][1]["args"]["kinds"], ["leave"])

    async def test_bad_options_and_missing_login(self):
        with self.assertRaises(WorkerError) as err:
            await self.client.snapshot({**self.options, "calendar_past_days": 9999})
        self.assertEqual(err.exception.code, "invalid_request")
        with self.assertRaises(WorkerError) as err:
            await BrowserDaemonClient(SETTINGS, daemon=self.daemon).snapshot(self.options)
        self.assertEqual(err.exception.code, "session_expired")
        self.assertEqual(self.daemon.calls, [])

    async def test_a_malformed_answer_is_a_portal_change(self):
        self.daemon.on("snapshot", ok({"account": {}}))
        with self.assertRaises(WorkerError) as err:
            await self.client.snapshot(self.options)
        self.assertEqual(err.exception.code, "portal_changed")

    async def test_day(self):
        answer = {"metrics": {"today_worked": 60}, "entries": [{"start": "08:00", "end": "09:00"}]}
        self.daemon.on("day", ok(answer))
        data = await self.client.day("2026-03-12", reuse=True)
        self.assertEqual(data["date"], "2026-03-12")
        self.assertEqual(data["metrics"], {"today_worked": 60})
        self.assertEqual(self.daemon.calls[0][1]["args"], {"date": "2026-03-12"})
        with self.assertRaises(WorkerError):
            await self.client.day("tomorrow")

    async def test_account_without_data(self):
        self.daemon.on("account", ok({"metrics": {}, "period": None, "balance_basis": None}))
        data = await self.client.account()
        self.assertEqual(data["account"], {"period": None, "current_month": False})

    async def test_previous_day_balance_names_its_date(self):
        raw = {
            "metrics": {"total_balance": 5},
            "period": self.today.strftime("%Y-%m"),
            "balance_basis": "previous_day",
        }
        self.daemon.on("account", ok(raw))
        account = (await self.client.account())["account"]
        self.assertEqual(account["as_of"], (self.today - timedelta(days=1)).isoformat())

    async def test_health_checks_the_browser(self):
        self.daemon.on("probe", ok({"browser_version": "1"}))
        result = await self.client.health()
        self.assertEqual(result["protocol_version"], 1)
        self.assertIn("day", result["capabilities"])
        self.assertFalse(result["writes_enabled"])

    async def test_logout_drops_the_cookies(self):
        self.daemon.on("logout", ok({"logged_out": True}))
        self.assertEqual(await self.client.logout(), {"logged_out": True})
        self.assertEqual(
            await BrowserDaemonClient(SETTINGS, daemon=self.daemon).logout(), {"logged_out": True}
        )
        self.assertEqual(len(self.daemon.calls), 1)

    async def test_runs_are_serialized(self):
        running = peak = 0

        async def run(**_kwargs):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await __import__("asyncio").sleep(0.01)
            running -= 1
            return ok({"metrics": {}, "entries": []})

        self.daemon.run = run
        import asyncio

        await asyncio.gather(*(self.client.day("2026-03-12") for _ in range(4)))
        self.assertEqual(peak, 1)


class RecordTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.daemon = FakeDaemon()
        day = (datetime.now(BERLIN) - timedelta(days=2)).date().isoformat()
        self.day = day
        self.data = {"start": f"{day}T08:00:00+02:00", "end": f"{day}T12:00:00+02:00"}
        self.daemon.on("record", lambda kw: ok({"saved": False, "dry_run": kw["args"]["dry_run"]}))

    async def test_preview_is_the_default_and_sends_prepared_arguments(self):
        client = make(self.daemon)
        result = await client.record({**self.data, "comment": "c", "type": "business_errand"})
        self.assertTrue(result["dry_run"])
        args = self.daemon.calls[0][1]["args"]
        self.assertEqual(args["date"], self.day)
        start = datetime.fromisoformat(self.data["start"]).astimezone(BERLIN)
        self.assertEqual(
            (args["start"], args["end"]),
            (start.strftime("%H:%M"), (start + timedelta(hours=4)).strftime("%H:%M")),
        )
        self.assertEqual(args["labels"], ["Business errand", "Dienstgang"])
        self.assertTrue(args["dry_run"])

    async def test_a_real_write_needs_the_write_switch(self):
        client = make(self.daemon)
        with self.assertRaises(WorkerError) as err:
            await client.record({**self.data, "dry_run": False})
        self.assertEqual(err.exception.code, "writes_disabled")
        self.assertEqual(self.daemon.calls, [])

    async def test_a_write_goes_through_when_enabled_and_is_never_replayed(self):
        client = make(self.daemon, allow_writes=True)
        self.daemon.on("record", BrowserDaemonError("unreachable"))
        with self.assertRaises(WorkerError) as err:
            await client.record({**self.data, "dry_run": False})
        self.assertEqual(err.exception.code, "write_uncertain")
        self.assertEqual(len(self.daemon.calls), 1)

    async def test_the_switch_does_not_gate_previews(self):
        await make(self.daemon).record({**self.data, "dry_run": True})
        self.assertEqual(len(self.daemon.calls), 1)

    async def test_invalid_intervals_never_reach_the_portal(self):
        day, now = self.day, datetime.now(BERLIN)
        cases = [
            {"start": f"{day}T08:00:00", "end": f"{day}T09:00:00"},  # naive
            {"start": f"{day}T09:00:00+02:00", "end": f"{day}T08:00:00+02:00"},
            {"start": f"{day}T08:00:30+02:00", "end": f"{day}T09:00:00+02:00"},
            {"start": f"{day}T23:00:00+02:00", "end": f"{day}T23:30:00+02:00", "type": "holiday"},
            {
                "start": now.isoformat(timespec="minutes"),
                "end": (now + timedelta(hours=1)).isoformat(timespec="minutes"),
            },  # future
            {**self.data, "comment": "x" * 501},
            {"end": f"{day}T09:00:00+02:00"},
        ]
        client = make(self.daemon, allow_writes=True)
        for case in cases:
            with self.assertRaises(WorkerError) as err:
                await client.record({**case, "dry_run": False})
            self.assertEqual(err.exception.code, "invalid_interval", case)
        self.assertEqual(self.daemon.calls, [])

    async def test_portal_verdicts_pass_through(self):
        client = make(self.daemon, allow_writes=True)
        self.daemon.on("record", failed("hrworks:overlap"))
        with self.assertRaises(WorkerError) as err:
            await client.record({**self.data, "dry_run": False})
        self.assertEqual(err.exception.code, "overlap")


@unittest.skipUnless(shutil.which("node"), "node is needed to check the scripts")
class ScriptTests(unittest.TestCase):
    def node(self, code, *args):
        done = subprocess.run(
            ["node", "-e", code, *args], capture_output=True, text=True, check=False, timeout=30
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_every_operation_is_a_function_expression_node_accepts(self):
        check = "(0, eval)('(' + process.argv[1] + ')');"
        for name in browser_daemon.OPERATIONS:
            with self.subTest(name):
                self.node(check, script(name))

    def test_helpers_parse_the_portal_the_way_the_old_worker_did(self):
        rows = [
            ["Target hours", "160:00"],
            ["Recorded hours (Working hours)", "33:44 (33:35)"],
            ["Total balance", "−12:30"],
            ["Monthly balance to the previous day", "1:05"],
            ["Total balance to the previous day", "-3:00 Hours"],
            ["Übertrag aus Vormonat", "2:00"],
            ["Nonsense", "x"],
        ]
        texts = ["12:30", "−0:05", "-7:59 Stunden", "1:75", "abc", "10:00 Hours"]
        expected = {
            "metrics": {
                "monthly_target": 9600,
                "monthly_worked": 2015,
                "carryover": 120,
                "monthly_balance": 65,
                "total_balance": -180,
            },
            "basis": "previous_day",
            "minutes": [750, -5, -479, None, None, 600],
            "dates": ["2026-02-01", "2026-03-05"],
        }
        helpers = browser_daemon.HELPERS
        code = (
            f"{helpers}\n"
            "const input = JSON.parse(process.argv[1]);\n"
            "const [metrics, basis] = accountMetrics(input.rows);\n"
            "console.log(JSON.stringify({metrics, basis, minutes: input.texts.map(minutes),"
            " dates: dates('01.02.2026 - 05.03.26')}));"
        )
        got = json.loads(self.node(code, json.dumps({"rows": rows, "texts": texts})))
        self.assertEqual(got, expected)

    def test_scripts_hold_no_credentials(self):
        for name in browser_daemon.OPERATIONS:
            self.assertNotIn("password: '", script(name))


if __name__ == "__main__":
    unittest.main()
