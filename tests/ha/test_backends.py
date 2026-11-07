"""Backend choice in the config flow and in create_client, with fake clients only."""

import sys
import unittest
from datetime import timedelta
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from browser_daemon_client import RunError, RunResult
from homeassistant.exceptions import ConfigEntryError

from custom_components.hrworks import api, async_migrate_entry, async_setup_entry, config_flow
from custom_components.hrworks.api import WorkerError, create_client
from custom_components.hrworks.config_flow import HrworksConfigFlow
from custom_components.hrworks.coordinator import HrworksCoordinator
from custom_components.hrworks.hrworks.browser_daemon import (
    CLIENT_NAME,
    BrowserDaemonClient,
    script,
)

ACCOUNT = {
    "company_id": "example",
    "username": "employee",
    "password": "test-password",
    "totp_uri": "otpauth://totp/Example:test?secret=JBSWY3DPEHPK3PXP",
    "profile_id": "a" * 64,
}


def flow(data):
    result = HrworksConfigFlow()
    result.hass = MagicMock()
    result.context = {"source": "reconfigure"}
    result._entry = SimpleNamespace(data=dict(data), title="HR WORKS")
    result._get_reconfigure_entry = lambda: result._entry
    result._data = dict(data)
    result.async_step_account = AsyncMock(return_value={"type": "account"})
    return result


def fake_client():
    return SimpleNamespace(health=AsyncMock(return_value={}), close=AsyncMock())


BD = {
    "browser_daemon_url": "https://bd.example",
    "browser_daemon_secret": "cluster",
    "browser_daemon_profile": "hrworks",
    "name": "Work",
}


class FlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_and_reconfigure_go_straight_to_the_connection_form(self):
        setup = HrworksConfigFlow()
        setup.hass = MagicMock()
        self.assertEqual((await setup.async_step_user())["step_id"], "user")
        result = await flow(ACCOUNT).async_step_reconfigure()
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertEqual(HrworksConfigFlow.VERSION, 2)

    async def test_connection_keeps_the_employee_login(self):
        flow_ = flow(ACCOUNT)
        client = fake_client()
        with patch.object(config_flow, "create_client", return_value=client) as create:
            await flow_.async_step_reconfigure(BD)
        self.assertEqual(create.call_args.args[1]["browser_daemon_url"], "https://bd.example")
        client.health.assert_awaited_once()
        for key in ("company_id", "username", "password", "totp_uri", "profile_id"):
            self.assertEqual(flow_._data[key], ACCOUNT[key])
        self.assertEqual(flow_._data["browser_daemon_secret"], "cluster")
        self.assertNotIn("backend", flow_._data)
        self.assertEqual(flow_._title, "Work")
        flow_.async_step_account.assert_awaited_once()

    async def test_blank_secret_keeps_the_saved_one(self):
        flow_ = flow({**ACCOUNT, "browser_daemon_secret": "kept"})
        with patch.object(config_flow, "create_client", return_value=fake_client()):
            await flow_.async_step_reconfigure(
                {"browser_daemon_url": "http://bd", "browser_daemon_profile": "p"}
            )
        self.assertEqual(flow_._data["browser_daemon_secret"], "kept")

    async def test_empty_url_stores_no_secret(self):
        flow_ = flow({**ACCOUNT, "browser_daemon_secret": "old"})
        with patch.object(config_flow, "create_client", return_value=fake_client()):
            await flow_.async_step_reconfigure(
                {
                    "browser_daemon_url": "",
                    "browser_daemon_secret": "typed",
                    "browser_daemon_profile": "p",
                }
            )
        self.assertEqual(flow_._data["browser_daemon_url"], "")
        self.assertIsNone(flow_._data["browser_daemon_secret"])

    async def test_selection_clears_a_previous_override(self):
        """Hidden fields are not submitted, so the checkbox decides."""
        flow_ = flow(
            {**ACCOUNT, "browser_daemon_url": "https://old", "browser_daemon_secret": "old"}
        )
        with patch.object(config_flow, "create_client", return_value=fake_client()):
            await flow_.async_step_reconfigure(
                {
                    "browser_daemon_override": False,
                    "browser_daemon_entry_id": "bd2",
                    "browser_daemon_url": "",
                    "browser_daemon_profile": "p",
                }
            )
        self.assertEqual(flow_._data["browser_daemon_entry_id"], "bd2")
        self.assertEqual(flow_._data["browser_daemon_url"], "")
        self.assertIsNone(flow_._data["browser_daemon_secret"])
        self.assertNotIn("browser_daemon_override", flow_._data)

    async def test_override_clears_the_selection(self):
        flow_ = flow({**ACCOUNT, "browser_daemon_entry_id": "bd2"})
        with patch.object(config_flow, "create_client", return_value=fake_client()):
            await flow_.async_step_reconfigure(
                {**BD, "browser_daemon_override": True, "browser_daemon_url": "https://bd.example"}
            )
        self.assertEqual(flow_._data["browser_daemon_entry_id"], "")
        self.assertEqual(flow_._data["browser_daemon_url"], "https://bd.example")

    async def test_override_needs_a_url(self):
        flow_ = flow(ACCOUNT)
        with patch.object(config_flow, "create_client") as create:
            result = await flow_.async_step_reconfigure(
                {
                    "browser_daemon_override": True,
                    "browser_daemon_secret": "s",
                    "browser_daemon_profile": "p",
                }
            )
        self.assertEqual(result["errors"], {"base": "invalid_request"})
        create.assert_not_called()

    async def test_form_defaults_to_an_enabled_integration_with_the_override_off(self):
        setup = HrworksConfigFlow()
        setup.hass = MagicMock()
        daemon = SimpleNamespace(entry_id="bd1", title="Browser Daemon (a)", disabled_by=None)
        disabled = SimpleNamespace(entry_id="off", title="Off", disabled_by="user")
        setup.hass.config_entries.async_entries.return_value = [daemon, disabled]
        schema = (await setup.async_step_user())["data_schema"].schema
        markers = {key.schema: key for key in schema}
        picker = schema[markers["browser_daemon_entry_id"]]
        self.assertEqual(picker.config["integration"], "browser_daemon")
        self.assertEqual(markers["browser_daemon_entry_id"].default(), "bd1")
        self.assertIs(markers["browser_daemon_override"].default(), False)
        override = {"field": "browser_daemon_override", "value": True}
        self.assertEqual(markers["browser_daemon_url"].visible, override)
        self.assertEqual(markers["browser_daemon_secret"].visible, override)

    async def test_form_without_integration_only_offers_the_override(self):
        setup = HrworksConfigFlow()
        setup.hass = MagicMock()
        setup.hass.config_entries.async_entries.return_value = []
        schema = (await setup.async_step_user())["data_schema"].schema
        markers = {key.schema: key for key in schema}
        self.assertNotIn("browser_daemon_entry_id", markers)
        self.assertIs(markers["browser_daemon_override"].default(), True)

    async def test_url_without_secret_is_invalid(self):
        flow_ = flow(ACCOUNT)
        with patch.object(config_flow, "create_client") as create:
            result = await flow_.async_step_reconfigure(
                {"browser_daemon_url": "https://bd.example", "browser_daemon_profile": "p"}
            )
        self.assertEqual(result["errors"], {"base": "invalid_request"})
        create.assert_not_called()

    async def test_url_needs_a_web_scheme(self):
        result = await flow(ACCOUNT).async_step_reconfigure(
            {
                "browser_daemon_url": "ftp://bd",
                "browser_daemon_secret": "s",
                "browser_daemon_profile": "p",
            }
        )
        self.assertEqual(result["errors"], {"base": "invalid_request"})

    async def test_missing_browser_daemon_integration_is_cannot_connect(self):
        with patch.dict(sys.modules, {"custom_components.browser_daemon": None}):
            result = await flow(ACCOUNT).async_step_reconfigure(
                {"browser_daemon_url": "", "browser_daemon_profile": "p"}
            )
        self.assertEqual(result["errors"], {"base": "cannot_connect"})


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def entry(self, data, version=1):
        return SimpleNamespace(version=version, data=data, entry_id="e", title="HR WORKS")

    def hass(self):
        hass = MagicMock()
        hass.config_entries.async_update_entry = MagicMock()
        return hass

    async def test_ssh_keys_are_dropped_and_the_login_kept(self):
        old = {
            **ACCOUNT,
            "backend": "browser_daemon",
            "browser_daemon_profile": "hrworks",
            "ssh_host": "h",
            "ssh_port": 22,
            "ssh_username": "u",
            "ssh_key_path": "/k",
            "ssh_known_hosts": "x",
            "worker_command": "hrworks worker",
        }
        hass = self.hass()
        self.assertTrue(await async_migrate_entry(hass, self.entry(old)))
        kwargs = hass.config_entries.async_update_entry.call_args.kwargs
        self.assertEqual((kwargs["version"], kwargs["minor_version"]), (2, 1))
        self.assertEqual(kwargs["data"], {**ACCOUNT, "browser_daemon_profile": "hrworks"})

    async def test_current_and_future_versions(self):
        hass = self.hass()
        self.assertTrue(await async_migrate_entry(hass, self.entry({}, version=2)))
        hass.config_entries.async_update_entry.assert_not_called()
        self.assertFalse(await async_migrate_entry(hass, self.entry({}, version=3)))

    async def test_an_entry_which_used_ssh_asks_for_reconfiguration(self):
        ssh = {**ACCOUNT, "backend": "ssh", "ssh_host": "h"}
        hass = self.hass()
        entry = self.entry(ssh)
        await async_migrate_entry(hass, entry)
        migrated = hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertNotIn("ssh_host", migrated)
        entry.data = migrated
        with patch("custom_components.hrworks.create_issue") as issue:
            with self.assertRaises(ConfigEntryError):
                await async_setup_entry(hass, entry)
        self.assertEqual(issue.call_args.args[-1], "reconfigure_required")


