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
from datetime import datetime
from typing import Any, Callable, Optional

import aiosqlite

from . import const, forecast, setup_catalog, strategies, suggest, tariff
from .aggregator import compute_balance, balance_from_config, _state_power_w
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
        # Bring forward any config from the old /data volume before loading.
        migrated = const.migrate_legacy_data_if_needed()
        if migrated:
            _LOGGER.info(
                "Migrated legacy config to %s: %s",
                os.path.dirname(const.get_history_db()) or ".",
                ", ".join(migrated),
            )
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
            # PV-surplus priority when a battery is present:
            #   False (default) = battery first  -> loads only get the export overflow
            #   True             = loads first   -> loads get PV directly, battery the rest
            # Either way a discharging battery is never counted as surplus, so
            # controllable loads are never powered from the battery.
            "surplus_loads_first": False,
            # Charge-priority floor (%): in "loads first" mode the battery keeps
            # its charging power until the SoC reaches this reserve before loads
            # may divert it. 0 = off. No effect in "battery first" mode.
            "surplus_battery_min_soc": 0.0,

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
                "charge_max_ct": 0.0,      # grid-charge storage only at price <= this (0 = negative/free)
                "discharge_min_ct": 0.0,   # forced battery discharge at price >= this (0 = disabled)
            },
            # Absence temperature setback (concept 6.6), grouped by persons+rooms.
            # groups: [{id, name, persons:[eid], comfort_time:"HH:MM",
            #           thermostats:[{id,name,climate,comfort_c,eco_c,reheat_k}]}]
            # Away (setback) only when ALL persons of a group are away.
            "setback": {
                "enabled": False,          # master switch (safety: off)
                "frost_c": 7.0,            # never set below this
                "groups": [],
            },
            # Per controllable device (from the wizard) strategy participation,
            # keyed by "kind:instanceId" -> {self_consumption, tariff_shift,
            # priority, pv_threshold_w, min_runtime_min, min_off_min, max_starts_per_day}
            "strategy_loads": {},
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
        # --- wizard setup configuration (category -> slot -> entity) ---------
        self._config: dict[str, Any] = {}
        self._load_config()
        # Live raw-state cache for entities referenced by the config.
        self._live_by_id: dict[str, dict[str, Any]] = {}
        # Last HA snapshot (states + registries) for the suggestion engine.
        self._ha_snapshot: dict[str, Any] = {}
        # Last HA energy-dashboard preferences (energy/get_prefs).
        self._energy_prefs: dict[str, Any] = {}

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
        for key in ("grid_invert", "battery_invert", "control_enabled",
                    "surplus_loads_first"):
            if key in patch and patch[key] is not None:
                self._settings[key] = bool(patch[key])
        if "retention_days" in patch:
            rd = patch["retention_days"]
            try:
                self._settings["retention_days"] = int(rd) if rd not in (None, "") else None
            except (TypeError, ValueError):
                pass
        if "surplus_battery_min_soc" in patch:
            ms = patch["surplus_battery_min_soc"]
            try:
                self._settings["surplus_battery_min_soc"] = (
                    max(0.0, min(100.0, float(ms))) if ms not in (None, "") else 0.0)
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
            for f in ("price_ct", "ht_price_ct", "nt_price_ct", "feed_in_ct",
                      "charge_max_ct", "discharge_min_ct"):
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

    def surplus_loads_first(self) -> bool:
        """True = controllable loads have priority over battery charging on PV
        surplus; False (default) = battery first."""
        return bool(self._settings.get("surplus_loads_first", False))

    def surplus_battery_min_soc(self) -> float:
        """SoC reserve (%) the battery charges to before 'loads first' may divert
        its charge power. 0 = off."""
        try:
            return float(self._settings.get("surplus_battery_min_soc", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # --- tariff --------------------------------------------------------------
    def tariff(self) -> dict[str, Any]:
        return dict(self._settings.get("tariff", {}))

    def has_energy_prefs(self) -> bool:
        return bool(self._energy_prefs.get("energy_sources"))

    def tariff_prefill(self) -> dict[str, Any]:
        """Tariff defaults from the HA Energy dashboard (grid source prices).

        The dashboard stores prices per kWh in the configured currency
        (e.g. 0.30); convert to ct/kWh (×100).
        """
        out: dict[str, Any] = {}

        def ct(value: Any) -> Optional[float]:
            try:
                return round(float(value) * 100.0, 2)
            except (TypeError, ValueError):
                return None

        for s in (self._energy_prefs.get("energy_sources") or []):
            if not isinstance(s, dict) or s.get("type") != "grid":
                continue
            if s.get("entity_energy_price"):
                out.setdefault("price_entity", s["entity_energy_price"])
            if s.get("number_energy_price") is not None and ct(s["number_energy_price"]) is not None:
                out.setdefault("price_ct", ct(s["number_energy_price"]))
            if s.get("number_energy_price_export") is not None and ct(s["number_energy_price_export"]) is not None:
                out.setdefault("feed_in_ct", ct(s["number_energy_price_export"]))
            for f0 in (s.get("flow_from") or []):
                if isinstance(f0, dict):
                    if f0.get("entity_energy_price"):
                        out.setdefault("price_entity", f0["entity_energy_price"])
                    if f0.get("number_energy_price") is not None and ct(f0["number_energy_price"]) is not None:
                        out.setdefault("price_ct", ct(f0["number_energy_price"]))
            for t0 in (s.get("flow_to") or []):
                if isinstance(t0, dict) and t0.get("number_energy_price") is not None and ct(t0["number_energy_price"]) is not None:
                    out.setdefault("feed_in_ct", ct(t0["number_energy_price"]))
        return out

    def feed_in_ct(self) -> Optional[float]:
        return self._settings.get("tariff", {}).get("feed_in_ct")

    # --- absence setback, grouped by persons + rooms (concept 6.6) ----------
    DEFAULT_REHEAT_K = 20.0  # minutes per Kelvin until a learned value exists

    def setback(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._settings.get("setback", {})))

    def groups(self) -> list[dict[str, Any]]:
        return self._settings.get("setback", {}).get("groups", [])

    @staticmethod
    def _sanitize_setback(raw: Any, gen_id: Callable[[str, set[str]], str]) -> dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        out: dict[str, Any] = {"enabled": bool(raw.get("enabled"))}
        try:
            out["frost_c"] = float(raw.get("frost_c", 7.0))
        except (TypeError, ValueError):
            out["frost_c"] = 7.0
        used_g: set[str] = set()
        groups = []
        for g in raw.get("groups") or []:
            if not isinstance(g, dict):
                continue
            gid = str(g.get("id") or "") or gen_id("g", used_g)
            if gid in used_g:
                gid = gen_id("g", used_g)
            used_g.add(gid)
            persons = [str(p) for p in (g.get("persons") or []) if p]
            used_t: set[str] = set()
            ths = []
            for t in g.get("thermostats") or []:
                if not isinstance(t, dict):
                    continue
                tid = str(t.get("id") or "") or gen_id("t", used_t)
                if tid in used_t:
                    tid = gen_id("t", used_t)
                used_t.add(tid)
                def num(key, dflt):
                    try:
                        return float(t.get(key, dflt))
                    except (TypeError, ValueError):
                        return dflt
                ths.append({"id": tid, "name": str(t.get("name") or "Thermostat"),
                            "climate": str(t.get("climate") or ""),
                            "comfort_c": num("comfort_c", 21.0), "eco_c": num("eco_c", 17.0),
                            "reheat_k": num("reheat_k", 0.0)})
            groups.append({"id": gid, "name": str(g.get("name") or "Gruppe"),
                           "persons": persons, "comfort_time": str(g.get("comfort_time") or ""),
                           "thermostats": ths})
        out["groups"] = groups
        return out

    def set_setback_full(self, raw: Any) -> dict[str, Any]:
        self._settings["setback"] = self._sanitize_setback(raw, self._gen_id)
        self._save_settings()
        self._seed_live_from_snapshot()
        return self.setback()

    def set_thermostat_reheat(self, group_id: str, th_id: str, k: float) -> None:
        for g in self.groups():
            if g.get("id") == group_id:
                for t in g.get("thermostats", []):
                    if t.get("id") == th_id:
                        t["reheat_k"] = round(float(k), 1)
                        self._save_settings()
                        return

    def live_state(self, entity_id: str) -> dict[str, Any]:
        """Latest cached raw state of a watched entity (live or snapshot)."""
        return self._live_by_id.get(entity_id) or \
            (self._ha_snapshot.get("state_by_id") or {}).get(entity_id) or {}

    def entity_truthy(self, entity_id: str) -> bool:
        """Whether an entity's state reads as 'on/active/connected/ready'."""
        if not entity_id:
            return False
        s = str(self.live_state(entity_id).get("state", "")).strip().lower()
        return s in ("on", "true", "yes", "1", "home", "present", "connected",
                     "plugged", "plugged_in", "charging", "ready", "heat",
                     "open", "active")

    def tariff_cheap_now(self) -> dict[str, Any]:
        """Universal 'is now a cheap tariff period?' from the registered price source."""
        t = self.tariff()
        pe = t.get("price_entity") or ""
        state = self.live_state(pe) if pe else {}
        return tariff.cheap_now(t, state, datetime.now())

    def presence_is_home(self, entity_id: str) -> Optional[bool]:
        """True/False for one presence entity, None if unknown/unavailable."""
        if not entity_id:
            return None
        s = str(self.live_state(entity_id).get("state", "")).lower()
        if s in ("home", "on", "true", "present", "anwesend"):
            return True
        if s in ("not_home", "away", "off", "false", "abwesend"):
            return False
        return None

    def strategies_overview(self) -> list[dict[str, Any]]:
        """Which energy-saving strategies are possible from the current config."""
        ov = strategies.overview(self._config, self._settings, self.groups())
        info = self.tariff_cheap_now()
        for s in ov:
            if s["key"] == "tariff_shift":
                s["cheap"] = bool(info.get("cheap"))
                s["status"] = info.get("reason")
        return ov

    # --- controllable devices (from the wizard) participating in strategies --
    def controllable_devices(self) -> list[dict[str, Any]]:
        """Configured load instances that have a control actuator (switch/setpoint)."""
        out: list[dict[str, Any]] = []
        for kind in ("heat_pump", "water_heater", "ev_charger", "consumers"):
            spec = setup_catalog.find_kind(kind)
            klabel = spec["label"] if spec else kind
            for inst in self._config.get(kind) or []:
                if not isinstance(inst, dict):
                    continue
                c = inst.get("control") or {}
                mode = "switch" if c.get("switch") else ("setpoint" if c.get("setpoint") else "")
                if not mode:
                    continue
                pw, have = 0.0, False
                for p in inst.get("powers") or []:
                    v = _state_power_w(self.live_state(p.get("entity"))) if p.get("entity") else None
                    if v is not None:
                        pw += v
                        have = True
                out.append({
                    "key": f"{kind}:{inst.get('id')}", "kind": kind, "kind_label": klabel,
                    "name": inst.get("name", klabel), "control_mode": mode,
                    "switch": c.get("switch", ""), "setpoint": c.get("setpoint", ""),
                    "power_w": round(pw, 1) if have else None,
                })
        # Battery with a controllable charge-power setpoint = a modulating load.
        blabel = (setup_catalog.find_kind("battery") or {}).get("label", "Batterie")
        for inst in self._config.get("battery") or []:
            if not isinstance(inst, dict) or not inst.get("charge_power"):
                continue
            out.append({
                "key": f"battery:{inst.get('id')}", "kind": "battery", "kind_label": blabel,
                "name": inst.get("name", blabel), "control_mode": "setpoint",
                "switch": "", "setpoint": inst.get("charge_power", ""),
                "discharge": inst.get("discharge_power", ""),  # forced-discharge actuator (arbitrage)
                "soc": inst.get("soc", ""),   # SoC entity = the natural stop signal
                "power_w": _state_power_w(self.live_state(inst.get("power"))) if inst.get("power") else None,
            })
        return out

    def strategy_loads(self) -> dict[str, Any]:
        return dict(self._settings.get("strategy_loads", {}))

    def set_strategy_load(self, key: str, patch: dict[str, Any]) -> bool:
        if not key:
            return False
        cfg = self._settings.setdefault("strategy_loads", {}).setdefault(key, {})
        for f in ("self_consumption", "tariff_shift"):
            if f in patch:
                cfg[f] = bool(patch[f])
        for f in ("priority", "pv_threshold_w", "min_runtime_min", "min_off_min", "max_starts_per_day"):
            if f in patch:
                try:
                    cfg[f] = int(patch[f] or 0)
                except (TypeError, ValueError):
                    pass
        for f in ("min_w", "max_w", "w_per_unit", "limit_max",
                  "grid_soc_min", "grid_soc_max"):  # modulating / stop / grid-charge
            if f in patch:
                try:
                    cfg[f] = float(patch[f] or 0)
                except (TypeError, ValueError):
                    pass
        for f in ("limit_entity", "ready_entity", "latest_start"):
            if f in patch:
                cfg[f] = str(patch[f] or "")
        self._save_settings()
        return True

    def _device_satisfied(self, cfg: dict[str, Any]) -> bool:
        """True if a device reached its stop limit (e.g. vehicle SoC / temperature).

        A threshold of 0 (or below) means the stop condition is disabled.
        """
        le = cfg.get("limit_entity")
        try:
            limit = float(cfg.get("limit_max"))
        except (TypeError, ValueError):
            return False
        if not le or limit <= 0:
            return False
        try:
            return float(self.live_state(le).get("state")) >= limit
        except (TypeError, ValueError):
            return False

    def strategy_devices(self) -> list[dict[str, Any]]:
        """Controllable devices merged with their strategy participation config."""
        sl = self.strategy_loads()
        out = []
        for d in self.controllable_devices():
            cfg = {"self_consumption": False, "tariff_shift": False, "priority": 5,
                   "pv_threshold_w": 0, "min_runtime_min": 0, "min_off_min": 0,
                   "max_starts_per_day": 0, "min_w": 0, "max_w": 0, "w_per_unit": 1,
                   "limit_entity": "", "limit_max": 0, "ready_entity": "", "latest_start": "",
                   "grid_soc_min": 0, "grid_soc_max": 100}
            cfg.update(sl.get(d["key"], {}))
            # The battery's stop signal is always its SoC — fill it in automatically
            # so the UI only asks for the threshold (active once limit_max > 0).
            if d.get("kind") == "battery" and not cfg.get("limit_entity") and d.get("soc"):
                cfg["limit_entity"] = d["soc"]
            out.append({**d, "cfg": cfg, "satisfied": self._device_satisfied(cfg)})
        return out

    def history_entities(self) -> list[dict[str, Any]]:
        """Configured devices with their plottable entities (for the history view)."""
        cfg = self._config

        def unit(eid: str) -> Optional[str]:
            return (self.live_state(eid).get("attributes", {}) or {}).get("unit_of_measurement")

        out: list[dict[str, Any]] = []

        def add(key: str, name: str, triples: list[tuple[str, Any, str]]) -> None:
            ents = [{"entity_id": e, "label": lbl, "unit": unit(e), "cls": cls}
                    for lbl, e, cls in triples if e]
            if ents:
                out.append({"key": key, "name": name, "entities": ents})

        g = cfg.get("grid") or {}
        add("grid", "Netz", [("Netzleistung", g.get("power"), "power"),
                             ("Bezug", g.get("import_power"), "power"),
                             ("Einspeisung", g.get("export_power"), "power")])
        for kind in ("pv", "battery", "heat_pump", "water_heater", "ev_charger", "consumers"):
            spec = setup_catalog.find_kind(kind)
            klabel = spec["label"] if spec else kind
            for inst in cfg.get(kind) or []:
                if not isinstance(inst, dict):
                    continue
                t: list[tuple[str, Any, str]] = []
                if inst.get("power"):   # battery: single power entity (+/- charge)
                    t.append(("Leistung", inst["power"], "power"))
                for p in (inst.get("powers") or []):
                    if p.get("entity"):
                        t.append((p.get("name") or "Leistung", p["entity"], "power"))
                for e in (inst.get("energies") or []) + (inst.get("energy") or []):
                    if isinstance(e, dict) and e.get("entity"):
                        t.append((e.get("name") or "Energie", e["entity"], "energy"))
                if inst.get("soc"):
                    t.append(("SoC", inst["soc"], "soc"))
                if inst.get("charge_power"):
                    t.append(("Ladesollwert", inst["charge_power"], "setpoint"))
                if inst.get("discharge_power"):
                    t.append(("Entladesollwert", inst["discharge_power"], "setpoint"))
                for c in (inst.get("circuits") or []):
                    nm = c.get("name") or "Kreis"
                    if c.get("temp"):
                        t.append((nm + " Temp", c["temp"], "temp"))
                    if c.get("setpoint"):
                        t.append((nm + " Soll", c["setpoint"], "setpoint"))
                add(f"{kind}:{inst.get('id')}", inst.get("name") or klabel, t)
        return out

    def group_present(self, group: dict[str, Any]) -> Optional[bool]:
        """Group presence: present if ANY person home; away only if ALL away;
        None (unknown) if no persons or any state unknown and none home."""
        persons = group.get("persons") or []
        if not persons:
            return None
        states = [self.presence_is_home(p) for p in persons]
        if any(s is True for s in states):
            return True
        if all(s is False for s in states):
            return False
        return None

    @staticmethod
    def _price_to_ct(state: dict[str, Any]) -> Optional[float]:
        """Normalise a price entity's value to ct/kWh using its unit.

        Dynamic-tariff integrations report in various units; convert currency
        per kWh/MWh to ct/kWh. Unit-less or already-ct values pass through.
        """
        try:
            val = float((state or {}).get("state"))
        except (TypeError, ValueError):
            return None
        unit = str(((state or {}).get("attributes") or {})
                   .get("unit_of_measurement") or "").lower()
        if "/mwh" in unit:
            val /= 10.0                       # currency/MWh -> ct/kWh (EUR-like)
        elif any(u in unit for u in ("eur/kwh", "€/kwh", "$/kwh", "usd/kwh",
                                     "gbp/kwh", "£/kwh", "chf/kwh")):
            val *= 100.0                      # currency/kWh -> ct/kWh
        # ct/kWh, cent/kWh, öre/kWh or unit-less -> already ct-scale
        return round(val, 3)

    def current_price_ct(self) -> Optional[float]:
        """Current purchase price in ct/kWh based on the configured tariff."""
        t = self._settings.get("tariff", {})
        mode = t.get("mode", "static")
        if mode == "dynamic":
            pe = t.get("price_entity") or ""
            # Prefer the live/snapshot value so the price shows immediately after
            # setup (before the next state_changed); fall back to the cached one.
            val = self._price_to_ct(self.live_state(pe)) if pe else None
            return val if val is not None else self._dynamic_price
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
            val = self._price_to_ct(new_state)
            if val is not None:
                self._dynamic_price = val
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
        # Header figures follow the same source as the live balance: the wizard
        # config when set, otherwise the legacy role-based aggregation.
        if self.has_config_balance():
            b = self.balance()
            pv_w, grid_w, battery_w = b.get("pv_w"), b.get("grid_w"), b.get("battery_w")
        else:
            pv_w = round(_sum(EnergyRole.PV), 1)
            grid_w = round(_sum(EnergyRole.GRID), 1)
            battery_w = round(_sum(EnergyRole.BATTERY), 1)
        return {
            "total": len(self._entities),
            "included": included,
            "pv_w": pv_w,
            "grid_w": grid_w,
            "battery_w": battery_w,
            "current_price_ct": self.current_price_ct(),
            "feed_in_ct": self.feed_in_ct(),
            "last_discovery_ts": self.last_discovery_ts,
        }

    def balance(self) -> dict[str, Any]:
        """Current live energy balance (PV/grid/battery/house/surplus).

        Uses the explicit wizard configuration once PV or grid power is set;
        otherwise falls back to the legacy role-based aggregation.
        """
        if self.has_config_balance():
            return balance_from_config(
                self._config,
                self._live_by_id,
                grid_invert=self._settings.get("grid_invert", False),
            )
        return compute_balance(
            list(self._entities.values()),
            grid_invert=self._settings.get("grid_invert", False),
            battery_invert=self._settings.get("battery_invert", False),
        )

    # --- wizard setup configuration (instance-based) ------------------------
    def _load_config(self) -> None:
        path = const.get_energy_config_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                self._config = self._sanitize_config(self._migrate_config(loaded))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not load energy config: %s", err)
            self._config = {}

    def _save_config(self) -> None:
        path = const.get_energy_config_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._config, fh, indent=2)
            os.replace(tmp, path)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Could not save energy config: %s", err)

    def config(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._config))

    # --- sanitiser (validate a full config posted by the UI) ----------------
    @staticmethod
    def _gen_id(prefix: str, used: set[str]) -> str:
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        cid = f"{prefix}{i}"
        used.add(cid)
        return cid

    def _san_named_list(self, value: Any, used: set[str], item_label: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(value, list):
            return out
        for it in value:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id") or "")
            if not iid or iid in used:
                iid = self._gen_id("i", used)
            used.add(iid)
            out.append({"id": iid, "name": str(it.get("name") or item_label),
                        "entity": str(it.get("entity") or "")})
        return out

    def _san_circuits(self, value: Any, used: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not isinstance(value, list):
            return out
        for it in value:
            if not isinstance(it, dict):
                continue
            iid = str(it.get("id") or "")
            if not iid or iid in used:
                iid = self._gen_id("hk", used)
            used.add(iid)
            out.append({"id": iid, "name": str(it.get("name") or "Heizkreis"),
                        "temp": str(it.get("temp") or ""), "setpoint": str(it.get("setpoint") or "")})
        return out

    @staticmethod
    def _san_control(value: Any) -> dict[str, Any]:
        v = value if isinstance(value, dict) else {}
        mode = v.get("mode")
        if mode not in ("switch", "setpoint"):
            mode = ""
        return {"mode": mode, "switch": str(v.get("switch") or ""),
                "setpoint": str(v.get("setpoint") or "")}

    def _san_field(self, field: dict[str, Any], value: Any, used: set[str]) -> Any:
        kind = field["kind"]
        if kind == "entity":
            return str(value) if value else ""
        if kind == "flag":
            return bool(value)
        if kind in ("named_power", "named_energy"):
            return self._san_named_list(value, used, field.get("item_label", "Eintrag"))
        if kind == "circuits":
            return self._san_circuits(value, used)
        return ""

    def _sanitize_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        out: dict[str, Any] = {}
        # grid (single fixed section)
        g = raw.get("grid") if isinstance(raw.get("grid"), dict) else {}
        grid: dict[str, Any] = {}
        for f in setup_catalog.GRID["fields"]:
            v = g.get(f["key"])
            grid[f["key"]] = bool(v) if f["kind"] == "flag" else (str(v) if v else "")
        out["grid"] = grid
        used: set[str] = set()
        for kind in setup_catalog.INSTANCE_KINDS:
            insts: list[dict[str, Any]] = []
            for raw_inst in raw.get(kind["key"]) or []:
                if not isinstance(raw_inst, dict):
                    continue
                cid = str(raw_inst.get("id") or "")
                if not cid or cid in used:
                    cid = self._gen_id(kind["key"], used)
                used.add(cid)
                inst: dict[str, Any] = {"id": cid, "name": str(raw_inst.get("name") or kind["label"])}
                for f in kind["fields"]:
                    inst[f["key"]] = self._san_field(f, raw_inst.get(f["key"]), used)
                if kind.get("control"):
                    inst["control"] = self._san_control(raw_inst.get("control"))
                insts.append(inst)
            out[kind["key"]] = insts
        return out

    def _migrate_config(self, cfg: Any) -> dict[str, Any]:
        """Convert the old flat beta config to the instance schema (best effort)."""
        if not isinstance(cfg, dict):
            return {}

        def to_named(eids: Any, label: str) -> list[dict[str, Any]]:
            if isinstance(eids, str):
                eids = [eids] if eids else []
            return [{"id": "", "name": label, "entity": e} for e in (eids or []) if e]

        new = dict(cfg)
        pv = cfg.get("pv")
        if isinstance(pv, dict):
            inst = {"id": "", "name": "PV-Anlage", "powers": to_named(pv.get("power"), "PV"),
                    "energy": to_named(pv.get("energy_today"), "Ertrag")}
            new["pv"] = [inst] if (inst["powers"] or inst["energy"]) else []
        battery = cfg.get("battery")
        if isinstance(battery, dict):
            b = {"id": "", "name": "Batterie", "power": battery.get("power", ""),
                 "soc": battery.get("soc", ""), "invert": bool(battery.get("invert"))}
            new["battery"] = [b] if (b["power"] or b["soc"]) else []
        for kind in ("heat_pump", "water_heater", "ev_charger"):
            v = cfg.get(kind)
            if isinstance(v, dict):
                ek = setup_catalog.find_kind(kind)
                label = ek["label"] if ek else kind
                ekey = "energies" if kind in ("heat_pump", "water_heater") else "energy"
                inst = {"id": "", "name": label, "powers": to_named(v.get("power"), "Leistung"),
                        ekey: to_named(v.get("energy"), "Energie")}
                new[kind] = [inst] if inst["powers"] else []
        cons = cfg.get("consumers")
        if isinstance(cons, list):
            migrated = []
            for c in cons:
                if not isinstance(c, dict):
                    continue
                if "powers" in c:
                    migrated.append(c)
                else:
                    migrated.append({"id": c.get("id", ""), "name": c.get("name", "Verbraucher"),
                                     "powers": to_named(c.get("power"), "Leistung"),
                                     "energy": to_named(c.get("energy"), "Energie")})
            new["consumers"] = migrated
        return new

    def set_full_config(self, raw: Any) -> dict[str, Any]:
        """Validate + persist a complete config document posted by the wizard."""
        self._config = self._sanitize_config(raw if isinstance(raw, dict) else {})
        self._save_config()
        self._seed_live_from_snapshot()
        return self.config()

    def _prefill(self) -> dict[str, Any]:
        """Energy-dashboard pre-fill with device-based power/SoC derivation.

        The Energy dashboard usually configures *energy* entities. For PV, grid
        and battery we derive the matching *power* (and battery SoC) entity from
        the same HA device as that energy/SoC entity, so the power slots are
        pre-selected too.
        """
        pf = suggest.prefill_from_prefs(self._energy_prefs)
        states = self._ha_snapshot.get("states") or []
        ent = self._ha_snapshot.get("entity_registry") or []
        if not states:
            return pf
        srcs = [s for s in (self._energy_prefs.get("energy_sources") or []) if isinstance(s, dict)]

        def derive(ref: Optional[str], unit_group: str, hint_kind: str) -> Optional[str]:
            return suggest.derive_on_device(states, ent, ref, unit_group,
                                            setup_catalog.kind_hints(hint_kind))

        # PV: derive a power for each solar source that only exposes energy.
        if pf.get("pv"):
            inst = pf["pv"][0]
            have = {p.get("entity") for p in inst.get("powers", [])}
            for s in (x for x in srcs if x.get("type") == "solar"):
                if s.get("stat_rate"):
                    continue
                p = derive(s.get("stat_energy_from"), "power", "pv")
                if p and p not in have:
                    inst.setdefault("powers", []).append({"id": "", "name": "PV", "entity": p})
                    have.add(p)

        # Grid: derive power from the energy entity's device if not configured.
        if not pf["grid"].get("power"):
            grid_src = next((x for x in srcs if x.get("type") == "grid"), {})
            ref = grid_src.get("stat_energy_from") or grid_src.get("stat_energy_to")
            if not ref:
                ff = grid_src.get("flow_from")
                if isinstance(ff, list) and ff:
                    ref = ff[0].get("stat_energy_from")
            p = derive(ref, "power", "grid")
            if p:
                pf["grid"]["power"] = p

        # Battery: derive power + SoC from the energy/SoC device.
        b_sources = [x for x in srcs if x.get("type") == "battery"]
        for i, inst in enumerate(pf.get("battery") or []):
            src = b_sources[i] if i < len(b_sources) else {}
            ref = inst.get("soc") or src.get("stat_energy_from") or src.get("stat_energy_to") or inst.get("power")
            if not inst.get("power"):
                p = derive(ref, "power", "battery")
                if p:
                    inst["power"] = p
            if not inst.get("soc"):
                soc = derive(ref or inst.get("power"), "soc", "battery")
                if soc:
                    inst["soc"] = soc
        return pf

    def import_prefs(self) -> dict[str, Any]:
        """Create instances from the HA energy-dashboard preferences (empty kinds)."""
        prefill = self._prefill()
        cfg = self._config
        if prefill.get("grid", {}).get("power") and not (cfg.get("grid") or {}).get("power"):
            cfg.setdefault("grid", {})["power"] = prefill["grid"]["power"]
        for kind in setup_catalog.KIND_KEYS:
            insts = prefill.get(kind) or []
            if insts and not cfg.get(kind):
                cfg[kind] = insts
        price = (prefill.get("tariff") or {}).get("price_entity")
        if price:
            t = self._settings.setdefault("tariff", {})
            if not t.get("price_entity"):
                t["price_entity"] = price
                self._save_settings()
        self._config = self._sanitize_config(cfg)
        self._save_config()
        self._seed_live_from_snapshot()
        return self.config()

    def config_entity_ids(self) -> set[str]:
        """All entity ids referenced anywhere in the config (any string with a dot)."""
        ids: set[str] = set()

        def walk(o: Any) -> None:
            if isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str) and "." in o:
                ids.add(o)

        walk(self._config)
        return ids

    def watched_entity_ids(self) -> set[str]:
        """Config entities plus setback persons, thermostat climates, the tariff
        price entity and the PV-forecast entity (so their live values stay fresh)."""
        ids = self.config_entity_ids()
        pe = (self._settings.get("tariff", {}) or {}).get("price_entity")
        if pe:
            ids.add(pe)
        fe = self._settings.get("pv_forecast_entity")
        if fe:
            ids.add(fe)
        for g in self.groups():
            if not isinstance(g, dict):
                continue
            ids.update(p for p in (g.get("persons") or []) if p)
            for t in g.get("thermostats") or []:
                if isinstance(t, dict) and t.get("climate"):
                    ids.add(t["climate"])
        return ids

    def observe_config_state(self, entity_id: str, new_state: dict[str, Any]) -> None:
        """Cache the live state of a watched entity (config / thermostat / presence)."""
        if new_state and entity_id in self.watched_entity_ids():
            self._live_by_id[entity_id] = new_state

    def set_ha_snapshot(
        self,
        states: list[dict[str, Any]],
        entity_registry: list[dict[str, Any]],
        device_registry: list[dict[str, Any]],
        area_registry: list[dict[str, Any]],
    ) -> None:
        self._ha_snapshot = {
            "states": states,
            "entity_registry": entity_registry,
            "device_registry": device_registry,
            "area_registry": area_registry,
            "state_by_id": {s.get("entity_id"): s for s in states if s.get("entity_id")},
        }
        self._seed_live_from_snapshot()

    def set_energy_prefs(self, prefs: dict[str, Any]) -> None:
        self._energy_prefs = prefs if isinstance(prefs, dict) else {}

    def _seed_live_from_snapshot(self) -> None:
        """Seed the live cache for configured entities from the last snapshot,
        so the balance works immediately without waiting for state_changed."""
        state_by_id = self._ha_snapshot.get("state_by_id") or {}
        for eid in self.watched_entity_ids():
            if eid not in self._live_by_id and eid in state_by_id:
                self._live_by_id[eid] = state_by_id[eid]

    def has_config_balance(self) -> bool:
        """True once any PV power or grid power is configured (balance source)."""
        if any(inst.get("powers") for inst in (self._config.get("pv") or [])
               if isinstance(inst, dict)):
            return True
        grid = self._config.get("grid") or {}
        return bool(grid.get("power") or grid.get("import_power") or grid.get("export_power"))

    def catalog_payload(self) -> dict[str, Any]:
        """Schema + current config + energy-dashboard pre-fill for the wizard."""
        return {
            "grid": setup_catalog.GRID,
            "kinds": setup_catalog.INSTANCE_KINDS,
            "circuit_fields": setup_catalog.CIRCUIT_FIELDS,
            "config": self.config(),
            "prefill": self._prefill(),
            "has_prefs": bool(self._energy_prefs.get("energy_sources")),
        }

    def suggestions(
        self, unit_group: str, kind: str = "", query: str = "", current: str = "",
    ) -> list[dict[str, Any]]:
        if unit_group not in setup_catalog.UNIT_GROUPS or not self._ha_snapshot:
            return []
        return suggest.rank_for_slot(
            self._ha_snapshot.get("states", []),
            self._ha_snapshot.get("entity_registry", []),
            self._ha_snapshot.get("device_registry", []),
            self._ha_snapshot.get("area_registry", []),
            slot={"unit_group": unit_group},
            category_hints=setup_catalog.kind_hints(kind) if kind else [],
            prefs_entities=suggest.prefs_entity_set(self._energy_prefs),
            query=query,
            current=current,
        )

    def _entity_items(
        self, fields: list[dict[str, Any]], inst: dict[str, Any],
        *, control: bool = False, invert: bool = False,
    ) -> list[dict[str, Any]]:
        state_by_id = self._ha_snapshot.get("state_by_id") or {}

        def item(label: str, eid: str, is_power: bool = False, inv: bool = False) -> None:
            if not eid:
                return
            st = self._live_by_id.get(eid) or state_by_id.get(eid) or {}
            attrs = st.get("attributes", {}) or {}
            power_w = None
            if is_power:
                power_w = _state_power_w(st)
                if power_w is not None and inv:
                    power_w = -power_w
            items.append({
                "slot_label": label, "entity_id": eid,
                "name": attrs.get("friendly_name", eid), "state": st.get("state"),
                "unit": attrs.get("unit_of_measurement"),
                "power_w": round(power_w, 1) if power_w is not None else None,
            })

        items: list[dict[str, Any]] = []
        for f in fields:
            kind, value = f["kind"], inst.get(f["key"])
            is_power = f.get("unit_group") == "power"
            if kind == "entity":
                item(f["label"], str(value or ""), is_power, invert and f["key"] == "power")
            elif kind in ("named_power", "named_energy"):
                for it in value or []:
                    if isinstance(it, dict):
                        item(it.get("name", f["label"]), str(it.get("entity") or ""), is_power)
            elif kind == "circuits":
                for it in value or []:
                    if isinstance(it, dict):
                        nm = it.get("name", "Heizkreis")
                        item(nm + " Temp.", str(it.get("temp") or ""))
                        item(nm + " Soll", str(it.get("setpoint") or ""))
        if control:
            c = inst.get("control") or {}
            if c.get("mode") == "switch":
                item("Schalten", str(c.get("switch") or ""))
            elif c.get("mode") == "setpoint":
                item("Sollwert", str(c.get("setpoint") or ""))
        return items

    def categories_with_entities(self) -> list[dict[str, Any]]:
        """Config grouped by logical genus for the device view: each assigned
        entity with its label and live value (regardless of HA device)."""
        groups: list[dict[str, Any]] = []
        g = self._config.get("grid") or {}
        gitems = self._entity_items(setup_catalog.GRID["fields"], g, invert=bool(g.get("invert")))
        groups.append({"key": "grid", "label": setup_catalog.GRID["label"],
                       "count": len(gitems), "entities": gitems})
        for kind in setup_catalog.INSTANCE_KINDS:
            for inst in self._config.get(kind["key"]) or []:
                if not isinstance(inst, dict):
                    continue
                items = self._entity_items(
                    kind["fields"], inst, control=bool(kind.get("control")),
                    invert=bool(inst.get("invert")),
                )
                groups.append({
                    "key": kind["key"] + ":" + str(inst.get("id")),
                    "label": inst.get("name", kind["label"]) + " (" + kind["label"] + ")",
                    "count": len(items), "entities": items,
                })
        return groups

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

    async def history(
        self, range_s: Optional[int] = None, points: int = 240,
        start: Optional[int] = None, end: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Return downsampled energy-state series for a time window.

        Either pass ``range_s`` (last N seconds up to now) or an explicit
        ``start``/``end`` epoch window (for free selection / panning). Buckets
        rows into ~`points` slots and averages each metric.
        """
        if self._db is None:
            return []
        now = int(time.time())
        if start is None or end is None:
            rs = range_s or 86400
            start, end = now - rs, now
        bucket = max(1, (end - start) // max(1, points))
        try:
            cur = await self._db.execute(
                "SELECT (ts/?)*? AS b, "
                "AVG(pv_w), AVG(grid_w), AVG(battery_w), "
                "AVG(house_load_w), AVG(surplus_w), AVG(battery_soc) "
                "FROM energy_state WHERE ts >= ? AND ts <= ? GROUP BY b ORDER BY b",
                (bucket, bucket, start, end),
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
