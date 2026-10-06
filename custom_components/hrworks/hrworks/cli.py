"""Modern employee CLI with shared transports, Rich help and TSV output."""

from __future__ import annotations

import asyncio
import calendar
import os
import shlex
import sys
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.console import Console
from rich.prompt import Confirm, Prompt
from rich.text import Text

from . import __version__
from .auth import authenticate
from .cache import CachedReads, ReadCache
from .client import WorkerClient
from .config import ConfigStore, credentials, redacted
from .models import TYPE_LABELS, calendar_ics, duration, import_csv, interval
from .output import emit
from .totp import InvalidTotp, parameters
from .transport import LocalTransport, OpenSshTransport, WorkerError

app = typer.Typer(
    help="HR WORKS employee tools · pretty TSV in terminals, plain TSV in pipes.",
    no_args_is_help=True,
    invoke_without_command=True,
    pretty_exceptions_enable=False,
)
auth_app = typer.Typer(
    help="Employee login, TOTP and saved browser sessions.", no_args_is_help=True
)
times_app = typer.Typer(help="Read, preview and record completed intervals.", no_args_is_help=True)
calendar_app = typer.Typer(
    help="Leave and sickness data and calendar exports.", no_args_is_help=True
)
cache_app = typer.Typer(help="Manage private cached employee reads.", no_args_is_help=True)
config_app = typer.Typer(
    help="Private profiles and credential-provider configuration.", no_args_is_help=True
)
for name, group in [
    ("auth", auth_app),
    ("times", times_app),
    ("calendar", calendar_app),
    ("config", config_app),
    ("cache", cache_app),
]:
    app.add_typer(group, name=name)

ERROR_MESSAGES = {
    "cache_unavailable": "Cannot access the private read cache. Check HRWORKS_CACHE_DIR permissions or remove a damaged cache database.",
    "unsupported_mfa": "This employee login needs an unsupported second factor. Use authenticator TOTP.",
    "busy": "The worker is busy. Try again shortly.",
    "not_found": "Session or operation not found. Authenticate again or update the worker.",
    "invalid_request": "The worker rejected the request. Check input and update the client and worker together.",
    "cannot_connect": "Cannot reach the worker. Check the executable or SSH connection.",
    "ssh_host_key": "SSH host key verification failed. Verify the host before trusting its key.",
    "worker_auth": "SSH authentication failed. Check the key and username.",
    "invalid_auth": "Employee login failed. Check your credentials.",
    "mfa_required": "MFA is required. Run hrworks auth login interactively or configure a TOTP URI.",
    "session_expired": "Session expired. Run hrworks auth login or configure credentials for automatic recovery.",
    "invalid_code": "Authenticator code rejected. Try a fresh code.",
    "invalid_totp": "Use a valid otpauth://totp/... URI with six digits.",
    "incompatible_worker": "Update the browser worker and CLI together.",
    "portal_changed": "The portal layout could not be read reliably. Update the adapter.",
    "browser_unavailable": "The worker cannot reach Chromium. Check its CDP endpoint.",
    "credential_provider": "Cannot read the rbw entry. Check that the vault is unlocked and the entry exists.",
    "invalid_config": "Invalid configuration. Run hrworks config show and config init.",
    "missing_credentials": "Company ID, username and password are required for login.",
    "profile_not_found": "The requested profile does not exist.",
    "invalid_interval": "Use a completed same-day interval with minute precision and a supported type.",
    "invalid_import": "CSV requires date,start,end and optional type,comment columns.",
    "unfinished_interval": "The import contains an unfinished interval. Remove it before importing.",
    "overlap": "An interval overlaps an existing or imported interval. Nothing will replace it.",
    "writes_disabled": "Writes are disabled on the worker. Enable them declaratively before saving.",
    "write_uncertain": "Submission status is uncertain. Review the day in HR WORKS before retrying.",
    "invalid_range": "Dates must be ISO dates in order, within 730 days of today.",
}


