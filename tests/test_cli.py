"""Subprocess tests prove stdout, exit codes and the installed entry point."""

import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from hrworks.cli import normalize_globals
from hrworks.config import ConfigStore
from hrworks.models import calendar_ics, import_csv
from hrworks.output import emit


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.config = root / "config.toml"
        self.worker = root / "fake-worker"
        self.worker.write_text(
            "#!"
            + sys.executable
            + "\n"
            + (Path(__file__).parent / "fixtures/worker.py").read_text()
        )
        self.worker.chmod(0o700)
        self.store = ConfigStore(self.config)
        self.store.save(
            "default",
            {
                "transport": "local",
                "worker_command": str(self.worker),
                "profile_id": "a" * 64,
                "timezone": "Europe/Berlin",
            },
        )
        self.env = {
            key: value for key, value in os.environ.items() if not key.startswith("HRWORKS_")
        }

    def cli(self, *args, input=None):
        return subprocess.run(
            [sys.executable, "-m", "hrworks.cli", "--config", str(self.config), *args],
            input=input,
            text=True,
            capture_output=True,
            env=self.env,
            timeout=20,
        )

    def test_version_and_json_anywhere(self):
        for args in [("--json", "--version"), ("--version", "--json")]:
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["version"], "2.0.0")
        self.assertEqual(
            normalize_globals(
                ["times", "record", "--comment", "--json", "--start", "08:00", "--json"]
            ),
            ["--json", "times", "record", "--comment", "--json", "--start", "08:00"],
        )

    def test_all_help_pages(self):
        for command in [
            (),
            ("times",),
            ("calendar",),
            ("auth",),
            ("config",),
            ("times", "record"),
            ("times", "import"),
            ("calendar", "export"),
            ("config", "init"),
        ]:
            with self.subTest(command=command):
                result = self.cli(*command, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Usage:", result.stdout)

    def test_default_output_is_literal_tsv_in_pipe(self):
        result = self.cli("balance")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\x1b", result.stdout)
        rows = list(csv.DictReader(io.StringIO(result.stdout), delimiter="\t"))
        self.assertEqual(rows[0]["time"], "8:00")
        self.assertEqual(rows[1]["time"], "−1:30")
        self.assertEqual(rows[0]["period"], "2026-09")

    def test_read_commands_json(self):
        for command in [
            ("doctor",),
            ("snapshot",),
            ("status",),
            ("balance",),
            ("auth", "status"),
            ("times", "list", "--date", "2026-09-01"),
            ("times", "list", "--month", "2026-09"),
            ("calendar", "list", "--from", "2026-09-01", "--to", "2026-09-30"),
            ("config", "show"),
            ("config", "list"),
        ]:
            with self.subTest(command=command):
                result = self.cli(*command, "--json")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                json.loads(result.stdout)

    def test_usage_errors_are_json_and_nonzero(self):
        result = self.cli("times", "record", "--json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "usage_error")
        self.assertNotIn("Traceback", result.stderr)

    def test_record_preview_and_explicit_save(self):
        args = (
            "times",
            "record",
            "--date",
            "2026-09-01",
            "--start",
            "08:00",
            "--end",
            "12:00",
            "--json",
        )
        result = self.cli(*args)
        self.assertTrue(json.loads(result.stdout)["dry_run"])
        result = self.cli(*args, "--save")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "confirmation_required")
        result = self.cli(*args, "--save", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)["entries"][0]["saved"])

    def test_csv_import_preview_and_save(self):
        path = Path(self.directory.name) / "entries.csv"
        path.write_text(
            "date,start,end,type,comment\n2026-09-01,08:00,12:00,working_time,Morning\n2026-09-01,13:00,17:00,doctors_appointment,\n"
        )
        for extra in [(), ("--save", "--yes")]:
            result = self.cli("times", "import", str(path), "--json", *extra)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entries = json.loads(result.stdout)["entries"]
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[1]["type"], "doctors_appointment")

    def test_secrets_private_and_redacted(self):
        result = self.cli(
            "config", "secret", "password", "--stdin", "--json", input="synthetic-password\n"
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("synthetic-password", result.stdout + result.stderr)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        result = self.cli("config", "show", "--json")
        self.assertEqual(json.loads(result.stdout)["password"], "[configured]")
        self.assertNotIn("synthetic-password", result.stdout)

    def test_login_without_saved_profile(self):
        self.store.save(
            "default",
            {
                "transport": "local",
                "worker_command": str(self.worker),
                "company_id": "example",
                "username": "invented",
                "password": "synthetic-password",
            },
        )
        result = self.cli("balance", "--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.store.profile("default")["profile_id"], "a" * 64)
        result = self.cli("auth", "login", "--force", "--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)["authenticated"])
        result = self.cli("auth", "logout", "--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("profile_id", self.store.read()["profiles"]["default"])

    def test_calendar_export(self):
        output = Path(self.directory.name) / "leave.ics"
        result = self.cli(
            "calendar",
            "export",
            str(output),
            "--from",
            "2026-09-01",
            "--to",
            "2026-09-30",
            "--json",
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["events"], 1)
        self.assertIn("DTEND;VALUE=DATE:20260903", output.read_text())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_ics_escaping_and_unicode_folding(self):
        output = calendar_ics(
            [
                {
                    "uid": "test",
                    "kind": "leave",
                    "start": "2026-09-01",
                    "end": "2026-09-02",
                    "summary": "ü" * 80 + ",;\nEND:VCALENDAR",
                }
            ]
        )
        self.assertIn("\\,\\;\\nEND:VCALENDAR", output)
        self.assertTrue(all(len(line.encode()) <= 75 for line in output.split("\r\n")))

    def test_import_rejects_open_and_overlapping_rows(self):
        for content in [
            "date,start,end\n2026-09-01,08:00,N/A\n",
            "date,start,end\n2026-09-01,08:00,12:00\n2026-09-01,11:00,13:00\n",
        ]:
            with self.assertRaises(ValueError):
                import_csv(content)

    def test_pretty_tsv_terminal_output_is_colorful_and_literal(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        output = Terminal()
        with (
            patch("sys.stdout", output),
            patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=True),
        ):
            emit([{"date": "2026-09-01", "comment": "[bold] literal", "balance": "−1:30"}])
        result = output.getvalue()
        self.assertIn("DATE", result)
        self.assertIn("[bold] literal", result)
        self.assertIn("\x1b[", result)
        self.assertNotIn("│", result)

    def test_completion_and_type_discovery(self):
        for shell in ["bash", "zsh", "fish"]:
            result = self.cli("completion", shell, "--json")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(json.loads(result.stdout)["shell"], shell)
            self.assertIn("hrworks", json.loads(result.stdout)["script"])
        result = self.cli("times", "types", "--json")
        self.assertEqual(len(json.loads(result.stdout)), 4)

    def test_manual_mfa_code_from_stdin(self):
        self.store.save(
            "default",
            {
                "transport": "local",
                "worker_command": str(self.worker),
                "company_id": "example",
                "username": "invented",
                "password": "synthetic",
            },
        )
        self.env["HRWORKS_TEST_MFA"] = "1"
        result = self.cli("auth", "login", "--code-stdin", "--json", input="123456\n")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)["authenticated"])
        self.assertNotIn("123456", result.stdout + result.stderr)

    def test_disabled_writes_and_partial_batch(self):
        path = Path(self.directory.name) / "batch.csv"
        path.write_text(
            "date,start,end\n2026-09-01,08:00,12:00\n2026-09-01,13:00,17:00\n2026-09-02,08:00,12:00\n"
        )
        self.env["HRWORKS_TEST_NO_WRITES"] = "1"
        result = self.cli("times", "import", str(path), "--save", "--yes", "--json")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "writes_disabled")
        del self.env["HRWORKS_TEST_NO_WRITES"]
        self.env["HRWORKS_TEST_FAIL_WRITE"] = "1"
        result = self.cli("times", "import", str(path), "--save", "--yes", "--json")
        self.assertEqual(result.returncode, 4, result.stdout)
        body = json.loads(result.stdout)
        self.assertEqual(body["saved_count"], 1)
        self.assertEqual(body["failed_index"], 1)
        self.assertEqual(len(body["results"]), 1)

    def test_empty_day_names_the_requested_date(self):
        self.env["HRWORKS_TEST_EMPTY_DAY"] = "1"
        result = self.cli("times", "list", "--date", "2026-09-01")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026-09-01", result.stdout)
        self.assertIn("No working-time entries", result.stdout)
        result = self.cli("times", "list", "--date", "2026-09-01", "--json")
        self.assertEqual(json.loads(result.stdout)[0]["entries"], [])

    def test_times_list_defaults_to_current_month(self):
        result = self.cli("times", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stdout)
        days = json.loads(result.stdout)
        today = date.today()
        self.assertEqual(len(days), today.day)
        self.assertEqual(days[0]["date"], today.replace(day=1).isoformat())
        self.assertTrue(all(day["date"].startswith(today.strftime("%Y-%m")) for day in days))