class CreateClientTests(unittest.TestCase):
    def shared(self):
        module = ModuleType("custom_components.browser_daemon")
        module.BrowserDaemonError = type("BrowserDaemonError", (Exception,), {})
        module.async_get_client = MagicMock()
        module.async_get_daemon = MagicMock(return_value=SimpleNamespace(url="http://shared:3001"))
        return patch.dict(sys.modules, {"custom_components.browser_daemon": module}), module

    def settings(self, **extra):
        return {"browser_daemon_profile": "hrworks", **extra}

    def test_blank_url_uses_the_selected_integrations_daemon(self):
        patcher, module = self.shared()
        settings = self.settings(browser_daemon_url="", browser_daemon_entry_id="bd2")
        hass = MagicMock()
        with patcher:
            client = create_client(hass, settings)
        module.async_get_daemon.assert_called_once_with(hass, client=CLIENT_NAME, entry_id="bd2")
        self.assertIs(client._client(), module.async_get_daemon.return_value)
        # The cluster secret stays in the Browser Daemon integration.
        self.assertNotIn("browser_daemon_secret", client.settings)
        self.assertEqual(client.settings["browser_daemon_url"], "http://shared:3001")
        self.assertEqual(settings["browser_daemon_url"], "")

    def test_an_override_ignores_the_integration(self):
        patcher, module = self.shared()
        settings = self.settings(browser_daemon_url="https://own", browser_daemon_secret="own")
        with patcher, patch.object(api, "async_get_clientsession", return_value="http"):
            client = create_client(MagicMock(), settings)
        module.async_get_daemon.assert_not_called()
        self.assertEqual(client.settings["browser_daemon_secret"], "own")

    def test_missing_integration_is_cannot_connect(self):
        with patch.dict(sys.modules, {"custom_components.browser_daemon": None}):
            with self.assertRaises(WorkerError) as err:
                create_client(MagicMock(), self.settings())
        self.assertEqual(err.exception.code, "cannot_connect")

    def test_unavailable_integration_is_cannot_connect(self):
        patcher, module = self.shared()
        module.async_get_daemon.side_effect = module.BrowserDaemonError("gone")
        with patcher, self.assertRaises(WorkerError) as err:
            create_client(MagicMock(), self.settings())
        self.assertEqual(err.exception.code, "cannot_connect")