@dataclass
class Runtime:
    store: ConfigStore
    profile: str
    as_json: bool = False
    no_color: bool = False
    non_interactive: bool = False
    no_cache: bool = False
    cache_ttl: int | None = None

    @property
    def settings(self):
        return self.store.profile(self.profile)

    def output(self, data, rows=None):
        emit(data, as_json=self.as_json, no_color=self.no_color, rows=rows)

    def cache(self):
        return ReadCache(self.store.path, self.profile, self.settings)

    def client(self, settings):
        if settings.get("transport") == "ssh":
            if not settings.get("ssh_host"):
                raise WorkerError("invalid_config")
            return WorkerClient(settings)
        if settings.get("transport") == "openssh":
            return WorkerClient(settings, transport=OpenSshTransport(settings))
        if settings.get("transport") != "local":
            raise WorkerError("invalid_config")
        command = shlex.split(settings.get("worker_command", "hrworks worker")) + ["--stdio"]
        for key, flag in [("cdp_url", "--cdp-url"), ("state_dir", "--state-dir")]:
            if settings.get(key):
                command.extend([flag, str(settings[key])])
        if settings.get("enable_writes"):
            command.append("--enable-writes")
        return WorkerClient(settings, transport=LocalTransport(command))

    def run(self, operation: Callable, *, login=False, cached=True):
        async def execute():
            settings = self.settings
            async with self.client(settings) as worker:
                client = (
                    CachedReads(
                        worker, self.cache(), settings, bypass=self.no_cache, ttl=self.cache_ttl
                    )
                    if cached
                    else worker
                )
                if login:
                    resolved = await asyncio.to_thread(credentials, settings)
                    await self.sign_in(client, resolved)
                    return await operation(client)
                try:
                    return await operation(client)
                except WorkerError as error:
                    if error.code not in {"session_expired", "mfa_required"}:
                        raise
                    resolved = await asyncio.to_thread(credentials, settings)
                    if not all(resolved.get(key) for key in ("company_id", "username", "password")):
                        raise error
                    await self.sign_in(client, resolved)
                    # Cache under the authenticated account identity, including first login.
                    if cached:
                        client = CachedReads(
                            worker,
                            self.cache(),
                            self.settings,
                            bypass=self.no_cache,
                            ttl=self.cache_ttl,
                        )
                    # Only read operations use this method's recovery path.
                    return await operation(client)

        return asyncio.run(execute())

    async def sign_in(self, client, settings, *, force=False, code=None):
        if not all(settings.get(key) for key in ("company_id", "username", "password")):
            raise WorkerError("missing_credentials")
        result = await authenticate(client, settings, force=force)
        if result.get("mfa_required"):
            if code:
                await client.mfa(code)
            elif (
                self.non_interactive
                or self.as_json
                or (not sys.stdin.isatty() or not sys.stdout.isatty())
            ):
                raise WorkerError("mfa_required")
            else:
                code = await asyncio.to_thread(
                    Prompt.ask, "Authenticator code", password=True, console=Console(stderr=True)
                )
                await client.mfa(code)
        # Keep only public profile and provider configuration on disk.
        saved = self.store.read().get("profiles", {}).get(self.profile, {})
        self.store.save(self.profile, {**saved, "profile_id": client.profile})
        return {"authenticated": True, "profile": self.profile}


def runtime(ctx: typer.Context) -> Runtime:
    return ctx.find_root().obj


@app.callback()
def root(
    ctx: typer.Context,
    as_json: Annotated[
        bool, typer.Option("--json", help="Emit one undecorated JSON document.")
    ] = False,
    profile: Annotated[
        str,
        typer.Option("--profile", "-p", envvar="HRWORKS_PROFILE", help="Named employee profile."),
    ] = "default",
    config: Annotated[
        Path | None,
        typer.Option("--config", envvar="HRWORKS_CONFIG", help="Private TOML configuration file."),
    ] = None,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable colors.")] = False,
    non_interactive: Annotated[
        bool, typer.Option("--non-interactive", help="Never prompt.")
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache", "--nocache", "-N", help="Fetch fresh reads and refresh cached data."
        ),
    ] = False,
    cache_ttl: Annotated[
        int | None,
        typer.Option(
            "--cache-ttl",
            envvar="HRWORKS_CACHE_TTL",
            min=0,
            max=604800,
            help="Override read cache lifetime in seconds (0 bypasses caching).",
        ),
    ] = None,
    version: Annotated[
        bool, typer.Option("--version", is_eager=True, help="Print the installed version.")
    ] = False,
):
    ctx.obj = Runtime(
        ConfigStore(config), profile, as_json, no_color, non_interactive, no_cache, cache_ttl
    )
    if version:
        runtime(ctx).output({"version": __version__})
        raise typer.Exit()


