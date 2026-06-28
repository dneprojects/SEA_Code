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

from typing import Any, Optional

# Entity states that count as "on" for a switchable load.
ON_STATES = ("on", "heat", "true")


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
        try:
            return float(self._cfg.get(key, default) or default)
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
        return self._num("w_per_unit", 1.0)

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


def devices(store: Any) -> list[Device]:
    """All strategy devices as typed adapters."""
    return [Device(store, d) for d in store.strategy_devices()]
