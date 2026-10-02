"""Private TOML profiles, environment overrides, and optional rbw credentials."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import tomllib
from pathlib import Path

from .transport import WorkerError

ENV_KEYS = {
    "HRWORKS_COMPANY_ID": "company_id",
    "HRWORKS_USERNAME": "username",
    "HRWORKS_PASSWORD": "password",
    "HRWORKS_TOTP_URI": "totp_uri",
    "HRWORKS_SSH_HOST": "ssh_host",
    "HRWORKS_SSH_USERNAME": "ssh_username",
    "HRWORKS_SSH_KEY": "ssh_key_path",
    "HRWORKS_WORKER": "worker_command",
    "HRWORKS_CDP_URL": "cdp_url",
    "HRWORKS_RBW_ENTRY": "rbw_entry",
}
SECRETS = {"password", "totp_uri"}
DEFAULTS = {"transport": "local", "worker_command": "hrworks-worker", "timezone": "Europe/Berlin"}


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
        result = {**DEFAULTS, **self.read().get("profiles", {}).get(name, {})}
        result.update({key: os.environ[env] for env, key in ENV_KEYS.items() if env in os.environ})
        if "HRWORKS_SSH_PORT" in os.environ:
            try:
                result["ssh_port"] = int(os.environ["HRWORKS_SSH_PORT"])
            except ValueError:
                raise WorkerError("invalid_config") from None
        return result

    def save(self, name: str, settings: dict):
        data = self.read()
        data.setdefault("profiles", {})[name] = dict(settings)
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
                field = next(
                    (
                        f
                        for f in item.get("fields", [])
                        if f.get("name", "").casefold() == "company id"
                    ),
                    None,
                )
                if field:
                    result["company_id"] = field.get("value")
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            raise WorkerError("credential_provider") from None
    return result


def redacted(settings: dict) -> dict:
    return {
        key: ("[configured]" if value else "[unset]") if key in SECRETS else value
        for key, value in settings.items()
    }
