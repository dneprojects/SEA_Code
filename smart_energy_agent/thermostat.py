"""Absence temperature setback for room thermostats (concept 6.6).

Pragmatic first version for fast-reacting radiator thermostats: when the
household is away, set each configured thermostat to its eco temperature; when
someone is home, restore the comfort temperature. Frost protection is always
respected; the feature is opt-in (master switch) and does nothing while
presence is unknown. Predictive/learned reheat lead time is a later extension.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

# Don't re-issue a setpoint if the thermostat is already within this margin.
TEMP_EPS = 0.3


class ThermostatEngine:
    def __init__(self, store: Any, call_service: Callable[..., Awaitable[Any]]) -> None:
        self._store = store
        self._call = call_service
        self._last: dict[str, float] = {}  # entity_id -> last target we set

    def decide(self, thermostat: dict[str, Any], present: bool, frost_c: float) -> Any:
        """Target temperature for a thermostat given presence, or None."""
        target = thermostat.get("comfort_c") if present else thermostat.get("eco_c")
        if target is None:
            return None
        try:
            return max(float(target), float(frost_c))
        except (TypeError, ValueError):
            return None

    async def run_once(self) -> None:
        sb = self._store.setback()
        if not sb.get("enabled"):
            return
        present = self._store.presence_is_home()
        if present is None:
            return  # no presence info -> stay safe, do nothing
        frost = sb.get("frost_c", 7.0)
        for th in self._store.thermostats():
            climate = th.get("climate")
            if not climate:
                continue
            target = self.decide(th, present, frost)
            if target is None:
                continue
            attrs = (self._store.live_state(climate).get("attributes") or {})
            try:
                cur = float(attrs.get("temperature"))
            except (TypeError, ValueError):
                cur = None
            already = cur is not None and abs(cur - target) < TEMP_EPS
            last = self._last.get(climate)
            if already or (last is not None and abs(last - target) < 0.05 and cur is None):
                self._last[climate] = target
                continue
            try:
                await self._call("climate", "set_temperature", climate, {"temperature": target})
                self._last[climate] = target
                _LOGGER.info("Setback: %s -> %.1f °C (%s)", climate, target,
                             "anwesend" if present else "abwesend")
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Setback set_temperature failed for %s: %s", climate, err)