@app.command()
def doctor(ctx: typer.Context):
    """Check worker protocol, Chromium connectivity and write capabilities."""
    rt = runtime(ctx)
    result = rt.run(lambda client: client.health())
    rt.output(result)


@app.command()
def worker(
    _ctx: typer.Context,
    cdp_url: Annotated[str, typer.Option("--cdp-url")] = "http://127.0.0.1:9222",
    browser_backend: Annotated[Literal["cdp", "steel"], typer.Option("--browser-backend")] = "cdp",
    steel_api_url: Annotated[str, typer.Option("--steel-api-url")] = "http://127.0.0.1:3002",
    state_dir: Annotated[Path | None, typer.Option("--state-dir")] = None,
    enable_writes: Annotated[bool, typer.Option("--enable-writes")] = False,
    _stdio: Annotated[bool, typer.Option("--stdio", hidden=True)] = False,
):
    """Run the JSON-line browser worker for local use or SSH transport."""
    from hrworks_worker.server import run_worker

    run_worker(
        cdp_url=cdp_url,
        backend=browser_backend,
        steel_api_url=steel_api_url,
        state_dir=state_dir or Path.home() / ".local/state/hrworks-worker",
        enable_writes=enable_writes,
    )


@app.command()
def snapshot(
    ctx: typer.Context,
    past_days: Annotated[int, typer.Option(min=0, max=730)] = 365,
    future_days: Annotated[int, typer.Option(min=0, max=730)] = 365,
    include_pending: bool = False,
    include_sickness: bool = True,
):
    """Fetch the same complete data used by Home Assistant."""
    rt = runtime(ctx)
    result = rt.run(
        lambda client: client.snapshot(
            {
                "calendar_past_days": past_days,
                "calendar_future_days": future_days,
                "include_pending": include_pending,
                "include_sickness": include_sickness,
            }
        )
    )
    rt.output(result)


@app.command()
def balance(ctx: typer.Context):
    """Show monthly work, targets, carryover and total balance."""
    rt = runtime(ctx)
    data = rt.run(lambda client: client.account())
    rows = [
        {
            "metric": key,
            "time": duration(value),
            "minutes": value,
            "period": data["account"].get("period"),
            "basis": data["account"].get("balance_basis"),
        }
        for key, value in data["metrics"].items()
    ]
    rt.output(data, rows)


@app.command()
def status(ctx: typer.Context):
    """Show today's credited hours and whether the employee is clocked in."""
    rt = runtime(ctx)
    data = rt.run(
        lambda client: client.snapshot(
            {
                "calendar_past_days": 0,
                "calendar_future_days": 0,
                "include_pending": False,
                "include_sickness": True,
            }
        )
    )
    row = {
        "date": data["today"]["date"],
        "clocked_in": data["clocked_in"],
        "worked": duration(data["metrics"].get("today_worked")),
        "target": duration(data["metrics"].get("today_target")),
        "balance": duration(data["metrics"].get("today_balance")),
    }
    rt.output(row)


@app.command()
def completion(ctx: typer.Context, shell: str):
    """Print a shell completion script: bash, zsh, fish, powershell or pwsh."""
    from typer.completion import get_completion_script

    if shell not in {"bash", "zsh", "fish", "powershell", "pwsh"}:
        raise WorkerError("invalid_config")
    script = get_completion_script(
        prog_name="hrworks", complete_var="_HRWORKS_COMPLETE", shell=shell
    )
    if runtime(ctx).as_json:
        runtime(ctx).output({"shell": shell, "script": script})
    else:
        sys.stdout.write(script + "\n")


@times_app.command("types")
def times_types(ctx: typer.Context):
    """List supported working-time type keys; portal availability is checked on save."""
    runtime(ctx).output([{"type": kind, "label": TYPE_LABELS[kind][0]} for kind in TYPE_LABELS])


@auth_app.command("login")
def login(
    ctx: typer.Context,
    force: Annotated[bool, typer.Option(help="Authenticate in a fresh browser context.")] = False,
    code_stdin: Annotated[bool, typer.Option(help="Read a one-time MFA code from stdin.")] = False,
):
    """Authenticate from a configured vault, environment, or private secrets."""
    rt = runtime(ctx)

    rt.cache().invalidate()
    code = sys.stdin.read().strip() if code_stdin else None

    async def operation():
        settings = await asyncio.to_thread(credentials, rt.settings)
        if not rt.non_interactive and not rt.as_json and sys.stdin.isatty() and sys.stdout.isatty():
            for key, label in [
                ("company_id", "Company ID"),
                ("username", "Username"),
                ("password", "Password"),
            ]:
                if not settings.get(key):
                    settings[key] = await asyncio.to_thread(
                        Prompt.ask, label, password=key == "password", console=Console(stderr=True)
                    )
        async with rt.client(rt.settings) as client:
            return await rt.sign_in(client, settings, force=force, code=code)

    result = asyncio.run(operation())
    rt.cache().invalidate()
    rt.output(result)


