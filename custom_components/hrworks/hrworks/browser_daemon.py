"""Read and (optionally) write the employee portal through browser-daemon's run API.

The portal logic runs as Playwright scripts (``scripts/*.js``) on the
node that owns a browser-daemon profile, so nothing but ``aiohttp`` is needed here. The
profile's browser keeps the HR WORKS login; every operation opens its own tab.

Credentials and TOTP codes travel in the run's ``args`` only. Nothing of a run (script,
args, error text) is logged: errors become fixed codes.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import aiohttp
from browser_daemon_client import BrowserDaemon, BrowserDaemonError, RunError, RunTimeout

from .errors import ERRORS, WorkerError
from .models import TYPE_LABELS

BERLIN = ZoneInfo("Europe/Berlin")
CLIENT_NAME = "Home Assistant · HR WORKS"
MFA_WINDOW = timedelta(minutes=10)
SCRIPT_DIR = Path(__file__).parent / "scripts"
OPERATIONS = (
    "probe",
    "login",
    "mfa",
    "snapshot",
    "day",
    "account",
    "record",
    "assist",
    "team_calendar",
    "company_absences",
    "logout",
)
# Seconds a run may take, per operation .
TIMEOUTS = {
    "snapshot": 170,
    "record": 150,
    "assist": 170,
    "team_calendar": 120,
    "company_absences": 300,
    "login": 90,
    "day": 90,
    "account": 90,
    "mfa": 60,
}
ERROR_CODE = re.compile(r"hrworks:([a-z_]+)")
SCRIPT_BUGS = ("TypeError", "ReferenceError", "SyntaxError", "RangeError")
# Rejections of the request itself: the run never started, so a write cannot have happened.
REFUSED = {
    400: "invalid_request",
    401: "worker_auth",
    403: "worker_auth",
    404: "browser_unavailable",
}


def _read(name: str) -> str:
    return (SCRIPT_DIR / f"{name}.js").read_text(encoding="utf-8").strip()


HELPERS = _read("helpers")
SOURCES = {name: _read(name) for name in OPERATIONS}


def script(operation: str) -> str:
    """One function expression: the shared helpers, then the operation called with them."""
    return (
        f"async (ctx) => {{\n{HELPERS}\nconst operation = {SOURCES[operation]};\n"
        "return await operation(ctx, H);\n}"
    )


def profile_id(company: str, username: str) -> str:
    """The employee identity, a hash of company and user (entity IDs stay stable)."""
    return hashlib.sha256(f"{company.casefold()}:{username.casefold()}".encode()).hexdigest()


def _failure(err: Exception) -> tuple[str, bool, str | None, str | None]:
    """A fixed error code, whether the run may already have reached the portal, and the
    failure stage and detail for diagnostics (fixed vocabulary, never the error text)."""
    if isinstance(err, RunTimeout):
        return "browser_unavailable", True, "browser_operation", "timeout"
    if isinstance(err, RunError):
        message = err.run.error or ""
        if match := ERROR_CODE.search(message):
            code = match[1] if match[1] in ERRORS else "portal_changed"
            if code == "login_timeout":
                return code, False, "browser_operation", "timeout"
            return code, False, None, None
        if "not found in the session" in message:  # the browser or its tab is gone, not the login
            return "session_expired", False, None, TAB_GONE
        if message.startswith(SCRIPT_BUGS):
            return "portal_changed", True, None, None
        detail = "timeout" if "Timeout" in message else None
        return "browser_unavailable", True, "browser_operation", detail
    status = getattr(err, "status", None)
    if status in REFUSED:
        detail = f"http_{status}" if status in {401, 403} else None
        return REFUSED[status], False, "browser_connect", detail
    if status == 501:
        return "incompatible_worker", False, "browser_connect", None
    if status is None:
        detail = "timeout" if isinstance(err.__cause__, TimeoutError) else "connection_failed"
        return "cannot_connect", True, "browser_connect", detail
    detail = "http_429" if status == 429 else "http_5xx" if status >= 500 else None
    return "browser_unavailable", True, "browser_connect", detail


TAB_GONE = "tab_gone"


class BrowserDaemonClient:
    """Reads and writes the portal through browser-daemon runs."""

    def __init__(
        self,
        settings: dict,
        *,
        http: aiohttp.ClientSession | None = None,
        daemon: BrowserDaemon | None = None,
        allow_writes: bool = False,
    ) -> None:
        self.settings = dict(settings)
        self.profile = settings.get("profile_id")
        self.capabilities: list[str] | None = None
        self.allow_writes = allow_writes
        self.browser_profile = (settings.get("browser_daemon_profile") or "").strip() or None
        self.node_group = (settings.get("browser_daemon_node_group") or "").strip() or None
        self._node: str | None = None
        self._session: str | None = None
        self._http = http
        self._owns_http = False
        self._daemon = daemon
        self._lock = asyncio.Lock()
        # The portal tab: the HR WORKS login lives in the tab that signed in, so every
        # operation runs in it (a persistent page).
        self._tab: str | None = None
        self._mfa_started: float | None = None
        self._connections = 0

    # Plumbing

    def _client(self) -> BrowserDaemon:
        if self._daemon is None:
            url = str(self.settings.get("browser_daemon_url") or "").strip()
            if not url:
                raise WorkerError("invalid_request")
            if self._http is None:
                self._http = aiohttp.ClientSession()
                self._owns_http = True
            self._daemon = BrowserDaemon(
                self._http,
                url,
                self.settings.get("browser_daemon_secret") or None,
                client=CLIENT_NAME,
            )
        return self._daemon

    async def close(self) -> None:
        self.capabilities = None
        # The browser session is ended by end_session() after each refresh.
        if self._owns_http and self._http is not None:
            await self._http.close()
            self._http = self._daemon = None
            self._owns_http = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        await self.close()

    async def end_session(self) -> None:
        """Stop the profile's browser; the saved profile keeps the "remember me" login.

        Skipped while an MFA code is awaited (the tab holds the pending login). Never raises.
        """
        if self._mfa_started is not None:
            return
        async with self._lock:
            self._tab = None
            try:
                client = self._client()
                if self.browser_profile:
                    found = await client.find_profile(self.browser_profile)
                    session = (found or {}).get("session_id")
                    node = (found or {}).get("node_name")
                else:
                    session, node = self._session, self._node
                if session:
                    await client.delete_session(session, node=node)
                self._session = None
            except (BrowserDaemonError, aiohttp.ClientError, TimeoutError, WorkerError):
                pass

    async def stop_profile_browser(self) -> str | None:
        """Stop a stuck browser of the profile, on any node; returns its session ID.

        Takes no lock, because the operation holding it may be what is stuck. A throwaway
        browser has no profile to find and stops nothing."""
        if not self.browser_profile:
            return None
        client = self._client()
        found = await client.find_profile(self.browser_profile)
        session = (found or {}).get("session_id")
        if not session:
            return None
        await client.delete_session(session, node=(found or {}).get("node_name"))
        self._tab = self._mfa_started = self._session = None
        return session

    async def _close_tab(self) -> None:
        tab, self._tab, self._mfa_started = self._tab, None, None
        if tab:
            try:
                await self._client().close_tab(tab, node=self._node)
            except (BrowserDaemonError, WorkerError):
                pass

    async def _adopt_tab(self) -> None:
        """After a restart of Home Assistant: take over the portal tab the profile still has."""
        if not self.browser_profile:
            return
        try:
            client = self._client()
            found = await client.find_profile(self.browser_profile)
            session = (found or {}).get("session_id")
            node = (found or {}).get("node_name")
            if not session or (self.node_group and node != self._node):
                return
            self._node = node
            for tab in await client.tabs(node=node):
                if (tab.get("sessionId") or tab.get("session_id")) != session:
                    continue
                if tab.get("type", "page") == "page" and "hrworks.de" in str(tab.get("url", "")):
                    self._tab = tab.get("id")
                    return
        except (BrowserDaemonError, WorkerError, AttributeError, TypeError):
            pass

    async def _run(
        self, operation: str, args: dict | None = None, *, write: bool = False, **options: Any
    ) -> Any:
        """Run one operation; raise only fixed :class:`WorkerError` codes."""
        target = {"profile": self.browser_profile} if self.browser_profile else {}
        async with self._lock:
            self._connections += 1
            try:
                if self.node_group and (self.browser_profile or self._session is None):
                    if self._client().local_node is None:
                        await self._client().overview()
                    placed = await self._client()._json(
                        "POST",
                        "/api/mesh/placement",
                        json={
                            "profile": self.browser_profile,
                            "group": self.node_group,
                            "engine": "chromium",
                            "create": True,
                        },
                        timeout=180,
                    )
                    if self._node is not None and self._node != placed["node"]:
                        self._tab = self._mfa_started = None
                        if operation == "mfa":
                            raise WorkerError("session_expired")
                    self._node = placed["node"]
                if self.browser_profile:
                    found = await self._client().find_profile(self.browser_profile)
                    if not found:
                        await self._client()._json(
                            "POST",
                            "/api/profiles",
                            node=self._node,
                            json={"name": self.browser_profile, "engine": "chromium"},
                        )
                    elif self._node is None:
                        self._node = found.get("node_name")
                else:
                    if self._session is None:
                        session = await self._client().create_session(
                            node=self._node,
                            persistent=False,
                            engine="chromium",
                            default_tabs=False,
                            idle_timeout_minutes=10,
                        )
                        self._session = session["id"]
                        self._node = session.get("node_name", self._node)
                    target = {"session_id": self._session}
                if self._node is not None:
                    options = {**options, "node": self._node}
                if self._tab is None and operation not in {"probe", "login", "logout"}:
                    await self._adopt_tab()
                if self._tab and operation not in {"probe", "logout"}:
                    options = {**options, "tab": self._tab, "keep_open": True}
                res = await self._client().run(
                    script=script(operation),
                    args=args or {},
                    timeout=TIMEOUTS.get(operation, 60),
                    **target,
                    **options,
                )
                if options.get("keep_open") and res.page:
                    self._tab = res.page
            except (BrowserDaemonError, aiohttp.ClientError, TimeoutError) as err:
                if isinstance(err, RunError) and "not found in the session" in (
                    err.run.error or ""
                ):
                    self._tab = self._mfa_started = None
                code, uncertain, stage, detail = _failure(err)
                raise WorkerError(
                    "write_uncertain" if write and uncertain else code, stage=stage, detail=detail
                ) from None
        return res

    def _path(self) -> str:
        if not self.profile:
            raise WorkerError("session_expired")
        return self.profile

    # The client interface used by the coordinator, flows and services

    async def health(self) -> dict:
        await self._run("probe")
        self.capabilities = ["fresh_login", "day", "account", "logout"]
        return {
            "protocol_version": 1,
            "writes_enabled": self.allow_writes,
            "capabilities": list(self.capabilities),
        }

    async def login(
        self, company: str, username: str, password: str, *, force: bool = False
    ) -> dict:
        values = (company, username, password)
        if any(not isinstance(v, str) or not v.strip() for v in values):
            raise WorkerError("invalid_request")
        identity = profile_id(company.strip(), username.strip())
        # keep_open: the tab that signs in keeps the login and stays (login.js closes it only
        # when the login fails). A tab which vanished is replaced by a new one.
        args = {
            "company": company.strip(),
            "username": username.strip(),
            "password": password,
            "force": bool(force),
        }
        had_tab = self._tab
        try:
            res = await self._run_login(args, had_tab)
        except WorkerError:
            await self._close_tab()
            raise
        result = res.result if isinstance(res.result, dict) else {}
        if result.get("mfa_required"):
            self._mfa_started = time.monotonic()
        elif not result.get("authenticated"):
            await self._close_tab()
            raise WorkerError("portal_changed")
        else:
            self._mfa_started = None
        self.profile = identity
        return {"profile_id": identity, **result}

    async def _run_login(self, args: dict, had_tab: str | None):
        try:
            return await self._run("login", args, keep_open=True)
        except WorkerError as err:
            if err.code != "session_expired" or not had_tab or self._tab is not None:
                raise
        return await self._run("login", args, keep_open=True)

    async def mfa(self, code: str) -> dict:
        identity = self._path()
        code = code.strip()
        started = self._mfa_started
        if (
            not re.fullmatch(r"[0-9]{6}", code)
            or started is None
            or time.monotonic() - started > MFA_WINDOW.total_seconds()
        ):
            raise WorkerError("invalid_code")
        if not self._tab:
            raise WorkerError("session_expired")
        try:
            await self._run("mfa", {"code": code})
        except WorkerError as err:
            if err.code in {"session_expired", "browser_unavailable", "cannot_connect"}:
                self._mfa_started = None
            raise
        self._mfa_started = None
        return {"profile_id": identity, "authenticated": True}

    async def snapshot(self, options: dict) -> dict:
        self._path()
        for key in ("calendar_past_days", "calendar_future_days"):
            if type(options.get(key)) is not int or not 0 <= options[key] <= 730:
                raise WorkerError("invalid_request")
        for key in ("include_sickness", "include_pending"):
            if type(options.get(key)) is not bool:
                raise WorkerError("invalid_request")
        started = time.monotonic()
        today = datetime.now(BERLIN).date()
        start = today - timedelta(days=options["calendar_past_days"])
        end = today + timedelta(days=options["calendar_future_days"] + 1)
        res = await self._run(
            "snapshot",
            {
                "year": today.year,
                "today": today.isoformat(),
                "years": list(range(start.year, end.year + 1)),
                "kinds": ["leave", "sickness"] if options["include_sickness"] else ["leave"],
            },
        )
        try:
            result = assemble_snapshot(res.result, options, today, start, end)
        except (KeyError, TypeError, ValueError):
            raise WorkerError("portal_changed") from None
        seconds = time.monotonic() - started
        result["timings"]["total"] = round(seconds, 2)
        result["diagnostics"] = await self._diagnostics(res.result, seconds)
        return result

    async def _diagnostics(self, raw: dict, seconds: float) -> dict:
        """Where and how a snapshot ran: no personal data, no secrets."""
        session = None
        if self.browser_profile:
            try:
                found = await self._client().find_profile(self.browser_profile)
                session = (found or {}).get("session_id")
            except BrowserDaemonError:
                pass
        return {
            "worker_seconds": round(seconds, 2),
            "backend": "browser-daemon",
            "browser_version": raw.get("browser_version"),
            "browser_session": session,
            "browser_endpoint": urlsplit(str(self.settings.get("browser_daemon_url"))).netloc
            or None,
            "connect_seconds": None,
            "connections": self._connections,
        }

    async def day(self, day: str, *, reuse: bool = False) -> dict:
        self._path()
        try:
            parsed = date.fromisoformat(day)
        except (TypeError, ValueError):
            raise WorkerError("invalid_request") from None
        res = await self._run("day", {"date": parsed.isoformat()})
        try:
            return {
                "date": parsed.isoformat(),
                "metrics": dict(res.result["metrics"]),
                "entries": list(res.result["entries"]),
            }
        except (KeyError, TypeError, ValueError):
            raise WorkerError("portal_changed") from None

    async def account(self) -> dict:
        self._path()
        today = datetime.now(BERLIN).date()
        res = await self._run("account", {"year": today.year})
        try:
            metrics, account = account_result(res.result, today)
        except (KeyError, TypeError, ValueError):
            raise WorkerError("portal_changed") from None
        return {"metrics": metrics, "account": account}

    async def logout(self) -> dict:
        if not self.profile:
            return {"logged_out": True}
        await self._run("logout")
        await self._close_tab()
        return {"logged_out": True}

    async def record(self, data: dict) -> dict:
        self._path()
        if type(data.get("dry_run", True)) is not bool:
            raise WorkerError("invalid_request")
        dry_run = data.get("dry_run", True)
        if not dry_run and not self.allow_writes:
            raise WorkerError("writes_disabled")
        args = record_arguments(data, now=datetime.now(BERLIN))
        res = await self._run("record", {**args, "dry_run": dry_run}, write=not dry_run)
        if not isinstance(res.result, dict):
            raise WorkerError("write_uncertain" if not dry_run else "portal_changed")
        return res.result

    async def assist(self, data: dict) -> dict:
        """Preview or save the rows the portal's AI assistance proposes for a prompt."""
        self._path()
        dry_run = data.get("dry_run", True)
        prompt, rows = data.get("prompt"), data.get("rows", [])
        if (
            type(dry_run) is not bool
            or not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > 20000
            or not isinstance(rows, list)
        ):
            raise WorkerError("invalid_request")
        if not dry_run and not self.allow_writes:
            raise WorkerError("writes_disabled")
        args = {"prompt": prompt, "rows": [assist_row(row) for row in rows], "dry_run": dry_run}
        res = await self._run("assist", args, write=not dry_run)
        if not isinstance(res.result, dict):
            raise WorkerError("write_uncertain" if not dry_run else "portal_changed")
        return res.result

    async def team_calendar(self, options: dict | None = None) -> dict:
        """Fetch team/company absence calendar."""
        self._path()
        res = await self._run("team_calendar", options or {})
        if not isinstance(res.result, dict):
            raise WorkerError("portal_changed")
        return res.result

    async def company_absences(self, options: dict | None = None) -> dict:
        """Fetch absences of a batch of organizational units (or list the units)."""
        self._path()
        res = await self._run("company_absences", options or {})
        if not isinstance(res.result, dict):
            raise WorkerError("portal_changed")
        return res.result


