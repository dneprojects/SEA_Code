"""In-memory entity registry plus a SQLite history store.

Keeps the classified entities in memory (live updated from state_changed
events), applies user curation overrides, and records the aggregated energy
balance to SQLite (input for the later history-based forecast). The DB path is
configurable so the history can be moved to an SSD/share.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import aiosqlite

from . import const, forecast
from .aggregator import compute_balance
from .models import (
    EnergyEntity, EnergyRole, ROLE_LABELS, ROLE_ORDER,
    CONSUMER_ROLES, DEFAULT_CONSUMER, CONSUMER_FIELD_TYPES,
    Device, DeviceType, DEVICE_TYPE_LABELS, DEVICE_TYPE_ORDER,
    DEFAULT_STRATEGY, STRATEGY_VALUES,
)

_LOGGER = logging.getLogger(__name__)


class Store:
    """Holds the current set of classified energy entities + user overrides."""

    def __init__(self) -> None:
        self._entities: dict[str, EnergyEntity] = {}
        self._db: Optional[aiosqlite.Connection] = None
        self.last_discovery_ts: float = 0.0
        # entity_id -> {"include": bool, "role": str}; persisted to disk.
        self._overrides: dict[str, dict[str, Any]] = {}
        self._load_overrides()
        # Runtime settings (sign conventions, retention). Persisted to disk.
        self._settings: dict[str, Any] = {
            "grid_invert": False,
            "battery_invert": False,
            "retention_days": None,   # None -> use env/default
            "control_enabled": False,  # master switch for PV-surplus control (safety: off)
            "strategy": DEFAULT_STRATEGY,  # optimization strategy (selection only for now)
            "pv_forecast_entity": "",  # HA entity providing a PV power/energy forecast

            # Electricity tariff: purchase price + feed-in compensation.
            "tariff": {
                "mode": "static",          # static | ht_nt | dynamic
                "price_ct": 30.0,          # static purchase price (ct/kWh)
                "ht_price_ct": 35.0,       # peak price
                "nt_price_ct": 25.0,       # off-peak price
                "nt_start": "22:00",       # off-peak window start
                "nt_end": "06:00",         # off-peak window end
                "price_entity": "",        # HA sensor (ct/kWh) for dynamic mode
                "feed_in_ct": 8.0,         # feed-in compensation (ct/kWh)
            },
        }
        self._load_settings()
        # Last seen value of the dynamic price entity (ct/kWh), if used.
        self._dynamic_price: Optional[float] = None
        # Last seen full state of the PV-forecast entity (with attributes).
        self._pv_forecast_state: Optional[dict[str, Any]] = None
        # Latest HA energy/solar_forecast result (Forecast.Solar etc.).
        self._solar_forecast: Optional[dict[str, Any]] = None
        # Per-consumer control configuration, keyed by control entity_id.
        self._consumers: dict[str, dict[str, Any]] = {}
        self._load_consumers()
        # In-memory control runtime state, keyed by control entity_id.
        self._runtime: dict[str, dict[str, Any]] = {}
        # Devices (primary discovery unit) + curation overrides + entity index.
        self._devices: dict[str, Device] = {}
        self._device_entity_index: dict[str, tuple[str, int]] = {}
        self._device_overrides: dict[str, dict[str, Any]] = {}
        self._load_device_overrides()

    # --- user curation / overrides ------------------------------------------
    def _load_overrides(self) -> None:
        path = const.get_overrides_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    self._overrides = json.load(fh)
                _LOGGER.info("Loaded %d overrides from %s", len(self._overrides), path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not load overrides from %s: %s", path, err)
            self._overrides = {}

    def _save_overrides(self) -> None:
        path = const.get_overrides_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._overrides, fh, indent=2)
            os.replace(tmp, path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not save overrides to %s: %s", path, err)

    def _apply_override(self, entity: EnergyEntity) -> None:
        ov = self._overrides.get(entity.entity_id)
        if not ov:
            return
        if "role" in ov and ov["role"]:
            entity.role = ov["role"]
        if "include" in ov and ov["include"] is not None:
            entity.include = bool(ov["include"])
        entity.overridden = True

    def set_override(
        self,
        entity_id: str,
        include: Optional[bool] = None,
        role: Optional[str] = None,
    ) -> bool:
        """Persist a user override and apply it to the live entity."""
        entity = self._entities.get(entity_id)
        if entity is None:
            return False
        ov = self._overrides.setdefault(entity_id, {})
        if include is not None:
            ov["include"] = bool(include)
        if role is not None:
            ov["role"] = role
        self._apply_override(entity)
        self._save_overrides()
        return True

    # --- runtime settings ----------------------------------------------------
    def _load_settings(self) -> None:
        path = const.get_settings_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    self._settings.update(json.load(fh))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not load settings from %s: %s", path, err)

    def _save_settings(self) -> None:
        path = const.get_settings_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._settings, fh, indent=2)
            os.replace(tmp, path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not save settings to %s: %s", path, err)

    def get_settings(self) -> dict[str, Any]:
        return dict(self._settings)

    def set_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        for key in ("grid_invert", "battery_invert", "control_enabled"):
            if key in patch and patch[key] is not None:
                self._settings[key] = bool(patch[key])
        if "retention_days" in patch:
            rd = patch["retention_days"]
            try:
                self._settings["retention_days"] = int(rd) if rd not in (None, "") else None
            except (TypeError, ValueError):
                pass
        if patch.get("strategy") in STRATEGY_VALUES:
            self._settings["strategy"] = patch["strategy"]
        if "pv_forecast_entity" in patch:
            self._settings["pv_forecast_entity"] = str(patch["pv_forecast_entity"] or "")
        if isinstance(patch.get("tariff"), dict):
            t = self._settings.setdefault("tariff", {})
            tp = patch["tariff"]
            if tp.get("mode") in ("static", "ht_nt", "dynamic"):
                t["mode"] = tp["mode"]
            for f in ("price_ct", "ht_price_ct", "nt_price_ct", "feed_in_ct"):
                if f in tp:
                    try:
                        t[f] = float(tp[f]) if tp[f] not in (None, "") else 0.0
                    except (TypeError, ValueError):
                        pass
            for f in ("nt_start", "nt_end", "price_entity"):
                if f in tp:
                    t[f] = str(tp[f])
        self._save_settings()
        return self.get_settings()

    def retention_days(self) -> int:
        rd = self._settings.get("retention_days")
        return rd if isinstance(rd, int) and rd > 0 else const.get_history_days()

    def control_enabled(self) -> bool:
        return bool(self._settings.get("control_enabled", False))

    # --- tariff --------------------------------------------------------------
    def tariff(self) -> dict[str, Any]:
        return dict(self._settings.get("tariff", {}))

    def feed_in_ct(self) -> Optional[float]:
        return self._settings.get("tariff", {}).get("feed_in_ct")

    def current_price_ct(self) -> Optional[float]:
        """Current purchase price in ct/kWh based on the configured tariff."""
        t = self._settings.get("tariff", {})
        mode = t.get("mode", "static")
        if mode == "dynamic":
            return self._dynamic_price
        if mode == "ht_nt":
            lt = time.localtime()
            cur = lt.tm_hour * 60 + lt.tm_min

            def mins(s: str) -> int:
                try:
                    h, m = str(s).split(":")
                    return int(h) * 60 + int(m)
                except (ValueError, AttributeError):
                    return 0

            start, end = mins(t.get("nt_start", "22:00")), mins(t.get("nt_end", "06:00"))
            in_nt = (start <= cur < end) if start < end else (cur >= start or cur < end)
            return t.get("nt_price_ct") if in_nt else t.get("ht_price_ct")
        return t.get("price_ct")

    def observe_external(self, entity_id: str, new_state: dict[str, Any]) -> None:
        """Capture external (non-classified) entities: dynamic price + PV forecast."""
        if not new_state:
            return
        t = self._settings.get("tariff", {})
        if t.get("mode") == "dynamic" and entity_id == t.get("price_entity"):
            try:
                self._dynamic_price = float(new_state.get("state"))
            except (TypeError, ValueError):
                pass
        if entity_id and entity_id == self._settings.get("pv_forecast_entity"):
            self._pv_forecast_state = new_state

    def pv_forecast_entity(self) -> str:
        return self._settings.get("pv_forecast_entity", "") or ""

    def set_solar_forecast(self, data: Optional[dict[str, Any]]) -> None:
        """Store the latest HA energy/solar_forecast result (e.g. Forecast.Solar)."""
        self._solar_forecast = data if isinstance(data, dict) else None

    # --- control runtime state ----------------------------------------------
    def _runtime_for(self, entity_id: str) -> dict[str, Any]:
        rt = self._runtime.get(entity_id)
        if rt is None:
            rt = {"last_on": 0.0, "last_off": 0.0, "starts": 0, "day": 0, "reason": ""}
            self._runtime[entity_id] = rt
        return rt

    def runtime(self, entity_id: str) -> dict[str, Any]:
        return dict(self._runtime_for(entity_id))

    def note_switch(self, entity_id: str, on: bool, reason: str) -> None:
        """Record that the engine switched a consumer on/off (for guards/UI)."""
        now = time.time()
        day = int(now // 86400)
        rt = self._runtime_for(entity_id)
        if rt["day"] != day:
            rt["day"] = day
            rt["starts"] = 0
        if on:
            rt["last_on"] = now
            rt["starts"] += 1
        else:
            rt["last_off"] = now
        rt["reason"] = reason

    # --- devices -------------------------------------------------------------
    def _load_device_overrides(self) -> None:
        path = const.get_device_overrides_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    self._device_overrides = json.load(fh)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not load device overrides: %s", err)
            self._device_overrides = {}

    def _save_device_overrides(self) -> None:
        path = const.get_device_overrides_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._device_overrides, fh, indent=2)
            os.replace(tmp, path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not save device overrides: %s", err)

    def _apply_device_override(self, dev: Device) -> None:
        ov = self._device_overrides.get(dev.device_id)
        if not ov:
            return
        if ov.get("device_type"):
            dev.device_type = ov["device_type"]
        if ov.get("include") is not None:
            dev.include = bool(ov["include"])
        dev.overridden = True

    def set_devices(self, devices: list[Device]) -> None:
        self._devices = {d.device_id: d for d in devices}
        self._device_entity_index = {}
        for d in self._devices.values():
            self._apply_device_override(d)
            for i, e in enumerate(d.entities):
                self._device_entity_index[e["entity_id"]] = (d.device_id, i)

    def set_device_override(
        self, device_id: str, include: Optional[bool] = None,
        device_type: Optional[str] = None,
    ) -> bool:
        dev = self._devices.get(device_id)
        if dev is None:
            return False
        ov = self._device_overrides.setdefault(device_id, {})
        if include is not None:
            ov["include"] = bool(include)
        if device_type is not None:
            ov["device_type"] = device_type
        self._apply_device_override(dev)
        self._save_device_overrides()
        return True

    def update_device_state(self, entity_id: str, new_state: dict[str, Any]) -> None:
        """Live-update a device's entity value from a state_changed event."""
        loc = self._device_entity_index.get(entity_id)
        if not loc or new_state is None:
            return
        did, idx = loc
        dev = self._devices.get(did)
        if not dev or idx >= len(dev.entities):
            return
        e = dev.entities[idx]
        e["state"] = new_state.get("state")
        unit = (new_state.get("attributes", {}) or {}).get("unit_of_measurement") or e.get("unit")
        if unit in ("W", "kW", "MW"):
            try:
                val = float(new_state.get("state"))
                factor = 1000.0 if unit == "kW" else (1_000_000.0 if unit == "MW" else 1.0)
                e["power_w"] = val * factor
            except (TypeError, ValueError):
                pass

    def devices(self) -> list[Device]:
        return list(self._devices.values())

    def grouped_devices(self, include_only: bool = False) -> list[dict[str, Any]]:
        by: dict[str, list[Device]] = {}
        for d in self._devices.values():
            if include_only and not d.include:
                continue
            by.setdefault(d.device_type, []).append(d)
        groups: list[dict[str, Any]] = []
        for t in DEVICE_TYPE_ORDER:
            items = sorted(by.get(t, []), key=lambda x: (x.name or "").lower())
            if items:
                groups.append({
                    "type": t, "label": DEVICE_TYPE_LABELS.get(t, t),
                    "count": len(items), "devices": [d.to_dict() for d in items],
                })
        return groups

    def device_summary(self) -> dict[str, Any]:
        return {
            "total": len(self._devices),
            "included": sum(1 for d in self._devices.values() if d.include),
        }

    # --- managed consumers (control configuration) ---------------------------
    def _load_consumers(self) -> None:
        path = const.get_consumers_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    self._consumers = json.load(fh)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not load consumers from %s: %s", path, err)
            self._consumers = {}

    def _save_consumers(self) -> None:
        path = const.get_consumers_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._consumers, fh, indent=2)
            os.replace(tmp, path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not save consumers to %s: %s", path, err)

    def _consumer_config(self, entity_id: str) -> dict[str, Any]:
        cfg = dict(DEFAULT_CONSUMER)
        cfg.update(self._consumers.get(entity_id, {}))
        return cfg

    def list_consumers(self) -> list[dict[str, Any]]:
        """Candidate controllable entities (included) + their config."""
        out: list[dict[str, Any]] = []
        for e in self._entities.values():
            if e.role in CONSUMER_ROLES and e.include:
                cfg = self._consumer_config(e.entity_id)
                rt = self._runtime.get(e.entity_id, {})
                out.append({
                    "entity_id": e.entity_id,
                    "name": e.friendly_name,
                    "role": e.role,
                    "area": e.area,
                    "power_w": e.power_w,
                    "linked_power_entity": e.linked_power_entity,
                    "configured": e.entity_id in self._consumers,
                    "config": cfg,
                    "is_on": e.state == "on",
                    "controllable": e.domain in const.CONTROLLABLE_DOMAINS,
                    "auto": cfg.get("control_mode") == "auto",
                    "reason": rt.get("reason", ""),
                })
        out.sort(key=lambda c: c["name"].lower())
        return out

    def set_consumer(self, entity_id: str, patch: dict[str, Any]) -> bool:
        """Validate + persist one consumer's control configuration."""
        if entity_id not in self._entities:
            return False
        cfg = self._consumers.setdefault(entity_id, {})
        for key, value in patch.items():
            if key not in DEFAULT_CONSUMER:
                continue
            caster = CONSUMER_FIELD_TYPES.get(key)
            if caster is bool:
                cfg[key] = bool(value)
            elif caster in (int, float):
                try:
                    cfg[key] = caster(value) if value not in (None, "") else caster(0)
                except (TypeError, ValueError):
                    pass
            else:
                cfg[key] = value
        self._save_consumers()
        return True

    # --- entity registry -----------------------------------------------------
    def set_entities(self, entities: list[EnergyEntity]) -> None:
        self._entities = {e.entity_id: e for e in entities}
        for entity in self._entities.values():
            self._apply_override(entity)
        self.last_discovery_ts = time.time()

    def update_state(self, entity_id: str, new_state: dict[str, Any]) -> bool:
        """Update live state/power for a tracked entity. Returns True if tracked."""
        entity = self._entities.get(entity_id)
        if entity is None or new_state is None:
            return False
        entity.state = new_state.get("state")
        attrs = new_state.get("attributes", {}) or {}
        unit = attrs.get("unit_of_measurement") or entity.unit
        # Update the live power value for any entity measured/expressed in W/kW.
        if unit in ("W", "kW", "MW"):
            try:
                val = float(new_state.get("state"))
                factor = 1000.0 if unit == "kW" else (1_000_000.0 if unit == "MW" else 1.0)
                entity.power_w = val * factor
            except (TypeError, ValueError):
                pass
        return True

    def entities(self) -> list[EnergyEntity]:
        return list(self._entities.values())

    def grouped(self, include_only: bool = False) -> list[dict[str, Any]]:
        """Entities grouped by role in display order, for the UI.

        include_only=True returns only entities the user/auto-default includes
        (the "active energy model"); False returns everything for curation.
        """
        groups: list[dict[str, Any]] = []
        by_role: dict[str, list[EnergyEntity]] = {}
        for e in self._entities.values():
            if include_only and not e.include:
                continue
            by_role.setdefault(e.role, []).append(e)
        for role in ROLE_ORDER:
            items = sorted(by_role.get(role, []), key=lambda x: x.friendly_name.lower())
            if items:
                groups.append(
                    {
                        "role": role,
                        "label": ROLE_LABELS.get(role, role),
                        "count": len(items),
                        "entities": [e.to_dict() for e in items],
                    }
                )
        return groups

    def summary(self) -> dict[str, Any]:
        """Quick aggregate numbers for the dashboard header (included only)."""
        def _sum(role: str) -> float:
            return sum(
                e.power_w or 0.0
                for e in self._entities.values()
                if e.role == role and e.include
            )

        included = sum(1 for e in self._entities.values() if e.include)
        return {
            "total": len(self._entities),
            "included": included,
            "pv_w": round(_sum(EnergyRole.PV), 1),
            "grid_w": round(_sum(EnergyRole.GRID), 1),
            "battery_w": round(_sum(EnergyRole.BATTERY), 1),
            "current_price_ct": self.current_price_ct(),
            "feed_in_ct": self.feed_in_ct(),
            "last_discovery_ts": self.last_discovery_ts,
        }

    def balance(self) -> dict[str, Any]:
        """Current live energy balance (PV/grid/battery/house/surplus)."""
        return compute_balance(
            list(self._entities.values()),
            grid_invert=self._settings.get("grid_invert", False),
            battery_invert=self._settings.get("battery_invert", False),
        )

    # --- SQLite history ------------------------------------------------------
    async def open_db(self) -> None:
        path = const.get_history_db()
        try:
            self._db = await aiosqlite.connect(path)
            # Aggregated energy balance snapshots (input for later forecast).
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS energy_state (
                    ts           INTEGER NOT NULL,
                    pv_w         REAL,
                    grid_w       REAL,
                    battery_w    REAL,
                    battery_soc  REAL,
                    house_load_w REAL,
                    surplus_w    REAL,
                    price_ct     REAL
                )
                """
            )
            # Add price_ct to pre-existing databases that lack it.
            try:
                await self._db.execute("ALTER TABLE energy_state ADD COLUMN price_ct REAL")
            except Exception:  # noqa: BLE001 - column already exists
                pass
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_energy_state_ts ON energy_state(ts)"
            )
            await self._db.commit()
            _LOGGER.info("History DB ready at %s", path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not open history DB at %s: %s", path, err)
            self._db = None

    async def record_state(self, balance: dict[str, Any], ts: Optional[int] = None) -> None:
        """Append one aggregated energy-balance snapshot to the history."""
        if self._db is None:
            return
        ts = ts if ts is not None else int(time.time())
        try:
            await self._db.execute(
                "INSERT INTO energy_state "
                "(ts, pv_w, grid_w, battery_w, battery_soc, house_load_w, surplus_w, price_ct) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    balance.get("pv_w"),
                    balance.get("grid_w"),
                    balance.get("battery_w"),
                    balance.get("battery_soc"),
                    balance.get("house_load_w"),
                    balance.get("surplus_w"),
                    self.current_price_ct(),
                ),
            )
            await self._db.commit()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not record energy state: %s", err)

    async def purge_old(self, days: Optional[int] = None) -> int:
        """Delete history older than the retention window. Returns rows deleted."""
        if self._db is None:
            return 0
        days = days if days is not None else self.retention_days()
        cutoff = int(time.time()) - days * 86400
        try:
            cur = await self._db.execute(
                "DELETE FROM energy_state WHERE ts < ?", (cutoff,)
            )
            await self._db.commit()
            return cur.rowcount or 0
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not purge history: %s", err)
            return 0

    async def history_count(self) -> int:
        if self._db is None:
            return 0
        try:
            cur = await self._db.execute("SELECT COUNT(*) FROM energy_state")
            row = await cur.fetchone()
            return int(row[0]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    async def history(self, range_s: int, points: int = 240) -> list[dict[str, Any]]:
        """Return downsampled energy-state series for the last `range_s` seconds.

        Buckets rows into ~`points` time slots and averages each metric, so the
        chart stays light regardless of how much raw history exists.
        """
        if self._db is None:
            return []
        bucket = max(1, range_s // max(1, points))
        cutoff = int(time.time()) - range_s
        try:
            cur = await self._db.execute(
                "SELECT (ts/?)*? AS b, "
                "AVG(pv_w), AVG(grid_w), AVG(battery_w), "
                "AVG(house_load_w), AVG(surplus_w), AVG(battery_soc) "
                "FROM energy_state WHERE ts >= ? GROUP BY b ORDER BY b",
                (bucket, bucket, cutoff),
            )
            rows = await cur.fetchall()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read history: %s", err)
            return []

        def r1(v: Any) -> Any:
            return round(v, 1) if isinstance(v, (int, float)) else None

        return [
            {
                "ts": int(b),
                "pv_w": r1(pv), "grid_w": r1(grid), "battery_w": r1(batt),
                "house_load_w": r1(house), "surplus_w": r1(sur), "battery_soc": r1(soc),
            }
            for (b, pv, grid, batt, house, sur, soc) in rows
        ]

    async def savings(
        self, range_s: int, baseline: str = "full_feed", sink_cap_w: float = 3000.0
    ) -> dict[str, Any]:
        """Estimate savings of the actual operation vs a selectable baseline.

        Net cost of a scenario = Σ(import·price − export·feed).
        Baselines (computed from recorded PV/house power):
          * full_feed   – all PV exported, all consumption from grid (worst case)
          * direct      – instantaneous self-consumption, no battery/control
          * surplus_sink – like direct, but PV surplus up to sink_cap_w is used
            instead of exported (e.g. heating rod / EV), valued as avoided
            purchase: lowers cost by absorbed·(price − feed)
        savings = baseline_cost − actual_cost.
        """
        empty = {
            "pv_kwh": 0.0, "house_kwh": 0.0, "import_kwh": 0.0, "export_kwh": 0.0,
            "self_kwh": 0.0, "sink_kwh": 0.0, "baseline_eur": 0.0, "actual_eur": 0.0,
            "savings_eur": 0.0, "baseline": baseline, "samples": 0,
        }
        if self._db is None:
            return empty
        cutoff = int(time.time()) - range_s
        try:
            cur = await self._db.execute(
                "SELECT ts, pv_w, grid_w, house_load_w, price_ct "
                "FROM energy_state WHERE ts >= ? ORDER BY ts", (cutoff,)
            )
            rows = await cur.fetchall()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not compute savings: %s", err)
            return empty
        if len(rows) < 2:
            return empty

        feed = (self.feed_in_ct() or 0.0) / 100.0
        cur_price = (self.current_price_ct() or 0.0) / 100.0
        cap_kw = max(0.0, sink_cap_w) / 1000.0
        max_dt = 2 * const.RECORD_INTERVAL
        pv_k = house_k = imp_k = exp_k = sink_k = 0.0
        base_eur = act_eur = 0.0
        prev = None
        for r in rows:
            if prev is not None:
                dt = r[0] - prev[0]
                if dt <= 0:
                    prev = r
                    continue
                dt = min(dt, max_dt)
                h = dt / 3600.0
                pv = (prev[1] or 0.0) / 1000.0 * h
                house = (prev[3] or 0.0) / 1000.0 * h
                g = prev[2] or 0.0
                imp = max(g, 0.0) / 1000.0 * h
                exp = max(-g, 0.0) / 1000.0 * h
                price = (prev[4] / 100.0) if prev[4] is not None else cur_price
                pv_k += pv; house_k += house; imp_k += imp; exp_k += exp
                act_eur += imp * price - exp * feed
                # baseline cost for this interval
                if baseline == "full_feed":
                    base_eur += house * price - pv * feed
                else:
                    self_d = min(pv, house)
                    imp_d = house - self_d
                    exp_d = pv - self_d
                    bc = imp_d * price - exp_d * feed
                    if baseline == "surplus_sink":
                        absorbed = min(exp_d, cap_kw * h)
                        bc -= absorbed * (price - feed)
                        sink_k += absorbed
                    base_eur += bc
            prev = r

        return {
            "pv_kwh": round(pv_k, 2),
            "house_kwh": round(house_k, 2),
            "import_kwh": round(imp_k, 2),
            "export_kwh": round(exp_k, 2),
            "self_kwh": round(pv_k - exp_k, 2),
            "sink_kwh": round(sink_k, 2),
            "baseline_eur": round(base_eur, 2),
            "actual_eur": round(act_eur, 2),
            "savings_eur": round(base_eur - act_eur, 2),
            "baseline": baseline,
            "samples": len(rows),
        }

    async def consumption_forecast(
        self, hours: int = 24, history_days: int = forecast.DEFAULT_HISTORY_DAYS
    ) -> dict[str, Any]:
        """History-based household consumption forecast for the next `hours`.

        Reads the recorded ``house_load_w`` series and projects it via the
        recency-weighted hour-of-day profile (forecast module). Includes a
        backtest of the profile's accuracy on the most recent day.
        """
        empty = {
            "horizon_h": hours, "start_ts": 0, "points": [], "kwh": 0.0,
            "samples": 0, "span_days": 0.0, "coverage": 0.0,
            "accuracy": {"samples": 0, "mae_w": None, "mape_pct": None},
        }
        if self._db is None:
            return empty
        cutoff = int(time.time()) - history_days * 86400
        try:
            cur = await self._db.execute(
                "SELECT ts, house_load_w FROM energy_state WHERE ts >= ? ORDER BY ts",
                (cutoff,),
            )
            rows = await cur.fetchall()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not read history for forecast: %s", err)
            return empty
        series = [(int(r[0]), r[1]) for r in rows]
        out = forecast.forecast_consumption(series, hours=hours)
        out["accuracy"] = forecast.profile_backtest(series)
        return out

    async def forecast_bundle(self, hours: int = 24) -> dict[str, Any]:
        """Consumption forecast + PV forecast + the resulting PV-surplus forecast.

        PV source priority: the HA energy/solar_forecast (Forecast.Solar, needs
        no entity config) first, then a user-configured PV-forecast entity
        (Solcast / generic) as a fallback.
        """
        consumption = await self.consumption_forecast(hours=hours)
        pv_points = forecast.parse_solar_forecast(self._solar_forecast)
        pv_source = "forecast_solar" if pv_points else "none"
        if not pv_points:
            pv_points = forecast.parse_pv_forecast(self._pv_forecast_state)
            if pv_points:
                pv_source = "entity"
        surplus = forecast.build_surplus_forecast(consumption, pv_points)
        return {
            "consumption": consumption,
            "surplus": surplus,
            "pv_source": pv_source,
            "pv_entity": self.pv_forecast_entity(),
            "pv_points": len(pv_points),
        }

    async def close_db(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None