@auth_app.command("logout")
def logout(ctx: typer.Context):
    """Remove this employee's browser cookies and saved profile association."""
    rt = runtime(ctx)
    rt.cache().invalidate()

    async def operation():
        async with rt.client(rt.settings) as client:
            result = await client.logout()
        saved = rt.store.read().get("profiles", {}).get(rt.profile, {})
        saved.pop("profile_id", None)
        rt.store.save(rt.profile, saved)
        return result

    rt.output(asyncio.run(operation()))


@auth_app.command("status")
def auth_status(ctx: typer.Context):
    """Verify that the saved employee browser session is usable."""
    rt = runtime(ctx)
    data = rt.run(lambda client: client.account(), cached=False)
    rt.output(
        {"authenticated": True, "profile": rt.profile, "period": data["account"].get("period")}
    )


@times_app.command("list")
def times_list(
    ctx: typer.Context,
    day: Annotated[
        str | None,
        typer.Option("--date", help="ISO day; overrides the default current-month view."),
    ] = None,
    month: Annotated[str | None, typer.Option(help="Read every day of YYYY-MM.")] = None,
):
    """List this month's separate intervals, including open intervals."""
    rt = runtime(ctx)
    if day and month:
        raise WorkerError("invalid_range")
    current_month = day is None and month is None
    try:
        if current_month:
            month = date.today().strftime("%Y-%m")
        if month:
            year, number = map(int, month.split("-"))
            days = [
                date(year, number, index)
                for index in range(
                    1,
                    (date.today().day if current_month else calendar.monthrange(year, number)[1])
                    + 1,
                )
            ]
        else:
            days = [date.fromisoformat(day) if day else date.today()]
    except ValueError:
        raise WorkerError("invalid_range") from None

    async def operation(client):
        return [
            await client.day(value.isoformat(), reuse=index > 0) for index, value in enumerate(days)
        ]

    data = rt.run(operation)
    rows = [{"date": item["date"], **entry} for item in data for entry in item["entries"]]
    if not rows:
        rows = [{"date": month or days[0].isoformat(), "status": "No working-time entries"}]
    rt.output(data, rows)


@times_app.command("record")
def record(
    ctx: typer.Context,
    start: Annotated[str, typer.Option(help="HH:MM or ISO timestamp including offset.")],
    end: Annotated[str, typer.Option(help="HH:MM or ISO timestamp including offset.")],
    day: Annotated[str, typer.Option("--date")] = date.today().isoformat(),
    kind: Annotated[
        str,
        typer.Option(
            "--type",
            help="working_time, doctors_appointment, business_errand, education_and_training",
        ),
    ] = "working_time",
    comment: str = "",
    save: Annotated[bool, typer.Option(help="Submit after preview and confirmation.")] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Confirm saving non-interactively.")
    ] = False,
):
    """Preview a completed interval. Saving requires explicit --save."""
    rt = runtime(ctx)
    data = interval(day, start, end, kind=kind, comment=comment, timezone=rt.settings["timezone"])
    _submit(rt, [data], save=save, yes=yes)


