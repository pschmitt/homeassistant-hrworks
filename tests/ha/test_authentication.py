"""Exercise real HA flow classes with a fake worker, never real HR records."""

import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.hrworks.api import WorkerError
from custom_components.hrworks.auth import authenticate, complete_mfa, credentials, secret_settings
from custom_components.hrworks.config_flow import (
    RBW_DISABLED,
    HrworksConfigFlow,
    account_schema,
    connection_schema,
)
from custom_components.hrworks.coordinator import (
    AUTH_RETRY_BACKOFF,
    OUTAGE_REPAIR_AFTER,
    TAB_GONE,
    HrworksCoordinator,
)
from custom_components.hrworks.repairs import EmployeeLoginRepair, WorkerUnavailableRepair
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
        flow.hass = MagicMock()
        flow.hass.config_entries.async_entries.return_value = []
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

    def test_profile_default_preserves_existing_names_and_explicit_throwaway(self):
        for defaults, expected in (
            ({}, "ha-hrworks"),
            ({"browser_daemon_profile": "hrworks"}, "hrworks"),
            ({"browser_daemon_profile": ""}, ""),
        ):
            markers = {key.schema: key for key in connection_schema(defaults, MagicMock()).schema}
            self.assertEqual(markers["browser_daemon_profile"].default(), expected)

    def test_account_fields_depend_on_vault_selection(self):
        markers = {key.schema: key for key in account_schema({}).schema}
        self.assertEqual(
            markers["password"].visible, {"field": "rbw_entry_id", "value": RBW_DISABLED}
        )
        self.assertEqual(markers["totp_uri"].visible, markers["password"].visible)
        self.assertEqual(
            markers["rbw_item_id"].visible,
            {"field": "rbw_entry_id", "operator": "not_eq", "value": RBW_DISABLED},
        )

    def test_account_vault_defaults_map_only_disabled_values(self):
        for defaults, expected in (
            ({}, RBW_DISABLED),
            ({"rbw_entry_id": ""}, RBW_DISABLED),
            ({"rbw_entry_id": None}, RBW_DISABLED),
            ({"rbw_entry_id": "saved-vault"}, "saved-vault"),
        ):
            with self.subTest(defaults=defaults):
                schema = account_schema(defaults)
                markers = {key.schema: key for key in schema.schema}
                self.assertEqual(markers["rbw_entry_id"].default(), expected)
                self.assertEqual(
                    schema({"company_id": "example", "username": "employee"})["rbw_entry_id"],
                    expected,
                )

    def test_native_visibility_serializes_and_strips_hidden_credentials(self):
        import probatio
        from homeassistant.data_entry_flow import (
            _strip_hidden_fields,
            add_visible_conditions_to_serialized_schema,
        )
        from homeassistant.helpers import config_validation as cv

        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [
            SimpleNamespace(entry_id="vault", title="Vault")
        ]
        schema = account_schema({}, hass=hass)
        fields = probatio.to_field_list(schema, custom_serializer=cv.custom_serializer)
        add_visible_conditions_to_serialized_schema(schema, fields)
        password = next(field for field in fields if field["name"] == "password")
        self.assertEqual(password["visible"], {"field": "rbw_entry_id", "value": RBW_DISABLED})
        vault = next(field for field in fields if field["name"] == "rbw_entry_id")
        options = json.dumps(vault["options"])
        self.assertIn('"disabled"', options)
        self.assertIn('"Disabled"', options)
        self.assertIn('"vault"', options)
        self.assertNotIn('""', options)
        item = next(field for field in fields if field["name"] == "rbw_item_id")
        self.assertEqual(
            item["visible"],
            {"field": "rbw_entry_id", "operator": "not_eq", "value": RBW_DISABLED},
        )
        vault_input = {
            "company_id": "example",
            "username": "employee",
            "password": "hidden",
            "rbw_entry_id": "vault",
            "rbw_item_id": "item",
            "totp_uri": "hidden",
        }
        visible_schema, visible_input = _strip_hidden_fields(schema, vault_input)
        validated = visible_schema(visible_input)
        self.assertNotIn("password", validated)
        self.assertNotIn("totp_uri", validated)
        direct_schema, direct_input = _strip_hidden_fields(
            schema,
            {
                "company_id": "example",
                "username": "employee",
                "password": "direct",
                "rbw_entry_id": RBW_DISABLED,
                "rbw_item_id": "stale-item",
                "totp_uri": URI,
                "code": "123456",
            },
        )
        direct = direct_schema(direct_input)
        self.assertNotIn("rbw_item_id", direct)
        self.assertEqual(direct["password"], "direct")
        self.assertEqual(direct["totp_uri"], URI)
        self.assertEqual(direct["code"], "123456")
        self.assertEqual(direct["rbw_entry_id"], RBW_DISABLED)

    async def test_disabling_vault_clears_hidden_old_item(self):
        for source in ("user", "reconfigure", "reauth"):
            for disabled in ("", RBW_DISABLED):
                for hidden in ({}, {"rbw_item_id": "stale-item"}):
                    with self.subTest(source=source, disabled=disabled, hidden=hidden):
                        flow = self.flow(
                            source,
                            data={**DATA, "rbw_entry_id": "old-vault", "rbw_item_id": "old-item"},
                        )
                        flow._client.login.return_value = {
                            "profile_id": "b" * 64,
                            "authenticated": True,
                        }
                        supplied = {**DATA, "rbw_entry_id": disabled, **hidden}
                        result = await flow.async_step_account(supplied)
                        self.assertEqual(result["type"], "done")
                        self.assertEqual(flow._data["rbw_entry_id"], "")
                        self.assertEqual(flow._data["rbw_item_id"], "")
                        self.assertEqual(supplied["rbw_entry_id"], disabled)
                        flow._client.login.assert_awaited_once_with(
                            "example", "employee", "test-password", force=True
                        )

    async def test_rbw_reads_fresh_password_and_current_code_per_attempt(self):
        settings = {**DATA, "rbw_entry_id": "vault", "rbw_item_id": "item"}
        original = dict(settings)
        reader = AsyncMock(
            side_effect=[
                {"value": "employee"},
                {"value": "first"},
                {"value": "123456"},
                {"value": "employee"},
                {"value": "changed"},
                {"value": "654321"},
            ]
        )
        client = self.flow()._client
        with patch.dict(
            "sys.modules", {"custom_components.rbw": SimpleNamespace(async_get_field=reader)}
        ):
            await authenticate(client, settings, hass=object())
            await authenticate(client, settings, hass=object())
        self.assertEqual(
            [call.args[2] for call in client.login.await_args_list], ["first", "changed"]
        )
        self.assertEqual(
            [call.args[0] for call in client.mfa.await_args_list], ["123456", "654321"]
        )
        self.assertEqual(
            [call.args[3] for call in reader.await_args_list], ["username", "password", "totp"] * 2
        )
        self.assertEqual(settings, original)

    async def test_rbw_unavailable_never_falls_back_to_saved_password(self):
        settings = {**DATA, "rbw_entry_id": "vault", "rbw_item_id": "item"}
        for result in ({}, {"value": ""}, RuntimeError("private vault failure")):
            reader = AsyncMock(
                side_effect=result if isinstance(result, Exception) else None, return_value=result
            )
            client = self.flow()._client
            with patch.dict(
                "sys.modules", {"custom_components.rbw": SimpleNamespace(async_get_field=reader)}
            ):
                with self.assertRaises(WorkerError) as caught:
                    await authenticate(client, settings, hass=object())
            self.assertEqual(caught.exception.code, "rbw_unavailable")
            self.assertNotIn("private vault failure", str(caught.exception))
            client.login.assert_not_awaited()

    async def test_rbw_account_mismatch_stops_before_password_read(self):
        reader = AsyncMock(return_value={"value": "other"})
        with patch.dict(
            "sys.modules", {"custom_components.rbw": SimpleNamespace(async_get_field=reader)}
        ):
            with self.assertRaises(WorkerError) as caught:
                await credentials(
                    object(), {**DATA, "rbw_entry_id": "vault", "rbw_item_id": "item"}
                )
        self.assertEqual(caught.exception.code, "different_account")
        reader.assert_awaited_once()

    async def test_direct_credentials_do_not_access_rbw(self):
        reader = AsyncMock(side_effect=AssertionError("rbw must stay optional"))
        with patch.dict(
            "sys.modules", {"custom_components.rbw": SimpleNamespace(async_get_field=reader)}
        ):
            self.assertEqual(await credentials(object(), DATA), DATA)
        reader.assert_not_awaited()

    async def test_rbw_setup_and_manual_mfa_do_not_persist_direct_secrets(self):
        flow = self.flow()
        reader = AsyncMock(
            side_effect=[{"value": "employee"}, {"value": "fresh"}, {"value": "123456"}]
        )
        with patch.dict(
            "sys.modules", {"custom_components.rbw": SimpleNamespace(async_get_field=reader)}
        ):
            result = await flow.async_step_account(
                {**DATA, "rbw_entry_id": "vault", "rbw_item_id": "item", "totp_uri": URI}
            )
        self.assertEqual(result["type"], "done")
        self.assertNotIn("password", flow._data)
        self.assertNotIn("totp_uri", flow._data)
        settings = await complete_mfa(
            flow._client,
            flow._data,
            {"code": "654321", "password": "ignored", "totp_uri": URI},
            hass=flow.hass,
        )
        self.assertNotIn("password", settings)
        self.assertNotIn("totp_uri", settings)
        self.assertNotIn("code", settings)
        flow._client.mfa.assert_awaited_with("654321")

    async def test_incomplete_rbw_selection_does_not_login(self):
        flow = self.flow()
        result = await flow.async_step_account({**DATA, "rbw_entry_id": "vault"})
        self.assertEqual(result["errors"]["base"], "rbw_incomplete")
        flow._client.login.assert_not_awaited()

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

    async def test_direct_repair_keeps_credential_fields_visible_without_vault_selector(self):
        from homeassistant.data_entry_flow import _strip_hidden_fields

        repair = EmployeeLoginRepair("test-entry")
        repair.hass = MagicMock()
        repair.hass.config_entries.async_entries.return_value = []
        form = repair._show_login_form(SimpleNamespace(data=DATA), {})
        schema, submitted = _strip_hidden_fields(form["data_schema"], {"password": "replacement"})
        self.assertEqual(schema(submitted)["password"], "replacement")

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

    async def test_worker_unavailable_repair_reloads_entry(self):
        entry = SimpleNamespace(data=DATA, entry_id="test-entry")
        repair = WorkerUnavailableRepair("test-entry")
        repair.hass = MagicMock()
        repair.hass.config_entries.async_get_entry.return_value = entry
        repair.hass.config_entries.async_reload = AsyncMock()
        form = await repair.async_step_init()
        self.assertEqual(form["step_id"], "confirm")
        result = await repair.async_step_confirm({})
        self.assertEqual(result["type"], "create_entry")
        repair.hass.config_entries.async_reload.assert_awaited_once_with("test-entry")

    def coordinator(self, uri=True):
        coordinator = object.__new__(HrworksCoordinator)
        coordinator.entry = SimpleNamespace(
            entry_id="test-entry", options={}, data={**DATA, **({"totp_uri": URI} if uri else {})}
        )
        coordinator.client = SimpleNamespace(
            snapshot=AsyncMock(),
            login=AsyncMock(return_value={"mfa_required": True}),
            mfa=AsyncMock(),
            end_session=AsyncMock(),
        )
        coordinator.hass = MagicMock()
        coordinator.hass.data = {}
        coordinator.last_error = None
        coordinator._auth_recovery_blocked = False
        coordinator._normal_update_interval = timedelta(minutes=15)
        coordinator.update_interval = coordinator._normal_update_interval
        coordinator._work_days = AsyncMock(return_value=[])
        coordinator.update_count = 0
        coordinator.failed_updates = 0
        coordinator.consecutive_failures = 0
        coordinator.reauthentications = 0
        coordinator.last_duration = None
        coordinator.last_finished = None
        return coordinator

    async def test_expired_session_gets_one_read_recovery(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = [WorkerError("session_expired"), {"metrics": {}}]
        with patch("custom_components.hrworks.coordinator.clear_issues"):
            result = await coordinator._async_update_data()
        self.assertEqual(result, {"metrics": {}, "work_days": []})
        self.assertEqual(coordinator.client.snapshot.await_count, 2)
        coordinator.client.login.assert_awaited_once()
        coordinator.client.mfa.assert_awaited_once()

    async def test_failed_recovery_never_loops(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = WorkerError("session_expired")
        with patch("custom_components.hrworks.coordinator.create_issue") as issue:
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
        self.assertEqual(coordinator.client.snapshot.await_count, 2)
        coordinator.client.login.assert_awaited_once()
        self.assertEqual(issue.call_args.args[-1], "session_expired")

    async def test_session_without_uri_requires_manual_repair(self):
        coordinator = self.coordinator(uri=False)
        coordinator.client.snapshot.side_effect = WorkerError("session_expired")
        with patch("custom_components.hrworks.coordinator.create_issue"):
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
        coordinator.client.login.assert_not_awaited()

    async def test_transient_failures_get_three_delayed_read_retries(self):
        for code in ("browser_unavailable", "cannot_connect"):
            coordinator = self.coordinator()
            coordinator.client.snapshot.side_effect = [
                WorkerError(code),
                WorkerError(code),
                WorkerError(code),
                {"metrics": {}},
            ]
            with patch("custom_components.hrworks.coordinator.clear_issues") as clear:
                result = await coordinator._async_update_data()
            self.assertEqual(result, {"metrics": {}, "work_days": []})
            self.assertEqual(coordinator.client.snapshot.await_count, 4)
            coordinator.client.login.assert_not_awaited()
            self.assertIsNone(coordinator.last_error)
            self.assertEqual(coordinator.update_interval, timedelta(minutes=15))
            clear.assert_called_once()

    async def test_an_outage_retries_quietly_and_only_a_long_one_raises_a_repair(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = WorkerError("browser_unavailable")
        with patch("custom_components.hrworks.coordinator.create_issue") as issue:
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
        self.assertEqual(coordinator.client.snapshot.await_count, 4)
        self.assertEqual(coordinator.update_interval, timedelta(seconds=60))
        # It never reloads itself, and a short outage needs nobody's attention.
        coordinator.hass.config_entries.async_schedule_reload.assert_not_called()
        issue.assert_not_called()

        coordinator.consecutive_failures = OUTAGE_REPAIR_AFTER - 1
        with patch("custom_components.hrworks.coordinator.create_issue") as issue:
            with self.assertRaises(UpdateFailed):
                await coordinator._async_update_data()
        self.assertEqual(issue.call_args.args[-1], "worker_unavailable")
        coordinator.hass.config_entries.async_schedule_reload.assert_not_called()

        coordinator.client.snapshot.side_effect = None
        coordinator.client.snapshot.return_value = {"metrics": {}}
        with patch("custom_components.hrworks.coordinator.clear_issues") as clear:
            await coordinator._async_update_data()
        self.assertEqual(coordinator.update_interval, timedelta(minutes=15))
        self.assertIsNone(coordinator.last_error)
        clear.assert_called_once()

    async def test_only_a_stuck_browser_is_stopped(self):
        for stage, stops in (("browser_connect", False), ("browser_operation", True)):
            coordinator = self.coordinator()
            coordinator._async_recover_browser_session = AsyncMock()
            coordinator.client.snapshot.side_effect = [
                WorkerError("browser_unavailable", stage=stage),
                {"metrics": {}},
            ]
            with patch("custom_components.hrworks.coordinator.clear_issues"):
                await coordinator._async_update_data()
            self.assertEqual(coordinator._async_recover_browser_session.await_count, int(stops))

    async def test_a_lost_tab_keeps_the_login(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = [
            WorkerError("session_expired", detail=TAB_GONE),
            {"metrics": {}},
        ]
        with patch("custom_components.hrworks.coordinator.clear_issues"):
            await coordinator._async_update_data()
        coordinator.client.login.assert_not_awaited()

    async def test_a_lost_tab_and_an_expired_login_signs_in(self):
        coordinator = self.coordinator()
        coordinator.client.snapshot.side_effect = [
            WorkerError("session_expired", detail=TAB_GONE),
            WorkerError("session_expired"),
            {"metrics": {}},
        ]
        with patch("custom_components.hrworks.coordinator.clear_issues"):
            await coordinator._async_update_data()
        coordinator.client.login.assert_awaited_once()

    async def test_a_rejected_login_is_retried_later_with_growing_pauses(self):
        coordinator = self.coordinator()
        clock = [1000.0]
        with patch("custom_components.hrworks.coordinator.time.monotonic", lambda: clock[0]):
            for pause in AUTH_RETRY_BACKOFF + (AUTH_RETRY_BACKOFF[-1],):
                coordinator._auth_recovery_blocked = True
                self.assertTrue(coordinator._auth_recovery_blocked)
                clock[0] += pause - 1
                self.assertTrue(coordinator._auth_recovery_blocked)
                clock[0] += 1
                self.assertFalse(coordinator._auth_recovery_blocked)
            coordinator._auth_recovery_blocked = True
            coordinator._auth_recovery_blocked = False
            self.assertFalse(coordinator._auth_recovery_blocked)
            coordinator._auth_recovery_blocked = True
            clock[0] += AUTH_RETRY_BACKOFF[0]
            self.assertFalse(coordinator._auth_recovery_blocked)

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
