"""Typed device adapters — the one place for the entity / unit / sign
conventions the control logic relies on.

A :class:`Device` wraps one wizard-configured *strategy device* (an item from
``store.strategy_devices()``) plus the live store, and exposes its data as
standard **channels** (``is_on``, ``cur_unit``, ``power_w``, ``soc``, ``ready``,
runtime, the config numbers, …). Controllers and the input builders read these
channels instead of parsing raw entity states and config dicts themselves, so
the conventions (which state strings mean "on", how a setpoint maps to watts,
defaults, …) live here and nowhere else.
"""

from __future__ import annotations

import math
from typing import Any, Optional

# Entity states that count as "on" for a switchable load.
ON_STATES = ("on", "heat", "true")

# Device capability categories (the OpenEMS "Nature" analog): controllers select
# devices by category/capability instead of by concrete kind strings.
ESS = "ess"                      # battery storage (SoC, charge + optional discharge)
EVCS = "evcs"                    # EV charging station
HEAT_PUMP = "heat_pump"
SETPOINT_LOAD = "setpoint_load"  # modulating numeric load (e.g. heating rod)
SWITCH_LOAD = "switch_load"      # on/off load
METER = "meter"                  # nothing controllable

_EVCS_KINDS = ("wallbox", "ev_charger", "evse", "ev", "car", "charger")
_HEATPUMP_KINDS = ("heat_pump", "heatpump")

# Capabilities ("Natures"): a device is described by the set of these it has, and
# controllers select devices by capability rather than by concrete kind — so a
# new device type (e.g. a V2G vehicle = STORAGE_DISCHARGE) is picked up by the
# existing controllers without new control logic.
CAP_METER = "meter"
CAP_SWITCH = "switch_load"          # on/off actuator
CAP_MODULATE = "modulating_load"    # power setpoint actuator
CAP_STAGED = "staged_load"          # N discrete stages (k relays)
CAP_CHARGE = "storage_charge"       # chargeable storage (charge actuator + SoC)
CAP_DISCHARGE = "storage_discharge" # dischargeable storage (battery / V2G)
CAP_SOC = "soc"                     # exposes a state of charge
CAP_CONNECTABLE = "connectable"     # plugged / ready signal
CAP_THERMAL = "thermal"             # temperature target


