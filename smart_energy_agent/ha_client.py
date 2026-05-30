"""Asynchronous Home Assistant websocket client.

Self-contained implementation (modeled on SmartHub's event_server.py patterns,
but without any coupling to SmartHub). Handles:
  * connect + auth handshake (auth_required -> auth -> auth_ok/auth_invalid)
  * incrementing command ids with per-id result futures
  * a background receive loop that dispatches results and events
  * automatic reconnect with exponential backoff
  * heartbeat ping
Provides high-level helpers: get_states, get_entity_registry,
get_device_registry, get_area_registry, subscribe_state_changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Optional

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from . import const

_LOGGER = logging.getLogger(__name__)

StateEventCallback = Callable[[dict[str, Any]], Awaitable[None] | None]


class HAClient:
    """Minimal but robust HA websocket client."""

    def __init__(
        self,
        on_state_changed: Optional[StateEventCallback] = None,
        on_connected: Optional[Callable[[], Awaitable[None] | None]] = None,
    ) -> None:
        self._ws: Optional[ClientConnection] = None
        self._uri, self._token = self._resolve_connection()
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._sub_id: Optional[int] = None
        self._on_state_changed = on_state_changed
        self._on_connected = on_connected
        self._recv_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._connected = asyncio.Event()
        self._stopping = False
        self.ha_version: Optional[str] = None

    # --- connection setup ----------------------------------------------------
    @staticmethod
    def _resolve_connection() -> tuple[str, Optional[str]]:
        """Pick add-on (Supervisor) or standalone (env) connection params."""
        if const.is_addon():
            return const.SUPERVISOR_WS_URI, os.getenv(const.ENV_SUPERVISOR_TOKEN)
        # Standalone / local development.
        return (
            os.getenv(const.ENV_HA_URL, "ws://localhost:8123/api/websocket"),
            os.getenv(const.ENV_HA_TOKEN),
        )

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    # --- public lifecycle ----------------------------------------------------
    async def run_forever(self) -> None:
        """Connect and keep the connection alive with backoff reconnects."""
        backoff = const.WS_RECONNECT_MIN
        while not self._stopping:
            try:
                await self._connect_once()
                backoff = const.WS_RECONNECT_MIN  # reset after a clean session
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - reconnect on any failure
                _LOGGER.warning("HA connection lost/failed: %s", err)
            finally:
                await self._teardown()

            if self._stopping:
                break
            _LOGGER.info("Reconnecting to HA in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, const.WS_RECONNECT_MAX)

    async def stop(self) -> None:
        self._stopping = True
        await self._teardown()

    # --- internal connection handling ---------------------------------------
    async def _connect_once(self) -> None:
        if not self._token:
            raise RuntimeError("No auth token available (SUPERVISOR_TOKEN/HA_TOKEN)")

        _LOGGER.info("Connecting to HA websocket at %s", self._uri)
        self._ws = await websockets.connect(
            self._uri, open_timeout=const.WS_OPEN_TIMEOUT, max_size=None
        )

        # Auth handshake.
        greeting = json.loads(await self._ws.recv())
        if greeting.get("type") == "auth_required":
            await self._ws.send(
                json.dumps({"type": "auth", "access_token": self._token})
            )
            auth_resp = json.loads(await self._ws.recv())
            if auth_resp.get("type") != "auth_ok":
                raise RuntimeError(
                    f"Auth failed: {auth_resp.get('message', auth_resp.get('type'))}"
                )
            self.ha_version = auth_resp.get("ha_version")

        _LOGGER.info("Authenticated with Home Assistant %s", self.ha_version)
        self._connected.set()

        # Start receive + ping loops.
        self._recv_task = asyncio.create_task(self._receive_loop())
        self._ping_task = asyncio.create_task(self._ping_loop())

        if self._on_connected:
            await _maybe_await(self._on_connected())

        # Block until the receive loop ends (connection closed/error).
        await self._recv_task

    async def _teardown(self) -> None:
        self._connected.clear()
        for task in (self._ping_task, self._recv_task):
            if task and not task.done():
                task.cancel()
        self._ping_task = None
        self._recv_task = None
        # Fail any pending requests so callers don't hang forever.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("HA connection closed"))
        self._pending.clear()
        self._sub_id = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                self._dispatch(json.loads(raw))
        except ConnectionClosed:
            _LOGGER.debug("Websocket closed by peer")
        # Returning here ends the session; run_forever() will reconnect.

    def _dispatch(self, msg: dict[str, Any]) -> None:
        mtype = msg.get("type")
        if mtype == "result":
            fut = self._pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                if msg.get("success", False):
                    fut.set_result(msg.get("result"))
                else:
                    fut.set_exception(
                        RuntimeError(str(msg.get("error", "unknown error")))
                    )
        elif mtype == "event":
            if msg.get("id") == self._sub_id and self._on_state_changed:
                event = msg.get("event", {})
                if event.get("event_type") == "state_changed":
                    asyncio.create_task(
                        _maybe_await_coro(self._on_state_changed(event.get("data", {})))
                    )
        elif mtype == "pong":
            pass  # handled implicitly via result futures for ping below

    async def _ping_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(const.WS_PING_INTERVAL)
                try:
                    await asyncio.wait_for(self._command("ping"), timeout=10)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Heartbeat ping failed: %s", err)
                    if self._ws is not None:
                        await self._ws.close()
                    return
        except asyncio.CancelledError:
            return

    # --- command helpers -----------------------------------------------------
    async def _command(self, msg_type: str, **payload: Any) -> Any:
        if self._ws is None:
            raise ConnectionError("Not connected to HA")
        msg_id = self._next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._ws.send(json.dumps({"id": msg_id, "type": msg_type, **payload}))
        return await fut

    async def get_states(self) -> list[dict[str, Any]]:
        return await self._command("get_states") or []

    async def get_entity_registry(self) -> list[dict[str, Any]]:
        return await self._command("config/entity_registry/list") or []

    async def get_device_registry(self) -> list[dict[str, Any]]:
        return await self._command("config/device_registry/list") or []

    async def get_area_registry(self) -> list[dict[str, Any]]:
        return await self._command("config/area_registry/list") or []

    async def call_service(
        self, domain: str, service: str, entity_id: str
    ) -> Any:
        """Call a HA service on a single target entity (e.g. switch.turn_on)."""
        return await self._command(
            "call_service",
            domain=domain,
            service=service,
            target={"entity_id": entity_id},
        )

    async def subscribe_state_changes(self) -> None:
        """Subscribe to state_changed events; results routed to on_state_changed."""
        self._sub_id = self._next_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._sub_id] = fut
        await self._ws.send(  # type: ignore[union-attr]
            json.dumps(
                {
                    "id": self._sub_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )
        )
        await fut
        _LOGGER.info("Subscribed to state_changed events")


async def _maybe_await(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value


async def _maybe_await_coro(value: Any) -> None:
    if asyncio.iscoroutine(value):
        await value
