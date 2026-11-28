"""Private TOML profiles, environment overrides, and optional rbw credentials."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

from .errors import WorkerError

ENV_KEYS = {
    "HRWORKS_COMPANY_ID": "company_id",
    "HRWORKS_USERNAME": "username",
    "HRWORKS_PASSWORD": "password",
    "HRWORKS_TOTP_URI": "totp_uri",
    "BROWSER_DAEMON_URL": "browser_daemon_url",
    "HRWORKS_BROWSER_DAEMON_URL": "browser_daemon_url",
    "HRWORKS_BROWSER_PROFILE": "browser_daemon_profile",
    "BROWSER_DAEMON_SECRET_FILE": "browser_daemon_secret_file",
    "HRWORKS_RBW_ENTRY": "rbw_entry",
}
SECRETS = {"password", "totp_uri"}
DEFAULT_SECRET_FILE = "/run/secrets/browser-daemon/cluster-secret"
DEFAULTS = {
    "browser_daemon_url": "http://127.0.0.1:3001",
    "browser_daemon_profile": "hrworks",
    "timezone": "Europe/Berlin",
}
# Keys of the removed SSH/local worker transports; old profiles keep working without them.
LEGACY_KEYS = {
    "transport",
    "worker_command",
    "ssh_host",
    "ssh_port",
    "ssh_username",
    "ssh_key_path",
    "ssh_known_hosts",
    "cdp_url",
    "state_dir",
}


def default_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "hrworks/config.toml"


class ConfigStore:
    def __init__(self, path: Path | None = None):
        self.path = (path or default_path()).expanduser()

    def read(self) -> dict:
        if not self.path.exists():
            return {"profiles": {}}
        try:
            data = tomllib.loads(self.path.read_text())
            profiles = data.get("profiles", {})
            if not isinstance(profiles, dict) or any(
                not isinstance(value, dict) for value in profiles.values()
            ):
                raise ValueError
            return data
        except (OSError, ValueError):
            raise WorkerError("invalid_config") from None

    def profile(self, name: str) -> dict:
        saved = self.read().get("profiles", {}).get(name, {})
        result = {**DEFAULTS, **{k: v for k, v in saved.items() if k not in LEGACY_KEYS}}
        result.update({key: os.environ[env] for env, key in ENV_KEYS.items() if env in os.environ})
        return result

    def save(self, name: str, settings: dict):
        data = self.read()
        data.setdefault("profiles", {})[name] = {
            k: v for k, v in settings.items() if k not in LEGACY_KEYS
        }
        self._write(data)

    def remove(self, name: str):
        data = self.read()
        if name not in data.get("profiles", {}):
            raise WorkerError("profile_not_found")
        del data["profiles"][name]
        self._write(data)

    def _write(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lines = ["# HR WORKS employee CLI profiles. Keep this file private.", ""]
        for name, settings in data.get("profiles", {}).items():
            lines.append(f"[profiles.{json.dumps(name)}]")
            for key, value in settings.items():
                if isinstance(value, (str, int, bool)):
                    lines.append(f"{json.dumps(key)} = {json.dumps(value, ensure_ascii=False)}")
            lines.append("")
        descriptor, temporary = tempfile.mkstemp(prefix=".hrworks-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                stream.write("\n".join(lines))
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def credentials(settings: dict) -> dict:
    """Resolve a vault entry in memory; never persist the resolved secrets."""
    result = dict(settings)
    if entry := settings.get("rbw_entry"):
        try:
            raw = subprocess.check_output(
                ["rbw", "get", "-e", entry, "--raw"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
            item = json.loads(raw)
            login = item.get("data", item.get("login", {}))
            for key in ("username", "password"):
                if not result.get(key):
                    result[key] = login.get(key)
            if not result.get("totp_uri") and login.get("totp"):
                result["totp_uri"] = login["totp"]
            if not result.get("company_id"):
                fields = {
                    (f.get("name") or "").casefold(): f.get("value")
                    for f in item.get("fields") or []
                }
                if company := fields.get("company id") or fields.get("company"):
                    result["company_id"] = company
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            raise WorkerError("credential_provider") from None
    return result


def redacted(settings: dict) -> dict:
    return {
        key: ("[configured]" if value else "[unset]") if key in SECRETS else value
        for key, value in settings.items()
    }


def cluster_secret(settings: dict) -> str | None:
    """The browser-daemon cluster secret from its file; never part of the saved profile."""
    path = settings.get("browser_daemon_secret_file") or DEFAULT_SECRET_FILE
    try:
        return Path(path).expanduser().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
