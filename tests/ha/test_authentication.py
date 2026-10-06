"""Exercise real HA flow classes with a fake worker, never real HR records."""

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.hrworks.api import WorkerClient, WorkerError
from custom_components.hrworks.auth import secret_settings
from custom_components.hrworks.config_flow import HrworksConfigFlow, account_schema
from custom_components.hrworks.coordinator import HrworksCoordinator
from custom_components.hrworks.repairs import EmployeeLoginRepair
from custom_components.hrworks.totp import InvalidTotp

URI = "otpauth://totp/Example:test?secret=JBSWY3DPEHPK3PXP"
DATA = {
    "company_id": "example",
    "username": "employee",
    "password": "test-password",
    "profile_id": "a" * 64,
}


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    def flow(self, source="user", data=None):
        flow = HrworksConfigFlow()
        flow.context = {"source": source}
        flow._data = dict(DATA if data is None else data)
        flow._client = SimpleNamespace(
            login=AsyncMock(return_value={"profile_id": "b" * 64, "mfa_required": True}),
            mfa=AsyncMock(),
            close=AsyncMock(),
        )
        flow._finish = AsyncMock(return_value={"type": "done"})
        if source != "user":
            flow._entry = SimpleNamespace(data=dict(DATA))
        return flow

    async def test_setup_generates_code_from_uri(self):
        flow = self.flow()
        result = await flow.async_step_account({**DATA, "totp_uri": URI})
        self.assertEqual(result["type"], "done")
        flow._client.login.assert_awaited_once_with(
            "example", "employee", "test-password", force=True
        )
        code = flow._client.mfa.call_args.args[0]
        self.assertTrue(code.isdigit() and len(code) == 6)
        self.assertEqual(flow._data["totp_uri"], URI)
        self.assertNotIn("code", flow._data)

    async def test_reconfigure_username_and_password(self):
        flow = self.flow("reconfigure")
        await flow.async_step_account(
            {"company_id": "example", "username": "other", "password": "changed", "totp_uri": URI}
        )
        self.assertEqual(flow._data["username"], "other")
        flow._client.login.assert_awaited_once_with("example", "other", "changed", force=True)

    async def test_reauth_cannot_silently_switch_accounts(self):
        flow = self.flow("reauth")
        result = await flow.async_step_account(
            {"company_id": "example", "username": "other", "password": "changed"}
        )
        self.assertEqual(result["errors"]["base"], "different_account")
        flow._client.login.assert_not_awaited()

    async def test_failed_totp_falls_back_to_manual_code(self):
        flow = self.flow()
        flow._client.mfa.side_effect = [WorkerError("invalid_code"), {}]
        result = await flow.async_step_account({**DATA, "totp_uri": URI})
        self.assertEqual(result["step_id"], "account")
        self.assertEqual(result["errors"]["base"], "invalid_code")
        await flow.async_step_account({"code": "123456"})
        flow._client.mfa.assert_awaited_with("123456")
        flow._finish.assert_awaited_once()

    async def test_uri_can_be_entered_only_at_mfa_challenge(self):
        flow = self.flow()
        result = await flow.async_step_account(DATA)
        self.assertEqual(result["step_id"], "account")
        self.assertEqual(result["errors"]["base"], "mfa_required")
        flow._client.mfa.assert_not_awaited()
        await flow.async_step_account({"totp_uri": URI})
        self.assertEqual(flow._data["totp_uri"], URI)

    async def test_invalid_uri_does_not_contact_worker(self):
        flow = self.flow()
        result = await flow.async_step_account({**DATA, "totp_uri": "https://example.org/secret"})
        self.assertEqual(result["errors"]["base"], "invalid_totp")
        flow._client.login.assert_not_awaited()

    def test_blank_secrets_retained_and_uri_removable(self):
        settings = {**DATA, "totp_uri": URI}
        self.assertEqual(secret_settings(settings, {"password": "", "totp_uri": ""}), settings)
        self.assertNotIn("totp_uri", secret_settings(settings, {"clear_totp": True}))
        with self.assertRaises(InvalidTotp):
            secret_settings(DATA, {"totp_uri": URI + "&digits=8"})
        schema = account_schema(settings, existing=True)
        for key in schema.schema:
            if str(key) in {"password", "totp_uri"}:
                self.assertNotEqual(key.default, URI)
                self.assertNotEqual(key.default, DATA["password"])

    async def test_repair_saves_new_uri_only_after_success(self):
        entry = SimpleNamespace(data=DATA, unique_id=DATA["profile_id"])
        repair = EmployeeLoginRepair("test-entry")
        repair.hass = MagicMock()
        repair.hass.config_entries.async_get_entry.return_value = entry
        repair.client = SimpleNamespace(mfa=AsyncMock(side_effect=WorkerError("invalid_code")))
        repair.settings = dict(DATA)
        repair._mfa_pending = True
        repair._finish = AsyncMock(return_value={"type": "done"})
        result = await repair.async_step_login({"totp_uri": URI})
        self.assertEqual(result["errors"]["base"], "invalid_code")
        self.assertNotIn("totp_uri", repair.settings)
        repair.hass.config_entries.async_update_entry.assert_not_called()
        repair.client.mfa.side_effect = None
        await repair.async_step_login({"totp_uri": URI})
        self.assertEqual(repair.settings["totp_uri"], URI)
        repair._finish.assert_awaited_once_with(entry)

    def coordinator(self, uri=True):
        coordinator = object.__new__(HrworksCoordinator)
        coordinator.entry = SimpleNamespace(
            options={}, data={**DATA, **({"totp_uri": URI} if uri else {})}
        )
        coordinator.client = SimpleNamespace(
            snapshot=AsyncMock(),
            login=AsyncMock(return_value={"mfa_required": True}),
            mfa=AsyncMock(),
        )
        coordinator.hass = MagicMock()
        coordinator.last_error = None
        coordinator._normal_update_interval = timedelta(minutes=15)
        coordinator.update_interval = coordinator._normal_update_interval
        return coordinator

    async def test_expired_session_gets_one_read_recovery(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = [WorkerError("session_expired"), {"metrics": {}}]
        with patch("custom_components.hrworks.coordinator.clear_issues"):
            result = await coordinator._async_update_data()
        self.assertEqual(result, {"metrics": {}})
        self.assertEqual(coordinator.client.snapshot.await_count, 2)
        coordinator.client.login.assert_awaited_once()
        coordinator.client.mfa.assert_awaited_once()

    async def test_failed_recovery_never_loops(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = WorkerError("session_expired")
        with patch("custom_components.hrworks.coordinator.create_issue") as issue:
            with self.assertRaises(ConfigEntryAuthFailed):
                await coordinator._async_update_data()
        self.assertEqual(coordinator.client.snapshot.await_count, 2)
        coordinator.client.login.assert_awaited_once()
        self.assertEqual(issue.call_args.args[-1], "session_expired")

    async def test_session_without_uri_requires_manual_repair(self):
        coordinator = self.coordinator(uri=False)
        coordinator.client.snapshot.side_effect = WorkerError("session_expired")
        with patch("custom_components.hrworks.coordinator.create_issue"):
            with self.assertRaises(ConfigEntryAuthFailed):
                await coordinator._async_update_data()
        coordinator.client.login.assert_not_awaited()

    async def test_transient_failure_gets_one_immediate_read_retry(self):
        for code in ("browser_unavailable", "cannot_connect"):
            coordinator = self.coordinator()
            coordinator.client.snapshot.side_effect = [WorkerError(code), {"metrics": {}}]
            with patch("custom_components.hrworks.coordinator.clear_issues") as clear:
                result = await coordinator._async_update_data()
            self.assertEqual(result, {"metrics": {}})
            self.assertEqual(coordinator.client.snapshot.await_count, 2)
            coordinator.client.login.assert_not_awaited()
            self.assertIsNone(coordinator.last_error)
            self.assertEqual(coordinator.update_interval, timedelta(minutes=15))
            clear.assert_called_once()

    async def test_persistent_failure_shortens_polling_and_success_restores_it(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = WorkerError("browser_unavailable")
        with patch("custom_components.hrworks.coordinator.create_issue") as issue:
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
        self.assertEqual(coordinator.client.snapshot.await_count, 2)
        self.assertEqual(coordinator.update_interval, timedelta(seconds=60))
        self.assertEqual(issue.call_args.args[-1], "worker_unavailable")
        coordinator.client.snapshot.side_effect = None
        coordinator.client.snapshot.return_value = {"metrics": {}}
        with patch("custom_components.hrworks.coordinator.clear_issues"):
            await coordinator._async_update_data()
        self.assertEqual(coordinator.update_interval, timedelta(minutes=15))
        self.assertIsNone(coordinator.last_error)

    async def test_replacement_worker_can_reauthenticate(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = [
            WorkerError("browser_unavailable"),
            WorkerError("session_expired"),
            {"metrics": {}},
        ]
        with patch("custom_components.hrworks.coordinator.clear_issues"):
            await coordinator._async_update_data()
        self.assertEqual(coordinator.client.snapshot.await_count, 3)
        coordinator.client.login.assert_awaited_once()
        coordinator.client.mfa.assert_awaited_once()

    async def test_non_transient_errors_are_not_retried(self):
        for code in ("portal_changed", "worker_auth", "ssh_host_key", "invalid_request"):
            coordinator = self.coordinator()
            coordinator.client.snapshot.side_effect = WorkerError(code)
            with patch("custom_components.hrworks.coordinator.create_issue"):
                with self.assertRaises(UpdateFailed):
                    await coordinator._async_update_data()
            coordinator.client.snapshot.assert_awaited_once()
            self.assertEqual(coordinator.update_interval, timedelta(minutes=15))

    async def test_recovery_never_slows_a_faster_configured_interval(self):
        coordinator = self.coordinator()
        coordinator._normal_update_interval = timedelta(seconds=30)
        coordinator.client.snapshot.side_effect = WorkerError("cannot_connect")
        with patch("custom_components.hrworks.coordinator.create_issue"):
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
        self.assertEqual(coordinator.update_interval, timedelta(seconds=30))

    async def test_account_switch_preserves_entity_ids(self):
        flow = self.flow("reconfigure")
        del flow._finish
        flow.context["unique_id"] = "b" * 64
        flow.handler = "hrworks"
        flow._data = {**DATA, "username": "other", "profile_id": "b" * 64}
        flow._entry.entry_id = "entry-id"
        flow._entry.unique_id = "a" * 64
        flow.hass = MagicMock()
        flow.hass.config_entries.async_entries.return_value = [flow._entry]
        flow.async_set_unique_id = AsyncMock()
        flow._entry.update_listeners = [object()]
        flow._entry.title = "HR WORKS"
        flow.async_update_and_abort = MagicMock(return_value={"type": "done"})
        entity = SimpleNamespace(entity_id="sensor.original", unique_id="a" * 64 + "_today_worked")
        device = SimpleNamespace(id="device-id", identifiers={("hrworks", "a" * 64)})
        with (
            patch("custom_components.hrworks.config_flow.er.async_get") as registry,
            patch(
                "custom_components.hrworks.config_flow.er.async_entries_for_config_entry",
                return_value=[entity],
            ),
            patch("custom_components.hrworks.config_flow.dr.async_get") as devices,
            patch(
                "custom_components.hrworks.config_flow.dr.async_entries_for_config_entry",
                return_value=[device],
            ),
        ):
            await flow._finish()
        registry.return_value.async_update_entity.assert_called_once_with(
            "sensor.original", new_unique_id="b" * 64 + "_today_worked"
        )
        devices.return_value.async_update_device.assert_called_once_with(
            "device-id", new_identifiers={("hrworks", "b" * 64)}
        )
        flow.async_update_and_abort.assert_called_once_with(
            flow._entry, data=flow._data, title="HR WORKS", unique_id="b" * 64
        )
        flow.hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_switch_to_existing_account_is_rejected(self):
        flow = self.flow("reconfigure")
        del flow._finish
        flow.context["unique_id"] = "b" * 64
        flow.handler = "hrworks"
        flow._data["profile_id"] = "b" * 64
        flow._entry.entry_id = "entry-id"
        flow.hass = MagicMock()
        flow.hass.config_entries.async_entries.return_value = [
            SimpleNamespace(entry_id="other-entry", unique_id="b" * 64)
        ]
        flow.async_set_unique_id = AsyncMock()
        result = await flow._finish()
        self.assertEqual(result["reason"], "already_configured")
        flow.hass.config_entries.async_update_entry.assert_not_called()

    async def test_old_worker_cannot_silently_reuse_session(self):
        client = WorkerClient(MagicMock(), {})
        client.request = AsyncMock(return_value={"protocol_version": 1})
        with self.assertRaises(WorkerError) as error:
            await client.login("example", "employee", "new-password", force=True)
        self.assertEqual(error.exception.code, "incompatible_worker")
        client.request.assert_awaited_once_with("health")

    async def test_only_credentials_not_totp_seed_cross_ssh(self):
        client = WorkerClient(MagicMock(), {"totp_uri": URI})
        client.request = AsyncMock(
            side_effect=[
                {"protocol_version": 1, "capabilities": ["fresh_login"]},
                {"profile_id": "a" * 64},
            ]
        )
        await client.login("example", "employee", "password", force=True)
        client.request.assert_awaited_with(
            "login",
            {
                "company_id": "example",
                "username": "employee",
                "password": "password",
                "force": True,
            },
        )

    async def test_unchanged_reconfigure_reloads_fresh_session(self):
        flow = self.flow("reconfigure")
        del flow._finish
        flow.handler = "hrworks"
        flow._entry.entry_id = "entry-id"
        flow._entry.unique_id = DATA["profile_id"]
        flow._entry.title = "HR WORKS"
        flow._entry.update_listeners = [object()]
        flow.hass = MagicMock()
        flow.hass.config_entries.async_entries.return_value = [flow._entry]
        flow.async_set_unique_id = AsyncMock()
        flow.async_update_and_abort = MagicMock(return_value={"type": "done"})
        await flow._finish()
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-id")

    async def test_entry_without_listener_reloads_after_reauth(self):
        flow = self.flow("reauth")
        del flow._finish
        flow.handler = "hrworks"
        flow._entry.entry_id = "entry-id"
        flow._entry.unique_id = DATA["profile_id"]
        flow._entry.title = "HR WORKS"
        flow._entry.update_listeners = []
        flow._data["password"] = "new-password"
        flow.hass = MagicMock()
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_mismatch = MagicMock()
        flow.async_update_and_abort = MagicMock(return_value={"type": "done"})
        await flow._finish()
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry-id")


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_removes_listener_and_closes_shared_transport(self):
        remove = MagicMock()
        hass = MagicMock()
        hass.bus.async_listen_once.return_value = remove
        client = WorkerClient(hass, {})
        client.transport = SimpleNamespace(request=AsyncMock(return_value={}), close=AsyncMock())
        await client.request("health")
        await client.request("health")
        hass.bus.async_listen_once.assert_called_once()
        await client.close()
        remove.assert_called_once()
        client.transport.close.assert_awaited_once()

    async def test_stop_event_does_not_remove_consumed_listener(self):
        hass = MagicMock()
        client = WorkerClient(hass, {})
        client.transport = SimpleNamespace(request=AsyncMock(return_value={}), close=AsyncMock())
        await client.request("health")
        await client.close(object())
        hass.bus.async_listen_once.return_value.assert_not_called()
        client.transport.close.assert_awaited_once()