def assist_row(row: dict) -> dict:
    """One expected AI-assistance row, as arguments for assist.js."""
    try:
        date.fromisoformat(row["date"])
        for field in ("start", "end"):
            datetime.strptime(row[field], "%H:%M")
        kind = row.get("type", "working_time")
        if kind not in TYPE_LABELS:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise WorkerError("invalid_request") from None
    return {
        "date": row["date"],
        "start": row["start"],
        "end": row["end"],
        "labels": list(TYPE_LABELS[kind]),
    }


def account_result(raw: dict, today: date) -> tuple[dict, dict]:
    """The ``(metrics, account)`` pair from account.js' answer."""
    period = raw["period"]
    if period is None:
        return dict(raw["metrics"]), {"period": None, "current_month": False}
    basis = raw["balance_basis"]
    return dict(raw["metrics"]), {
        "period": period,
        "current_month": period == today.strftime("%Y-%m"),
        "balance_basis": basis,
        "as_of": (today - timedelta(days=1)).isoformat() if basis == "previous_day" else None,
    }


PENDING = {
    "applied for",
    "pending",
    "requested",
    "beantragt",
    "in approval",
    "awaiting approval",
}


def normalize(value: str) -> str:
    return " ".join(value.replace("’", "'").split()).casefold().rstrip(":").strip()