class Device:
    """Read-only typed view over one strategy device + the live store."""

    def __init__(self, store: Any, d: dict[str, Any]) -> None:
        self._store = store
        self._d = d
        self._cfg: dict[str, Any] = d.get("cfg", {}) or {}

    # --- identity -----------------------------------------------------------
    @property
    def key(self) -> str:
        return str(self._d.get("key", ""))

    @property
    def kind(self) -> str:
        return str(self._d.get("kind", ""))

    @property
    def name(self) -> str:
        return str(self._d.get("name", ""))

    @property
    def is_battery(self) -> bool:
        return self._d.get("kind") == "battery"

    # --- capability typing (FEMS "Natures") --------------------------------
    @property
    def category(self) -> str:
        """Capability class derived from kind + wiring (see the constants)."""
        if self.is_battery:
            return ESS
        k = self.kind.lower()
        if k in _EVCS_KINDS:
            return EVCS
        if k in _HEATPUMP_KINDS:
            return HEAT_PUMP
        if self.setpoint_entity:
            return SETPOINT_LOAD
        if self.switch_entity:
            return SWITCH_LOAD
        return METER

    @property
    def is_ess(self) -> bool:
        return self.is_battery

    @property
    def is_evcs(self) -> bool:
        return self.category == EVCS

    @property
    def can_modulate(self) -> bool:
        return bool(self.setpoint_entity)

    @property
    def can_switch(self) -> bool:
        return bool(self.switch_entity)

    @property
    def can_force_discharge(self) -> bool:
        return bool(self.discharge_entity)

    @property
    def has_soc(self) -> bool:
        return bool(self._d.get("soc"))

    @property
    def stages(self) -> list[str]:
        """Stage switch entities for a staged load (e.g. a 3-stage heating rod)."""
        st = self._cfg.get("stages")
        return [str(s) for s in st if s] if isinstance(st, list) else []

    def capabilities(self) -> set[str]:
        """The set of capabilities ("Natures") this device exposes, derived from
        its wiring. Controllers query these instead of the concrete ``kind``."""
        caps: set[str] = set()
        if self.switch_entity:
            caps.add(CAP_SWITCH)
        if self.stages:
            caps.add(CAP_STAGED)
        if self.setpoint_entity:
            caps.add(CAP_MODULATE)
            # A charge setpoint means "chargeable storage" for a battery or any
            # device that can also discharge (a V2G vehicle) — not for a plain
            # modulating load (heating rod), which only has CAP_MODULATE.
            if self.is_battery or self.discharge_entity:
                caps.add(CAP_CHARGE)
        if self.discharge_entity:           # battery discharge, or a V2G vehicle
            caps.add(CAP_DISCHARGE)
        if self._d.get("soc"):
            caps.add(CAP_SOC)
        if self.ready_entity:
            caps.add(CAP_CONNECTABLE)
        return caps

    def has_cap(self, cap: str) -> bool:
        return cap in self.capabilities()

    @property
    def priority(self) -> int:
        try:
            return int(self._cfg.get("priority", 5))
        except (TypeError, ValueError):
            return 5

    # --- control wiring -----------------------------------------------------
    @property
    def mode(self) -> str:
        return str(self._d.get("control_mode", ""))

    @property
    def switch_entity(self) -> str:
        return str(self._d.get("switch", "") or "")

    @property
    def setpoint_entity(self) -> str:
        return str(self._d.get("setpoint", "") or "")

    @property
    def discharge_entity(self) -> str:
        return str(self._d.get("discharge", "") or "")

    @property
    def ready_entity(self) -> str:
        return str(self._cfg.get("ready_entity", "") or "")

    # --- participation / flags ---------------------------------------------
    @property
    def self_consumption(self) -> bool:
        return bool(self._cfg.get("self_consumption"))

    @property
    def tariff_shift(self) -> bool:
        return bool(self._cfg.get("tariff_shift"))

    @property
    def interruptible(self) -> bool:
        return bool(self._cfg.get("interruptible", True))

    @property
    def satisfied(self) -> bool:
        return bool(self._d.get("satisfied"))

    # --- config numbers -----------------------------------------------------
    def _num(self, key: str, default: float) -> float:
        # Only a missing/blank value falls back to the default — a configured 0
        # stays 0 (e.g. min_w=0, grid_soc_min=0 are valid settings).
        v = self._cfg.get(key, default)
        if v is None or v == "":
            return float(default)
        try:
            return float(v)
        except (TypeError, ValueError):
            return float(default)

    @property
    def max_w(self) -> float:
        return self._num("max_w", 0.0)

    @property
    def min_w(self) -> float:
        return self._num("min_w", 0.0)

    @property
    def wpu(self) -> float:
        return self._num("w_per_unit", 1.0) or 1.0   # 0 would divide -> use 1

    @property
    def pv_threshold_w(self) -> float:
        return self._num("pv_threshold_w", 0.0)

    @property
    def grid_soc_min(self) -> float:
        return self._num("grid_soc_min", 0.0)

    @property
    def grid_soc_max(self) -> float:
        return self._num("grid_soc_max", 100.0)

    @property
    def capacity_kwh(self) -> float:
        """Usable storage capacity (kWh); 0 = unknown (optimizer degrades)."""
        return self._num("capacity_kwh", 0.0)

    @property
    def min_runtime_s(self) -> int:
        return int(self._cfg.get("min_runtime_min", 0) or 0) * 60

    @property
    def min_off_s(self) -> int:
        return int(self._cfg.get("min_off_min", 0) or 0) * 60

    @property
    def max_starts(self) -> int:
        return int(self._cfg.get("max_starts_per_day", 0) or 0)

    @property
    def latest_start(self) -> str:
        return str(self._cfg.get("latest_start", "") or "")

    @property
    def charge_from_grid(self) -> bool:
        """EVCS: charge whenever connected (allow grid), not surplus-only."""
        return bool(self._cfg.get("charge_from_grid"))

    # --- live channels ------------------------------------------------------
    def _state(self, entity: str) -> str:
        if not entity:
            return ""
        return str(self._store.live_state(entity).get("state", "")).lower()

    @property
    def is_on(self) -> bool:
        return self._state(self.switch_entity) in ON_STATES

    @property
    def power_w(self) -> float:
        try:
            return float(self._d.get("power_w") or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def cur_unit(self) -> float:
        try:
            return float(self._store.live_state(self.setpoint_entity).get("state"))
        except (TypeError, ValueError):
            return 0.0

    @property
    def soc(self) -> Optional[float]:
        s = self._d.get("soc")
        if not s:
            return None
        try:
            return float(self._store.live_state(s).get("state"))
        except (TypeError, ValueError):
            return None

    @property
    def ready(self) -> bool:
        """True only if a ready entity is configured *and* reads truthy."""
        return bool(self.ready_entity) and bool(self._store.entity_truthy(self.ready_entity))

    @property
    def runtime(self) -> dict[str, Any]:
        eid = self.switch_entity or self.setpoint_entity
        return self._store.runtime(eid) if eid else {}

    # --- actuator limits ----------------------------------------------------
    @property
    def max_unit(self) -> float:
        """Upper setpoint in the entity's own unit (max_w / w-per-unit)."""
        return round(self.max_w / self.wpu, 2) if self.max_w > 0 else 0.0

    def actuator_bounds(self) -> dict[str, tuple[float, float]]:
        """Hard ``[min, max]`` per controllable numeric entity, in its own unit.

        Fed to the controller chain's resolver so no controller (including a
        future optimizer) can drive a device past its limit. Only an upper bound
        is set when ``max_w`` is configured; otherwise it stays open (∞).
        """
        hi = self.max_w / self.wpu if self.max_w > 0 else math.inf
        return {ent: (0.0, hi) for ent in (self.setpoint_entity, self.discharge_entity) if ent}


def devices(store: Any) -> list[Device]:
    """All strategy devices as typed adapters."""
    return [Device(store, d) for d in store.strategy_devices()]


def select(store: Any, cap: str) -> list[Device]:
    """All devices exposing capability ``cap`` (the capability-driven query)."""
    return [d for d in devices(store) if d.has_cap(cap)]


def ess_devices(store: Any) -> list[Device]:
    """Battery / storage devices."""
    return [d for d in devices(store) if d.is_ess]


def modulating_loads(store: Any) -> list[Device]:
    """Modulating (setpoint) loads that are not the battery."""
    return [d for d in devices(store) if d.can_modulate and not d.is_ess]


def actuator_bounds(store: Any) -> dict[str, tuple[float, float]]:
    """Merged hard actuator limits across all devices (for the resolver)."""
    out: dict[str, tuple[float, float]] = {}
    for d in devices(store):
        out.update(d.actuator_bounds())
    return out
