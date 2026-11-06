"""Create the browser-daemon client of a config entry."""

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_BD_ENTRY_ID, CONF_BD_URL
from .hrworks.browser_daemon import CLIENT_NAME, BrowserDaemonClient
from .hrworks.errors import WorkerError

__all__ = ["WorkerError", "create_client"]


def create_client(hass, settings: dict, *, allow_writes: bool = False) -> BrowserDaemonClient:
    """A client for the entry's custom connection or selected Browser Daemon integration."""
    if settings.get(CONF_BD_URL):
        return BrowserDaemonClient(
            settings, http=async_get_clientsession(hass), allow_writes=allow_writes
        )
    try:
        from custom_components.browser_daemon import BrowserDaemonError, async_get_daemon
    except ImportError as err:
        raise WorkerError("cannot_connect") from err
    try:
        daemon = async_get_daemon(
            hass, client=CLIENT_NAME, entry_id=settings.get(CONF_BD_ENTRY_ID) or None
        )
    except BrowserDaemonError as err:
        raise WorkerError("cannot_connect") from err
    # The cluster secret stays in the Browser Daemon integration.
    return BrowserDaemonClient(
        {**settings, CONF_BD_URL: daemon.url}, daemon=daemon, allow_writes=allow_writes
    )
