"""aiohttp web server, served through HA Ingress.

Phase 1: shows the classified entities grouped by energy role with live power
values, and lets the user curate them (include/exclude, change role). Curation
is persisted via the store and survives restarts.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web

from . import const
from .models import (
    ROLE_LABELS, ROLE_ORDER, CONSUMER_TYPES, CONTROL_MODES, COMBINE_MODES,
    DEVICE_TYPE_LABELS, DEVICE_TYPE_ORDER, SUBROLE_LABELS, SUBROLE_ORDER,
    STRATEGIES,
)
from .store import Store

_RANGES = {"6h": 21600, "24h": 86400, "48h": 172800, "7d": 604800, "30d": 2592000}
_SAVINGS_RANGES = {"day": 86400, "week": 604800, "month": 2592000, "year": 31536000}

_LOGGER = logging.getLogger(__name__)


class WebServer:
    def __init__(self, store: Store, ha_status: dict[str, Any]) -> None:
        self._store = store
        self._ha_status = ha_status
        self._app = web.Application()
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/api/summary", self._api_summary)
        self._app.router.add_get("/api/flow", self._api_flow)
        self._app.router.add_get("/api/entities", self._api_entities)
        self._app.router.add_get("/api/roles", self._api_roles)
        self._app.router.add_post("/api/override", self._api_override)
        self._app.router.add_get("/api/history", self._api_history)
        self._app.router.add_get("/api/savings", self._api_savings)
        self._app.router.add_get("/api/forecast", self._api_forecast)
        self._app.router.add_get("/api/settings", self._api_settings_get)
        self._app.router.add_post("/api/settings", self._api_settings_post)
        self._app.router.add_get("/api/strategies", self._api_strategies)
        self._app.router.add_get("/api/strategy-loads", self._api_strategy_loads_get)
        self._app.router.add_post("/api/strategy-loads", self._api_strategy_loads_post)
        self._app.router.add_get("/api/consumers", self._api_consumers_get)
        self._app.router.add_post("/api/consumers", self._api_consumers_post)
        self._app.router.add_get("/api/devices", self._api_devices_get)
        self._app.router.add_post("/api/device-override", self._api_device_override)
        self._app.router.add_get("/api/setup/catalog", self._api_setup_catalog)
        self._app.router.add_get("/api/setup/suggest", self._api_setup_suggest)
        self._app.router.add_post("/api/setup/config", self._api_setup_config)
        self._app.router.add_post("/api/setup/import-prefs", self._api_setup_import)
        self._app.router.add_get("/api/categories", self._api_categories)
        self._app.router.add_get("/api/thermostats", self._api_thermostats_get)
        self._app.router.add_post("/api/thermostats", self._api_thermostats_post)
        self._app.router.add_static(
            "/static", str(const.WEB_DIR), show_index=False
        )
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, const.WEB_HOST, const.WEB_PORT)
        await site.start()
        _LOGGER.info("Web UI listening on %s:%s", const.WEB_HOST, const.WEB_PORT)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # --- handlers ------------------------------------------------------------
    async def _index(self, _request: web.Request) -> web.Response:
        index_file = const.WEB_DIR / "index.html"
        return web.FileResponse(index_file)

    async def _api_summary(self, _request: web.Request) -> web.Response:
        from . import __version__
        data = self._store.summary()
        data["version"] = __version__
        data["ha_connected"] = self._ha_status.get("connected", False)
        data["ha_version"] = self._ha_status.get("version")
        return web.json_response(data)

    async def _api_flow(self, _request: web.Request) -> web.Response:
        return web.json_response(self._store.balance())

    async def _api_entities(self, request: web.Request) -> web.Response:
        include_only = request.query.get("scope", "all") == "included"
        return web.json_response(
            {"groups": self._store.grouped(include_only=include_only)}
        )

    async def _api_roles(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {"roles": [{"value": r, "label": ROLE_LABELS.get(r, r)} for r in ROLE_ORDER]}
        )

    async def _api_override(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        entity_id = body.get("entity_id")
        if not entity_id:
            return web.json_response({"error": "entity_id required"}, status=400)
        ok = self._store.set_override(
            entity_id,
            include=body.get("include"),
            role=body.get("role"),
        )
        if not ok:
            return web.json_response({"error": "unknown entity_id"}, status=404)
        return web.json_response({"ok": True})

    async def _api_history(self, request: web.Request) -> web.Response:
        q = request.query
        try:
            frm, to = int(q["from"]), int(q["to"])
        except (KeyError, ValueError):
            frm = to = None
        if frm is not None and to is not None and to > frm:
            series = await self._store.history(start=frm, end=to)
            return web.json_response({"from": frm, "to": to, "series": series})
        rng = q.get("range", "24h")
        series = await self._store.history(_RANGES.get(rng, 86400))
        return web.json_response({"range": rng, "series": series})

    async def _api_savings(self, request: web.Request) -> web.Response:
        rng = request.query.get("range", "day")
        range_s = _SAVINGS_RANGES.get(rng, 86400)
        baseline = request.query.get("baseline", "full_feed")
        if baseline not in ("full_feed", "direct", "surplus_sink"):
            baseline = "full_feed"
        try:
            cap = float(request.query.get("cap", "3000"))
        except ValueError:
            cap = 3000.0
        data = await self._store.savings(range_s, baseline=baseline, sink_cap_w=cap)
        data["range"] = rng
        return web.json_response(data)

    async def _api_forecast(self, request: web.Request) -> web.Response:
        try:
            hours = int(request.query.get("hours", "24"))
        except ValueError:
            hours = 24
        hours = max(1, min(hours, 168))
        data = await self._store.forecast_bundle(hours=hours)
        return web.json_response(data)

    # --- setup wizard --------------------------------------------------------
    async def _api_setup_catalog(self, _request: web.Request) -> web.Response:
        return web.json_response(self._store.catalog_payload())

    async def _api_setup_suggest(self, request: web.Request) -> web.Response:
        unit_group = request.query.get("unit_group", "")
        kind = request.query.get("kind", "")
        query = request.query.get("q", "")
        return web.json_response(
            {"candidates": self._store.suggestions(unit_group, kind, query)}
        )

    async def _api_setup_config(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        config = body.get("config", body)
        return web.json_response({"ok": True, "config": self._store.set_full_config(config)})

    async def _api_setup_import(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "config": self._store.import_prefs()})

    async def _api_categories(self, _request: web.Request) -> web.Response:
        return web.json_response({"groups": self._store.categories_with_entities()})

    async def _api_thermostats_get(self, _request: web.Request) -> web.Response:
        groups = self._store.groups()
        return web.json_response({
            "setback": self._store.setback(),
            "presence": {g.get("id"): self._store.group_present(g) for g in groups},
        })

    async def _api_thermostats_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        sb = self._store.set_setback_full(body.get("setback", body))
        groups = self._store.groups()
        return web.json_response({
            "ok": True, "setback": sb,
            "presence": {g.get("id"): self._store.group_present(g) for g in groups},
        })

    async def _api_settings_get(self, _request: web.Request) -> web.Response:
        from . import __version__
        s = self._store.summary()
        data = self._store.get_settings()
        data.update({
            "version": __version__,
            "ha_connected": self._ha_status.get("connected", False),
            "ha_version": self._ha_status.get("version"),
            "db_path": const.get_history_db(),
            "effective_retention_days": self._store.retention_days(),
            "history_count": await self._store.history_count(),
            "total": s["total"],
            "included": s["included"],
            "current_price_ct": self._store.current_price_ct(),
            "strategies": STRATEGIES,
            "tariff_prefill": self._store.tariff_prefill(),
            "has_prefs": self._store.has_energy_prefs(),
        })
        return web.json_response(data)

    async def _api_settings_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        return web.json_response(self._store.set_settings(body))

    async def _api_strategies(self, _request: web.Request) -> web.Response:
        return web.json_response({"strategies": self._store.strategies_overview()})

    async def _api_strategy_loads_get(self, _request: web.Request) -> web.Response:
        return web.json_response({"devices": self._store.strategy_devices()})

    async def _api_strategy_loads_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        if not self._store.set_strategy_load(body.get("key"), body.get("patch", {})):
            return web.json_response({"error": "key required"}, status=400)
        return web.json_response({"ok": True, "devices": self._store.strategy_devices()})

    async def _api_consumers_get(self, _request: web.Request) -> web.Response:
        return web.json_response({
            "consumers": self._store.list_consumers(),
            "types": CONSUMER_TYPES,
            "modes": CONTROL_MODES,
            "combine": COMBINE_MODES,
        })

    async def _api_consumers_post(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        entity_id = body.pop("entity_id", None)
        if not entity_id:
            return web.json_response({"error": "entity_id required"}, status=400)
        ok = self._store.set_consumer(entity_id, body)
        if not ok:
            return web.json_response({"error": "unknown entity_id"}, status=404)
        return web.json_response({"ok": True})

    async def _api_devices_get(self, request: web.Request) -> web.Response:
        include_only = request.query.get("scope", "all") == "included"
        return web.json_response({
            "groups": self._store.grouped_devices(include_only=include_only),
            "summary": self._store.device_summary(),
            "types": [{"value": t, "label": DEVICE_TYPE_LABELS.get(t, t)} for t in DEVICE_TYPE_ORDER],
            "subroles": [{"value": r, "label": SUBROLE_LABELS.get(r, r)} for r in SUBROLE_ORDER],
        })

    async def _api_device_override(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "invalid json"}, status=400)
        device_id = body.get("device_id")
        if not device_id:
            return web.json_response({"error": "device_id required"}, status=400)
        ok = self._store.set_device_override(
            device_id, include=body.get("include"), device_type=body.get("device_type"),
        )
        if not ok:
            return web.json_response({"error": "unknown device_id"}, status=404)
        return web.json_response({"ok": True})
