"""Employee authentication with optional, fresh Home Assistant rbw fields."""

from .hrworks.auth import secret_settings as direct_secret_settings
from .hrworks.errors import WorkerError
from .hrworks.totp import InvalidTotp, totp_code

__all__ = ["authenticate", "complete_mfa", "credentials", "secret_settings"]


def secret_settings(current: dict, supplied: dict) -> dict:
    if not current.get("rbw_entry_id"):
        return direct_secret_settings(current, supplied)
    settings = dict(current)
    settings.pop("password", None)
    settings.pop("totp_uri", None)
    return settings


async def _field(hass, settings: dict, field: str) -> str:
    try:
        from custom_components.rbw import async_get_field

        result = await async_get_field(
            hass, settings["rbw_entry_id"], settings["rbw_item_id"], field
        )
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError()
        return value
    except Exception:
        raise WorkerError("rbw_unavailable") from None


async def credentials(hass, settings: dict) -> dict:
    """Resolve credentials for one attempt without persisting vault secrets."""
    entry, item = settings.get("rbw_entry_id"), settings.get("rbw_item_id")
    if bool(entry) != bool(item):
        raise WorkerError("rbw_incomplete")
    if not entry:
        return dict(settings)
    username = await _field(hass, settings, "username")
    if settings.get("username") and username.casefold() != settings["username"].casefold():
        raise WorkerError("different_account")
    return {**settings, "username": username, "password": await _field(hass, settings, "password")}


async def complete_mfa(client, current: dict, supplied: dict, *, hass=None) -> dict:
    settings = secret_settings(current, supplied)
    code = supplied.get("code", "").strip()
    if not code:
        if settings.get("rbw_entry_id"):
            code = await _field(hass, settings, "totp")
        elif settings.get("totp_uri"):
            code = totp_code(settings["totp_uri"])
        else:
            raise InvalidTotp()
    await client.mfa(code)
    return settings


async def authenticate(client, settings: dict, *, force: bool = False, hass=None) -> dict:
    resolved = await credentials(hass, settings)
    result = await client.login(
        resolved["company_id"], resolved["username"], resolved["password"], force=force
    )
    if result.get("mfa_required") and (settings.get("totp_uri") or settings.get("rbw_entry_id")):
        await complete_mfa(client, settings, {}, hass=hass)
        result = {**result, "mfa_required": False, "authenticated": True}
    return result