def assemble_snapshot(raw: dict, options: dict, today: date, start: date, end: date) -> dict:
    """Merge snapshot.js' answer like ``EmployeePortal.snapshot`` does."""
    metrics, account = account_result(raw["account"], today)
    event_map = {}
    for item in raw["lists"]:
        if item["year"] == today.year:
            metrics.update(item.get("vacation", {}))
        for event in item["events"]:
            if event["end"] <= start.isoformat() or event["start"] >= end.isoformat():
                continue
            if not event["approved"]:
                pending = normalize(event["status"]) in PENDING
                if not options["include_pending"] or not pending:
                    continue
            event_map[event["uid"]] = event
    metrics.update(raw["today"]["metrics"])
    entries = raw["today"]["entries"]
    clocked_in = any(e.get("start") and not e.get("end") for e in entries)
    if raw.get("clock") == "in_available":
        clocked_in = False
    elif raw.get("clock") == "out_available":
        clocked_in = True
    timings = {step: round(float(seconds), 2) for step, seconds in raw["timings"].items()}
    return {
        "metrics": metrics,
        "account": account,
        "today": {"date": today.isoformat()},
        "vacation_year": today.year,
        "events": sorted(event_map.values(), key=lambda e: (e["start"], e["uid"])),
        "calendar_window": {"start": start.isoformat(), "end": end.isoformat()},
        "clocked_in": clocked_in,
        # Intervals only; comments stay in the portal.
        "today_entries": [{k: e[k] for k in ("start", "end", "type") if e.get(k)} for e in entries],
        "fetched_at": datetime.now(BERLIN).isoformat(),
        "timings": timings,
    }


def record_arguments(data: dict, *, now: datetime) -> dict:
    """Validate one interval, as arguments for record.js."""
    try:
        start = datetime.fromisoformat(data["start"])
        end = datetime.fromisoformat(data["end"])
        kind = data.get("type", "working_time")
        comment = data.get("comment", "")
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError
        start, end = start.astimezone(BERLIN), end.astimezone(BERLIN)
        if (
            start.date() != end.date()
            or start >= end
            or end > now
            or start.second
            or end.second
            or start.microsecond
            or end.microsecond
            or not isinstance(comment, str)
            or len(comment) > 500
            or kind not in TYPE_LABELS
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise WorkerError("invalid_interval") from None
    return {
        "date": start.date().isoformat(),
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "type": kind,
        "labels": list(TYPE_LABELS[kind]),
        "comment": comment,
    }
