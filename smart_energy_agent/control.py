"""PV-surplus control engine (phase 3).

Conservative, rule-based scheduler. Per cycle it issues AT MOST ONE switch
action to avoid oscillation:

  * If surplus exceeds the on-margin, turn ON the highest-priority eligible
    auto-consumer that fits into the available surplus.
  * If the household is importing beyond the off-margin, turn OFF the
    lowest-priority running auto-consumer whose minimum runtime has elapsed.

Guards: minimum off-time before restart, minimum runtime before switch-off,
max starts per day. Only entities in control_mode "auto" on switchable domains
are ever touched, and only when the master switch (control_enabled) is on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from . import const

_LOGGER = logging.getLogger(__name__)


@dataclass
class ConsumerDecision:
    entity_id: str
    domain: str
    priority: int
    nominal_power_w: float
    pv_threshold_w: float
    is_on: bool
    last_on: float
    last_off: float
    starts_today: int
    max_starts: int
    min_runtime_s: int
    min_off_s: int


def decide_action(
    now: float, surplus_w: float, consumers: list[ConsumerDecision]
) -> Optional[tuple[str, str, str]]:
    """Return (entity_id, "on"|"off", reason) for one action, or None."""
    on_margin = const.CONTROL_ON_MARGIN_W
    off_margin = const.CONTROL_OFF_MARGIN_W

    if surplus_w > on_margin:
        cands = [
            c for c in consumers
            if not c.is_on
            and (now - c.last_off) >= c.min_off_s
            and (c.max_starts == 0 or c.starts_today < c.max_starts)
            and surplus_w >= max(c.pv_threshold_w, c.nominal_power_w, on_margin)
        ]
        if cands:
            # Highest priority first; among equal, the one that fits tightest.
            cands.sort(key=lambda c: (-c.priority, c.nominal_power_w))
            c = cands[0]
            return (c.entity_id, "on",
                    f"PV-Überschuss {round(surplus_w)} W ≥ Bedarf")

    if surplus_w < -off_margin:
        cands = [
            c for c in consumers
            if c.is_on and (now - c.last_on) >= c.min_runtime_s
        ]
        if cands:
            # Lowest priority first; among equal, shed the largest load.
            cands.sort(key=lambda c: (c.priority, -c.nominal_power_w))
            c = cands[0]
            return (c.entity_id, "off",
                    f"Netzbezug {round(-surplus_w)} W, schalte ab")

    return None


def decide_modulation(surplus_signed: float, mods: list[dict]) -> list[dict]:
    """Distribute the signed surplus (+export / −import, W) across modulating
    loads by priority and return the new setpoint per load.

    Export raises high-priority loads first; import sheds low-priority first.
    Each load is clamped to its [min_w, max_w]; the remaining surplus is passed
    on to the next load. Setpoints are returned in the entity's own unit
    (watts / w_per_unit).
    """
    remaining = surplus_signed
    order = sorted(mods, key=lambda m: -m["priority"]) if remaining >= 0 \
        else sorted(mods, key=lambda m: m["priority"])
    out = []
    for m in order:
        wpu = m["wpu"] or 1.0
        target_w = max(m["min_w"], min(m["max_w"], m["cur_w"] + remaining))
        remaining -= (target_w - m["cur_w"])
        out.append({"entity": m["entity"], "domain": m["domain"],
                    "unit": round(target_w / wpu, 2), "cur_unit": m["cur_unit"],
                    "power_w": round(target_w, 1)})
    return out


class ControlEngine:
    """Builds decision input from the store and executes one action per cycle."""

    def __init__(self, store, call_service: Callable[[str, str, str], Awaitable]):
        self._store = store
        self._call_service = call_service

    def _build(self) -> list[ConsumerDecision]:
        """Build decisions from the wizard-configured devices that opted into
        PV-surplus self-consumption and are switchable."""
        out: list[ConsumerDecision] = []
        for d in self._store.strategy_devices():
            cfg = d["cfg"]
            sw = d.get("switch")
            if d.get("control_mode") != "switch" or not sw or not cfg.get("self_consumption"):
                continue
            rt = self._store.runtime(sw)
            state = str(self._store.live_state(sw).get("state", "")).lower()
            out.append(ConsumerDecision(
                entity_id=sw,
                domain=sw.split(".", 1)[0],
                priority=int(cfg.get("priority", 5)),
                nominal_power_w=float(d.get("power_w") or 0),
                pv_threshold_w=float(cfg.get("pv_threshold_w", 0) or 0),
                is_on=state in ("on", "heat", "true"),
                last_on=rt.get("last_on", 0.0),
                last_off=rt.get("last_off", 0.0),
                starts_today=rt.get("starts", 0),
                max_starts=int(cfg.get("max_starts_per_day", 0) or 0),
                min_runtime_s=int(cfg.get("min_runtime_min", 0) or 0) * 60,
                min_off_s=int(cfg.get("min_off_min", 0) or 0) * 60,
            ))
        return out

    def _mods(self) -> list[dict]:
        """Modulating (setpoint) self-consumption loads with current setpoint."""
        out = []
        for d in self._store.strategy_devices():
            cfg = d["cfg"]
            eid = d.get("setpoint")
            if d.get("control_mode") != "setpoint" or not eid or not cfg.get("self_consumption"):
                continue
            max_w = float(cfg.get("max_w", 0) or 0)
            if max_w <= 0:
                continue  # needs an upper power bound to modulate
            try:
                cur_unit = float(self._store.live_state(eid).get("state"))
            except (TypeError, ValueError):
                cur_unit = 0.0
            wpu = float(cfg.get("w_per_unit", 1) or 1) or 1.0
            out.append({"entity": eid, "domain": eid.split(".", 1)[0],
                        "cur_unit": cur_unit, "cur_w": cur_unit * wpu, "wpu": wpu,
                        "min_w": float(cfg.get("min_w", 0) or 0), "max_w": max_w,
                        "priority": int(cfg.get("priority", 5))})
        return out

    async def _modulate(self, surplus_signed: float) -> None:
        for a in decide_modulation(surplus_signed, self._mods()):
            if abs(a["unit"] - a["cur_unit"]) < 0.1:
                continue
            if a["domain"] not in ("number", "input_number"):
                continue
            try:
                await self._call_service(a["domain"], "set_value", a["entity"], {"value": a["unit"]})
                _LOGGER.info("Control: %s -> %s (regelbar, %d W)", a["entity"], a["unit"], a["power_w"])
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Modulation failed for %s: %s", a["entity"], err)

    async def run_once(self, now: float) -> Optional[tuple[str, str, str]]:
        if not self._store.control_enabled():
            return None
        balance = self._store.balance()
        # Grid-centric signal: + = export (surplus available), − = import.
        surplus_signed = -float(balance.get("grid_w", 0.0) or 0.0)
        action = None
        consumers = self._build()
        if consumers:
            action = decide_action(now, surplus_signed, consumers)
            if action is not None:
                entity_id, what, reason = action
                domain = entity_id.split(".", 1)[0]
                service = "turn_on" if what == "on" else "turn_off"
                try:
                    await self._call_service(domain, service, entity_id)
                    self._store.note_switch(entity_id, what == "on", reason)
                    _LOGGER.info("Control: %s %s (%s)", service, entity_id, reason)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Control action failed for %s: %s", entity_id, err)
        # Modulating loads absorb the remaining surplus every cycle.
        await self._modulate(surplus_signed)
        return action
