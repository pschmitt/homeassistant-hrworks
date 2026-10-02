"""Attach the shared Python client to Home Assistant's lifecycle."""

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from .hrworks.client import WorkerClient as SharedWorkerClient
from .hrworks.transport import WorkerError

__all__ = ["WorkerClient", "WorkerError"]


class WorkerClient(SharedWorkerClient):
    """HA only owns lifecycle; transport and protocol are shared with the CLI."""

    def __init__(self, hass, settings: dict):
        super().__init__(settings)
        self.hass = hass
        self._remove_stop = None

    async def request(self, path: str, payload: dict | None = None) -> dict:
        if self._remove_stop is None:
            self._remove_stop = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, self.close
            )
        return await super().request(path, payload)

    async def close(self, _event=None):
        remove_stop = self._remove_stop
        self._remove_stop = None
        if remove_stop and _event is None:
            remove_stop()
        await super().close()
