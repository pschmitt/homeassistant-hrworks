"""A stand-in for browser-daemon-client for CLI subprocess tests: no network, no browser.

Put its parent directory first on PYTHONPATH. It answers every run from the operation
(recognised by its script) with synthetic data and honours the HRWORKS_TEST_* switches.
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class BrowserDaemonError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


@dataclass
class RunResult:
    ok: bool
    result: Any = None
    error: str | None = None
    timed_out: bool = False
    steps: list = field(default_factory=list)
    logs: list = field(default_factory=list)
    url: str | None = None
    page: str | None = None
    screenshot: str | None = None
    ms: int | None = None


class RunError(BrowserDaemonError):
    def __init__(self, run, status=None):
        super().__init__(run.error or "The run failed", status)
        self.run = run


class RunTimeout(RunError):
    pass


def read_secret(path=None):
    return "fake-secret"


def failed(code):
    return RunError(RunResult(ok=False, error=f"hrworks:{code}"), 422)


class BrowserDaemon:
    def __init__(self, session, url, secret=None, *, client=None, timeout=30):
        self.url = url

    async def close_tab(self, page_id, node=None):
        pass

    async def tabs(self, node=None):
        return []

    async def find_profile(self, name):
        return {"name": name, "session_id": "fake-session"}

    async def run(self, *, script=None, args=None, tab=None, **_options):
        from hrworks.browser_daemon import OPERATIONS
        from hrworks.browser_daemon import script as compose

        operation = next(name for name in OPERATIONS if compose(name) == script)
        data = args or {}
        log = os.environ.get("HRWORKS_TEST_REQUEST_LOG")
        if log:  # noqa: ASYNC230
            with Path(log).open("a") as stream:  # noqa: ASYNC230
                stream.write(
                    json.dumps({"path": f"profiles/x/{operation}", "payload": data}) + "\n"
                )
        env = os.environ.get
        if operation == "probe":
            return RunResult(ok=True, result={"browser_version": "1"})
        if operation == "login":
            if env("HRWORKS_TEST_LOGIN_ERROR"):
                raise failed(env("HRWORKS_TEST_LOGIN_ERROR"))
            if env("HRWORKS_TEST_MFA"):
                return RunResult(ok=True, result={"mfa_required": True}, page="TAB")
            return RunResult(ok=True, result={"authenticated": True}, page="TAB")
        if operation == "mfa":
            if data.get("code") != "123456":
                raise failed("invalid_code")
            return RunResult(ok=True, result={"authenticated": True}, page="TAB")
        if operation == "account":
            return RunResult(
                ok=True,
                result={
                    "metrics": {"monthly_worked": 480, "total_balance": -90},
                    "period": "2026-09",
                    "balance_basis": "previous_day",
                },
            )
        if operation == "day":
            entries = [
                {
                    "start": "08:00",
                    "end": "12:00",
                    "type": "Working time",
                    "comment": "Invented [bold] row",
                }
            ]
            if env("HRWORKS_TEST_EMPTY_DAY"):
                entries = []
            return RunResult(ok=True, result={"metrics": {"today_worked": 480}, "entries": entries})
        if operation == "snapshot":
            return RunResult(
                ok=True,
                result={
                    "account": {
                        "metrics": {},
                        "period": None,
                        "balance_basis": None,
                    },
                    "lists": [
                        {
                            "year": year,
                            "kind": kind,
                            "vacation": {},
                            "events": [
                                {
                                    "uid": "synthetic-leave",
                                    "kind": "leave",
                                    "start": "2026-09-01",
                                    "end": "2026-09-03",
                                    "summary": "Invented vacation",
                                    "status": "Approved",
                                    "approved": True,
                                    "half_day": False,
                                    "description": "Approved",
                                }
                            ]
                            if kind == "leave" and year == 2026
                            else [],
                        }
                        for year in data["years"]
                        for kind in data["kinds"]
                    ],
                    "today": {
                        "metrics": {"today_worked": 480, "today_target": 480, "today_balance": 0},
                        "entries": [],
                    },
                    "clock": "in_available",
                    "timings": {"account": 0.1, "events": 0.1, "today": 0.1},
                    "browser_version": "1",
                },
            )
        if operation == "record":
            if env("HRWORKS_TEST_FAIL_WRITE") and not data["dry_run"] and data["start"] == "13:00":
                raise failed("write_uncertain")
            return RunResult(
                ok=True,
                result={
                    "saved": not data["dry_run"],
                    "dry_run": data["dry_run"],
                    "duration_minutes": 240,
                    "date": data["date"],
                    "start": data["start"],
                    "end": data["end"],
                    "type": data["type"],
                },
            )
        if operation == "logout":
            return RunResult(ok=True, result={"logged_out": True})
        raise failed("not_found")
