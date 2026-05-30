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


class ControlEngine:
    """Builds decision input from the store and executes one action per cycle."""

    def __init__(self, store, call_service: Callable[[str, str, str], Awaitable]):
        self._store = store
        self._call_service = call_service

    def _build(self) -> list[ConsumerDecision]:
        out: list[ConsumerDecision] = []
        for c in self._store.list_consumers():
            if not (c["auto"] and c["controllable"]):
                continue
            cfg = c["config"]
            rt = self._store.runtime(c["entity_id"])
            out.append(ConsumerDecision(
                entity_id=c["entity_id"],
                domain=c["entity_id"].split(".", 1)[0],
                priority=int(cfg.get("priority", 5)),
                nominal_power_w=float(cfg.get("nominal_power_w", 0) or 0),
                pv_threshold_w=float(cfg.get("pv_surplus_threshold_w", 0) or 0),
                is_on=bool(c["is_on"]),
                last_on=rt.get("last_on", 0.0),
                last_off=rt.get("last_off", 0.0),
                starts_today=rt.get("starts", 0),
                max_starts=int(cfg.get("max_starts_per_day", 0) or 0),
                min_runtime_s=int(cfg.get("min_runtime_min", 0) or 0) * 60,
                min_off_s=int(cfg.get("min_off_min", 0) or 0) * 60,
            ))
        return out

    async def run_once(self, now: float) -> Optional[tuple[str, str, str]]:
        if not self._store.control_enabled():
            return None
        consumers = self._build()
        if not consumers:
            return None
        balance = self._store.balance()
        action = decide_action(now, balance.get("surplus_w", 0.0), consumers)
        if action is None:
            return None
        entity_id, what, reason = action
        domain = entity_id.split(".", 1)[0]
        service = "turn_on" if what == "on" else "turn_off"
        try:
            await self._call_service(domain, service, entity_id)
            self._store.note_switch(entity_id, what == "on", reason)
            _LOGGER.info("Control: %s %s (%s)", service, entity_id, reason)
            return action
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Control action failed for %s: %s", entity_id, err)
            return None