@times_app.command("import")
def times_import(
    ctx: typer.Context,
    file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="CSV: date,start,end,type,comment.")
    ],
    save: Annotated[bool, typer.Option(help="Submit the entire validated proposal.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
):
    """Preview or import separate CSV intervals; never merge lunch breaks."""
    rt = runtime(ctx)
    data = import_csv(file.read_text(encoding="utf-8-sig"), timezone=rt.settings["timezone"])
    _submit(rt, data, save=save, yes=yes)


def _submit(rt: Runtime, entries: list[dict], *, save: bool, yes: bool):
    async def operation():
        async with rt.client(rt.settings) as client:

            async def preview():
                return [
                    {**entry, **await client.record({**entry, "dry_run": True})}
                    for entry in entries
                ]

            try:
                previews = await preview()
            except WorkerError as error:
                if error.code not in {"session_expired", "mfa_required"}:
                    raise
                await rt.sign_in(client, await asyncio.to_thread(credentials, rt.settings))
                previews = await preview()
            if not save:
                return {"dry_run": True, "entries": previews}
            if not yes:
                if (
                    rt.as_json
                    or rt.non_interactive
                    or (not sys.stdin.isatty() or not sys.stdout.isatty())
                ):
                    raise WorkerError("confirmation_required")
                emit(previews, no_color=rt.no_color)
                if not Confirm.ask(
                    "Save these intervals in HR WORKS?", default=False, console=Console(stderr=True)
                ):
                    return {"cancelled": True, "entries": previews}
            if not (await client.health()).get("writes_enabled"):
                raise WorkerError("writes_disabled")
            results = []
            for index, entry in enumerate(entries):
                cache = rt.cache()
                affected_day = entry["start"][:10]
                try:
                    cache.invalidate(affected_day)
                    try:
                        result = await client.record({**entry, "dry_run": False})
                    except BaseException:
                        # Preserve uncertain-write/cancellation errors even if cleanup fails.
                        with suppress(WorkerError):
                            cache.invalidate(affected_day)
                        raise
                    results.append(result)
                    cache.invalidate(affected_day)
                except WorkerError as error:
                    # Report partial progress and stop. Never retry an uncertain write.
                    return {
                        "complete": False,
                        "saved_count": sum(bool(item.get("saved")) for item in results),
                        "results": results,
                        "failed_index": index,
                        "error": {
                            "code": error.code,
                            "message": ERROR_MESSAGES.get(error.code, error.code),
                        },
                    }
            return {"complete": True, "entries": results}

    result = asyncio.run(operation())
    rows = result.get("entries", result.get("results"))
    if result.get("complete") is False:
        rows = [
            *(rows or []),
            {
                "error": result["error"]["code"],
                "message": result["error"]["message"],
                "failed_index": result["failed_index"],
            },
        ]
    rt.output(result, rows=rows)
    if result.get("complete") is False:
        raise typer.Exit(4 if result["error"]["code"] == "write_uncertain" else 1)


def events(rt, lower, upper, kind, pending):
    try:
        first = date.fromisoformat(lower) if lower else date.today() - timedelta(days=365)
        last = date.fromisoformat(upper) if upper else date.today() + timedelta(days=365)
        today = date.today()
        if first > last or abs((first - today).days) > 730 or abs((last - today).days) > 730:
            raise ValueError
        if kind not in {"all", "leave", "sickness"}:
            raise ValueError
    except ValueError:
        raise WorkerError("invalid_range") from None
    snapshot = rt.run(
        lambda client: client.snapshot(
            {
                "calendar_past_days": max(0, (today - first).days),
                "calendar_future_days": max(0, (last - today).days),
                "include_sickness": kind != "leave",
                "include_pending": pending,
            }
        )
    )
    return [
        event
        for event in snapshot["events"]
        if (kind == "all" or event["kind"] == kind)
        and date.fromisoformat(event["start"]) <= last
        and date.fromisoformat(event["end"]) > first
    ]


@calendar_app.command("list")
def calendar_list(
    ctx: typer.Context,
    lower: Annotated[str | None, typer.Option("--from")] = None,
    upper: Annotated[str | None, typer.Option("--to")] = None,
    kind: Annotated[str, typer.Option("--kind", help="all, leave or sickness")] = "all",
    pending: bool = False,
):
    """Read all-day leave and sickness; end dates are exclusive."""
    rt = runtime(ctx)
    result = events(rt, lower, upper, kind, pending)
    rt.output(
        result,
        rows=[
            {
                key: item[key]
                for key in ("kind", "start", "end", "summary", "status", "approved", "half_day")
            }
            for item in result
        ],
    )


@calendar_app.command("export")
def calendar_export(
    ctx: typer.Context,
    output: Annotated[Path, typer.Argument(help="Write an iCalendar (.ics) file.")],
    lower: Annotated[str | None, typer.Option("--from")] = None,
    upper: Annotated[str | None, typer.Option("--to")] = None,
    kind: str = "all",
    pending: bool = False,
):
    """Export leave and sickness to a standard all-day iCalendar."""
    rt = runtime(ctx)
    result = events(rt, lower, upper, kind, pending)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", newline="") as stream:
        stream.write(calendar_ics(result))
    rt.output({"path": str(output), "events": len(result)})


@config_app.command("show")
def config_show(ctx: typer.Context):
    """Show effective configuration with secrets redacted."""
    rt = runtime(ctx)
    rt.output(redacted(rt.settings))


@config_app.command("list")
def config_list(ctx: typer.Context):
    """List named profiles without employee credentials."""
    rt = runtime(ctx)
    rows = [
        {
            "profile": name,
            "transport": settings.get("transport", "local"),
            "session": bool(settings.get("profile_id")),
            "vault": bool(settings.get("rbw_entry")),
        }
        for name, settings in rt.store.read().get("profiles", {}).items()
    ]
    rt.output(rows)


@config_app.command("init")
def config_init(
    ctx: typer.Context,
    transport: Annotated[
        str,
        typer.Option(
            help="local, ssh (built-in client, strict known hosts) or openssh "
            "(system ssh; honours ~/.ssh/config)"
        ),
    ] = "local",
    ssh_host: str | None = None,
    ssh_username: str | None = None,
    ssh_port: int | None = None,
    ssh_key: Path | None = None,
    known_hosts: Path | None = None,
    rbw_entry: str | None = None,
    company_id: str | None = None,
    username: str | None = None,
    worker: str = "hrworks worker",
    cdp_url: str | None = None,
    state_dir: Path | None = None,
    timezone: str = "Europe/Berlin",
    enable_writes: Annotated[
        bool, typer.Option(help="Pass --enable-writes to a local worker.")
    ] = False,
):
    """Create or update a profile. No secrets are accepted as command arguments."""
    rt = runtime(ctx)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise WorkerError("invalid_config") from None
    if (
        transport not in {"local", "ssh", "openssh"}
        or transport in {"ssh", "openssh"}
        and not ssh_host
        or ssh_port is not None
        and not 1 <= ssh_port <= 65535
    ):
        raise WorkerError("invalid_config")
    settings = {
        **rt.store.read().get("profiles", {}).get(rt.profile, {}),
        "transport": transport,
        "worker_command": worker,
        "timezone": timezone,
        "enable_writes": enable_writes,
    }
    for key, value in {
        "ssh_host": ssh_host,
        "ssh_username": ssh_username,
        "ssh_port": ssh_port,
        "ssh_key_path": str(ssh_key) if ssh_key else None,
        "rbw_entry": rbw_entry,
        "company_id": company_id,
        "username": username,
        "cdp_url": cdp_url,
        "state_dir": str(state_dir) if state_dir else None,
    }.items():
        if value is not None:
            settings[key] = value
    if known_hosts:
        settings["ssh_known_hosts"] = known_hosts.read_text()
    previous = rt.store.read().get("profiles", {}).get(rt.profile, {})
    if any(
        settings.get(key) != previous.get(key) for key in ("company_id", "username", "rbw_entry")
    ):
        settings.pop("profile_id", None)
    rt.cache().invalidate()
    rt.store.save(rt.profile, settings)
    rt.output(redacted(settings))


@config_app.command("secret")
def config_secret(
    ctx: typer.Context,
    field: Annotated[str, typer.Argument(help="password or totp_uri")],
    clear: bool = False,
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read the secret from stdin instead of a masked prompt.")
    ] = False,
):
    """Set or remove a private secret without exposing it in shell history."""
    rt = runtime(ctx)
    if field not in {"password", "totp_uri"}:
        raise WorkerError("invalid_config")
    settings = rt.store.read().get("profiles", {}).get(rt.profile, {})
    if clear:
        settings.pop(field, None)
    else:
        if stdin:
            value = sys.stdin.read().rstrip("\r\n")
        elif (
            not rt.non_interactive and not rt.as_json and sys.stdin.isatty() and sys.stdout.isatty()
        ):
            value = Prompt.ask(field, password=True, console=Console(stderr=True))
        else:
            raise WorkerError("confirmation_required")
        if not value:
            raise WorkerError("invalid_config")
        if field == "totp_uri" and parameters(value)[2] != 6:
            raise InvalidTotp()
        settings[field] = value
    rt.cache().invalidate()
    rt.store.save(rt.profile, settings)
    rt.output({"field": field, "configured": field in settings})


