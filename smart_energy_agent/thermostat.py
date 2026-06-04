"""Absence temperature setback with predictive reheat (concept 6.6).

Grouped by persons + their rooms: a group is "away" only when *all* its persons
are away; then its thermostats drop to the eco temperature. When anyone is home
(or during predictive pre-heating) the comfort temperature is restored.

Predictive reheat: if a group has a comfort target time, each thermostat starts
heating early enough — based on a learned reheat rate (minutes per Kelvin, EWMA
from observed heat-ups) — so comfort is reached at that time. Frost protection is
always respected; the feature is opt-in and does nothing while presence is
unknown.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

_LOGGER = logging.getLogger(__name__)

DEFAULT_REHEAT_K = 20.0   # minutes per Kelvin until a value is learned
TEMP_EPS = 0.3            # don't re-issue if already within this margin
EWMA_ALPHA = 0.3


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def minutes_to_time(now_tm: time.struct_time, hhmm: str) -> Optional[int]:
    """Minutes from now to the next occurrence of HH:MM (today or tomorrow)."""
    try:
        h, m = (int(x) for x in str(hhmm).split(":"))
    except (ValueError, AttributeError):
        return None
    delta = (h * 60 + m) - (now_tm.tm_hour * 60 + now_tm.tm_min)
    return delta + 24 * 60 if delta < 0 else delta


class ThermostatEngine:
    def __init__(self, store: Any, call_service: Callable[..., Awaitable[Any]]) -> None:
        self._store = store
        self._call = call_service
        self._last: dict[str, float] = {}            # entity -> last target we set
        self._heat: dict[str, tuple[float, float]] = {}  # entity -> (start_ts, start_temp)

    def target(self, th: dict[str, Any], present: bool, frost: float,
               current_temp: Optional[float], mins_to_comfort: Optional[int]) -> tuple[Optional[float], bool]:
        """Return (target_temp, is_preheating) for a thermostat."""
        comfort, eco = _to_float(th.get("comfort_c")), _to_float(th.get("eco_c"))
        if comfort is None or eco is None:
            return None, False
        if present:
            return max(comfort, frost), False
        tgt, preheat = eco, False
        if mins_to_comfort is not None and current_temp is not None:
            k = th.get("reheat_k") or 0.0
            k = k if k > 0 else DEFAULT_REHEAT_K
            lead = k * max(0.0, comfort - current_temp)
            if mins_to_comfort <= lead:
                tgt, preheat = comfort, True
        return max(tgt, frost), preheat

    def _learn(self, group: dict[str, Any], th: dict[str, Any], climate: str,
               heating: bool, cur: Optional[float], now_ts: float) -> None:
        comfort = _to_float(th.get("comfort_c"))
        if comfort is None or cur is None:
            if not heating:
                self._heat.pop(climate, None)
            return
        if heating and cur < comfort - 0.5:
            self._heat.setdefault(climate, (now_ts, cur))
        elif climate in self._heat:
            start_ts, start_t = self._heat.pop(climate)
            dt_min, d_t = (now_ts - start_ts) / 60.0, comfort - start_t
            if dt_min >= 2 and d_t >= 0.5:
                k = dt_min / d_t
                old = th.get("reheat_k") or 0.0
                new = k if old <= 0 else (EWMA_ALPHA * k + (1 - EWMA_ALPHA) * old)
                self._store.set_thermostat_reheat(group.get("id"), th.get("id"), new)
        if not heating:
            self._heat.pop(climate, None)

    async def run_once(self) -> None:
        sb = self._store.setback()
        if not sb.get("enabled"):
            return
        frost = _to_float(sb.get("frost_c")) or 7.0
        now_ts = time.time()
        now_tm = time.localtime(now_ts)
        for g in self._store.groups():
            present = self._store.group_present(g)
            if present is None:
                continue
            mtc = minutes_to_time(now_tm, g.get("comfort_time")) if g.get("comfort_time") else None
            for th in g.get("thermostats", []):
                climate = th.get("climate")
                if not climate:
                    continue
                attrs = self._store.live_state(climate).get("attributes") or {}
                cur = _to_float(attrs.get("current_temperature"))
                tgt, preheat = self.target(th, present, frost, cur, mtc)
                if tgt is None:
                    continue
                self._learn(g, th, climate, bool(present or preheat), cur, now_ts)
                set_now = _to_float(attrs.get("temperature"))
                already = set_now is not None and abs(set_now - tgt) < TEMP_EPS
                last = self._last.get(climate)
                if already or (last is not None and abs(last - tgt) < 0.05 and set_now is None):
                    self._last[climate] = tgt
                    continue
                try:
                    await self._call("climate", "set_temperature", climate, {"temperature": tgt})
                    self._last[climate] = tgt
                    _LOGGER.info("Setback: %s -> %.1f °C (%s%s)", climate, tgt,
                                 "anwesend" if present else "abwesend",
                                 ", Vorheizen" if preheat else "")
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Setback set_temperature failed for %s: %s", climate, err)
