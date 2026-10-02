"""Shared employee authentication for setup, repairs and read recovery."""

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .const import CONF_COMPANY, CONF_TOTP_URI
from .totp import InvalidTotp, parameters, totp_code


def secret_settings(current: dict, supplied: dict) -> dict:
    """Blank fields retain secrets; explicit removal takes precedence."""
    settings = dict(current)
    if supplied.get(CONF_PASSWORD):
        settings[CONF_PASSWORD] = supplied[CONF_PASSWORD]
    if supplied.get("clear_totp"):
        settings.pop(CONF_TOTP_URI, None)
    elif supplied.get(CONF_TOTP_URI, "").strip():
        uri = supplied[CONF_TOTP_URI].strip()
        # HR WORKS accepts six digits. Other URI parameters are honoured.
        if parameters(uri)[2] != 6:
            raise InvalidTotp()
        settings[CONF_TOTP_URI] = uri
    return settings


async def authenticate(client, settings: dict, *, force: bool = False) -> dict:
    result = await client.login(
        settings[CONF_COMPANY], settings[CONF_USERNAME], settings[CONF_PASSWORD], force=force
    )
    if result.get("mfa_required") and settings.get(CONF_TOTP_URI):
        await client.mfa(totp_code(settings[CONF_TOTP_URI]))
        result = {**result, "mfa_required": False, "authenticated": True}
    return result


async def complete_mfa(client, current: dict, supplied: dict) -> dict:
    settings = secret_settings(current, supplied)
    code = supplied.get("code", "").strip()
    if not code:
        if not settings.get(CONF_TOTP_URI):
            raise InvalidTotp()
        code = totp_code(settings[CONF_TOTP_URI])
    await client.mfa(code)
    return settings