class FakeDaemon:
    """Answers runs by operation: snapshots first fail as expired, then succeed."""

    def __init__(self):
        self.operations = []
        self.snapshots = 0
        self.args = {}

    async def run(self, **kwargs):
        from custom_components.hrworks.hrworks import browser_daemon

        operation = next(n for n in browser_daemon.OPERATIONS if script(n) == kwargs["script"])
        self.operations.append(operation)
        self.args[operation] = kwargs["args"]
        if operation == "snapshot":
            self.snapshots += 1
            if self.snapshots == 1:
                raise RunError(RunResult(ok=False, error="hrworks:session_expired"), 422)
            return RunResult(
                ok=True,
                result={
                    "account": {"metrics": {}, "period": None, "balance_basis": None},
                    "lists": [],
                    "today": {"metrics": {}, "entries": []},
                    "clock": None,
                    "timings": {},
                },
            )
        if operation == "login":
            return RunResult(ok=True, result={"mfa_required": True}, page="TAB")
        return RunResult(ok=True, result={"authenticated": True})

    async def close_tab(self, _tab):
        pass

    async def create_session(self, **_kwargs):
        return {"id": "throwaway-session"}

    async def delete_session(self, _session, node=None):
        pass

    async def find_profile(self, _name):
        return {"session_id": "s-1"}

    async def tabs(self, node=None):
        return []


class RecoveryWithBrowserDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_session_logs_in_again_with_the_saved_login_and_totp(self):
        data = {**ACCOUNT, "backend": "browser_daemon", "browser_daemon_profile": "hrworks"}
        daemon = FakeDaemon()
        client = BrowserDaemonClient(data, daemon=daemon)
        coordinator = object.__new__(HrworksCoordinator)
        coordinator.entry = SimpleNamespace(entry_id="e", options={}, data=data)
        coordinator.client = client
        coordinator.hass = MagicMock()
        coordinator.hass.data = {}
        coordinator.last_error = coordinator.last_error_stage = coordinator.last_error_detail = None
        coordinator._auth_recovery_blocked = False
        coordinator._normal_update_interval = timedelta(minutes=15)
        coordinator.update_interval = coordinator._normal_update_interval
        coordinator._work_days = AsyncMock(return_value=[])
        coordinator.update_count = coordinator.failed_updates = 0
        coordinator.consecutive_failures = coordinator.reauthentications = 0
        coordinator.last_duration = coordinator.last_finished = None
        with patch("custom_components.hrworks.coordinator.clear_issues"):
            result = await coordinator._async_update_data()
        self.assertEqual(daemon.operations, ["snapshot", "login", "mfa", "snapshot"])
        self.assertEqual(daemon.args["login"]["password"], "test-password")
        self.assertTrue(daemon.args["login"]["force"])
        self.assertRegex(daemon.args["mfa"]["code"], r"^\d{6}$")
        self.assertEqual(coordinator.reauthentications, 1)
        self.assertEqual(result["work_days"], [])
        self.assertEqual(result["diagnostics"]["backend"], "browser-daemon")

    async def test_login_timeout_keeps_its_stage_for_diagnostics(self):
        daemon = FakeDaemon()

        async def run(**_kwargs):
            raise RunError(RunResult(ok=False, error="hrworks:login_timeout"), 422)

        daemon.run = run
        client = BrowserDaemonClient({**ACCOUNT, "backend": "browser_daemon"}, daemon=daemon)
        with self.assertRaises(WorkerError) as err:
            await client.login("example", "employee", "pw")
        self.assertEqual(
            (err.exception.code, err.exception.stage, err.exception.detail),
            ("login_timeout", "browser_operation", "timeout"),
        )


if __name__ == "__main__":
    unittest.main()