@config_app.command("remove")
def config_remove(
    ctx: typer.Context, name: str, yes: Annotated[bool, typer.Option("--yes", "-y")] = False
):
    """Remove local profile settings; browser cookies require auth logout."""
    rt = runtime(ctx)
    if not yes:
        if (
            rt.non_interactive
            or rt.as_json
            or (not sys.stdin.isatty() or not sys.stdout.isatty())
            or not Confirm.ask(
                f"Remove profile {name}?", default=False, console=Console(stderr=True)
            )
        ):
            raise WorkerError("confirmation_required")
    ReadCache(rt.store.path, name, rt.store.profile(name)).invalidate()
    rt.store.remove(name)
    rt.output({"removed": name})


@cache_app.command("clear")
def cache_clear(ctx: typer.Context, all_profiles: bool = False):
    """Clear this profile's cache, or all profiles with --all-profiles."""
    rt = runtime(ctx)
    rt.cache().invalidate(all_profiles=all_profiles)
    rt.output({"cleared": "all_profiles" if all_profiles else rt.profile})


def normalize_globals(arguments: list[str]) -> list[str]:
    """Allow global switches after subcommands without stealing option values."""
    import typer.main

    command = typer.main.get_command(app)
    value_options = set()

    def visit(cmd):
        for param in cmd.params:
            if hasattr(param, "opts") and not getattr(param, "is_flag", True):
                value_options.update(param.opts)
        for child in getattr(cmd, "commands", {}).values():
            visit(child)

    visit(command)
    flags = {
        "--json",
        "--no-color",
        "--non-interactive",
        "--version",
        "--no-cache",
        "--nocache",
        "-N",
    }
    valued = {"--config", "--profile", "-p", "--cache-ttl"}
    prefix = []
    remaining = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            remaining.extend(arguments[index:])
            break
        name = token.split("=", 1)[0]
        if name in flags | valued:
            prefix.append(token)
            if name in valued and "=" not in token and index + 1 < len(arguments):
                index += 1
                prefix.append(arguments[index])
        else:
            remaining.append(token)
            if name in value_options and "=" not in token and index + 1 < len(arguments):
                index += 1
                remaining.append(arguments[index])
        index += 1
    return prefix + remaining


