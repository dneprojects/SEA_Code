"""Smart Energy Agent entrypoint - wires HA client, discovery, store and web UI."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any

from . import const, discovery
from .control import ControlEngine
from .ha_client import HAClient
from .store import Store
from .web import WebServer

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _setup_logging() -> None:
    logging.basicConfig(
        level=_LOG_LEVELS.get(const.get_log_level(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


class SmartEnergyAgent:
    def __init__(self) -> None:
        self.store = Store()
        self.ha_status: dict[str, Any] = {"connected": False, "version": None}
        self.client = HAClient(
            on_state_changed=self._on_state_changed,
            on_connected=self._on_connected,
        )
        self.web = WebServer(self.store, self.ha_status)
        self.control = ControlEngine(self.store, self.client.call_service)

    async def _on_connected(self) -> None:
        """Run discovery snapshot + subscribe once the WS is authenticated."""
        self.ha_status["connected"] = True
        self.ha_status["version"] = self.client.ha_version
        try:
            states, ent_reg, dev_reg, area_reg = await asyncio.gather(
                self.client.get_states(),
                self.client.get_entity_registry(),
                self.client.get_device_registry(),
                self.client.get_area_registry(),
            )
        except Exception as err:  # noqa: BLE001
            logging.getLogger(__name__).warning("Initial fetch failed: %s", err)
            return
        entities = discovery.discover(states, ent_reg, dev_reg, area_reg)
        self.store.set_entities(entities)
        self.store.set_devices(
            discovery.discover_devices(states, ent_reg, dev_reg, area_reg)
        )
        # Keep the raw snapshot for the setup wizard's suggestion engine.
        self.store.set_ha_snapshot(states, ent_reg, dev_reg, area_reg)
        await self.client.subscribe_state_changes()
        await self._refresh_solar_forecast()
        await self._refresh_energy_prefs()

    async def _refresh_solar_forecast(self) -> None:
        """Pull the Energy-dashboard solar forecast (Forecast.Solar) into the store."""
        try:
            self.store.set_solar_forecast(await self.client.get_solar_forecast())
        except Exception as err:  # noqa: BLE001
            logging.getLogger(__name__).debug("Solar forecast fetch failed: %s", err)

    async def _refresh_energy_prefs(self) -> None:
        """Pull the HA Energy-dashboard preferences (wizard pre-fill/ranking)."""
        try:
            self.store.set_energy_prefs(await self.client.get_energy_prefs())
        except Exception as err:  # noqa: BLE001
            logging.getLogger(__name__).debug("Energy prefs fetch failed: %s", err)

    def _on_state_changed(self, data: dict[str, Any]) -> None:
        entity_id = data.get("entity_id")
        new_state = data.get("new_state")
        if entity_id and new_state:
            self.store.update_state(entity_id, new_state)
            self.store.update_device_state(entity_id, new_state)
            self.store.observe_external(entity_id, new_state)
            self.store.observe_config_state(entity_id, new_state)

    async def _recorder(self) -> None:
        """Periodically record the energy balance and purge old history."""
        last_purge = 0.0
        while True:
            await asyncio.sleep(const.RECORD_INTERVAL)
            if not self.client.connected:
                continue
            try:
                await self.store.record_state(self.store.balance())
                now = asyncio.get_running_loop().time()
                if now - last_purge >= const.PURGE_INTERVAL:
                    deleted = await self.store.purge_old()
                    last_purge = now
                    if deleted:
                        logging.getLogger(__name__).info(
                            "Purged %d old history rows", deleted
                        )
            except Exception as err:  # noqa: BLE001
                logging.getLogger(__name__).warning("Recorder error: %s", err)

    async def _control(self) -> None:
        """Periodically run the PV-surplus control engine (no-op if disabled)."""
        while True:
            await asyncio.sleep(const.CONTROL_INTERVAL)
            if not self.client.connected:
                continue
            try:
                await self.control.run_once(time.time())
            except Exception as err:  # noqa: BLE001
                logging.getLogger(__name__).warning("Control loop error: %s", err)

    async def _solar_forecast(self) -> None:
        """Periodically refresh the solar forecast + energy prefs (initial pull on connect)."""
        while True:
            await asyncio.sleep(const.SOLAR_FORECAST_INTERVAL)
            if self.client.connected:
                await self._refresh_solar_forecast()
                await self._refresh_energy_prefs()

    async def run(self) -> None:
        await self.store.open_db()
        await self.web.start()
        client_task = asyncio.create_task(self.client.run_forever())
        recorder_task = asyncio.create_task(self._recorder())
        control_task = asyncio.create_task(self._control())
        forecast_task = asyncio.create_task(self._solar_forecast())

        # Keep ha_status in sync with the live connection flag.
        async def _status_sync() -> None:
            while True:
                self.ha_status["connected"] = self.client.connected
                self.ha_status["version"] = self.client.ha_version
                await asyncio.sleep(2)

        status_task = asyncio.create_task(_status_sync())
        try:
            await client_task
        finally:
            status_task.cancel()
            recorder_task.cancel()
            control_task.cancel()
            forecast_task.cancel()
            await self.client.stop()
            await self.web.stop()
            await self.store.close_db()


async def _amain() -> None:
    _setup_logging()
    agent = SmartEnergyAgent()
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass  # not available on all platforms

    run_task = asyncio.create_task(agent.run())
    await stop_event.wait()
    await agent.client.stop()
    run_task.cancel()


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
