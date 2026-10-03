"""Synthetic JSON-line worker; no network, browser, or employee credentials."""

import json
import os
import sys
from pathlib import Path

for line in sys.stdin:
    message = json.loads(line)
    path = message["path"]
    data = message.get("payload") or {}
    if request_log := os.environ.get("HRWORKS_TEST_REQUEST_LOG"):
        with Path(request_log).open("a") as stream:
            stream.write(json.dumps({"path": path, "payload": data}) + "\n")
    if path == "health":
        result = {
            "protocol_version": 1,
            "writes_enabled": not os.environ.get("HRWORKS_TEST_NO_WRITES"),
            "capabilities": ["fresh_login", "day", "account", "logout"],
        }
    elif path == "login":
        result = {
            "profile_id": "a" * 64,
            "authenticated": True,
            "mfa_required": bool(os.environ.get("HRWORKS_TEST_MFA")),
        }
    elif path.endswith("/mfa"):
        result = {} if data.get("code") == "123456" else {"error": "invalid_code"}
    elif path.endswith("/account"):
        result = {
            "metrics": {"monthly_worked": 480, "total_balance": -90},
            "account": {
                "period": "2026-09",
                "current_month": False,
                "balance_basis": "previous_day",
            },
        }
    elif path.endswith("/day"):
        result = {
            "date": data["date"],
            "metrics": {"today_worked": 480},
            "entries": [
                {
                    "start": "08:00",
                    "end": "12:00",
                    "type": "Working time",
                    "comment": "Invented [bold] row",
                }
            ],
        }
        if os.environ.get("HRWORKS_TEST_EMPTY_DAY"):
            result["entries"] = []
    elif path.endswith("/snapshot"):
        result = {
            "metrics": {"today_worked": 480, "today_target": 480, "today_balance": 0},
            "today": {"date": "2026-09-01"},
            "clocked_in": False,
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
                }
            ],
        }
    elif path.endswith("/record"):
        result = {
            "saved": not data.get("dry_run", True),
            "dry_run": data.get("dry_run", True),
            "duration_minutes": 240,
            **data,
        }
        if (
            os.environ.get("HRWORKS_TEST_FAIL_WRITE")
            and not data.get("dry_run", True)
            and data.get("start", "").endswith("13:00:00+02:00")
        ):
            result = {"error": "write_uncertain"}
    elif path.endswith("/logout"):
        result = {"logged_out": True}
    else:
        result = {"error": "not_found"}
    print(json.dumps(result), flush=True)