def main():
    """Keep fixed errors and machine output clean; never dump tracebacks."""
    try:
        arguments = normalize_globals(sys.argv[1:])
        if "--no-color" in arguments:
            os.environ["NO_COLOR"] = "1"
        result = app(args=arguments, standalone_mode=False)
        if isinstance(result, int) and result:
            raise SystemExit(result)
    except (WorkerError, InvalidTotp, ValueError, ZoneInfoNotFoundError) as error:
        code = (
            error.code
            if isinstance(error, WorkerError)
            else ("invalid_totp" if isinstance(error, InvalidTotp) else str(error))
        )
        if code not in ERROR_MESSAGES and code not in {"confirmation_required"}:
            code = "invalid_config"
        message = ERROR_MESSAGES.get(
            code, "Saving requires --save --yes or interactive confirmation."
        )
        if "--json" in sys.argv:
            emit({"error": {"code": code, "message": message}}, as_json=True)
        else:
            Console(stderr=True).print(f"[bold red]{code}[/bold red]: {message}", markup=True)
        raise SystemExit(
            4
            if code == "write_uncertain"
            else 3
            if code
            in {
                "invalid_auth",
                "invalid_code",
                "mfa_required",
                "missing_credentials",
                "session_expired",
            }
            else 1
        ) from None
    except (OSError, KeyboardInterrupt):
        if "--json" in sys.argv:
            emit(
                {
                    "error": {
                        "code": "io_error",
                        "message": "Interrupted or unable to access a required file.",
                    }
                },
                as_json=True,
            )
        else:
            Console(stderr=True).print(
                "Interrupted or unable to access a required file.", style="red"
            )
        raise SystemExit(1) from None

    except Exception as error:
        usage = hasattr(error, "exit_code") and callable(getattr(error, "format_message", None))
        code = "usage_error" if usage else "internal_error"
        message = (
            error.format_message()
            if usage
            else "Unexpected failure. Report the command and CLI version; omit secrets."
        )
        if "--json" in sys.argv:
            emit({"error": {"code": code, "message": message}}, as_json=True)
        else:
            Console(stderr=True).print(Text(message, style="red"))
        raise SystemExit(2 if usage else 1) from None


if __name__ == "__main__":
    main()
