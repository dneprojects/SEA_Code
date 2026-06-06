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
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from . import const

_LOGGER = logging.getLogger(__name__)


def _hhmm_to_min(value: object) -> Optional[int]:
    """'HH:MM' -> minute-of-day, or None if unset/invalid."""
    try:
        h, m = str(value).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _deadline_due(deadline_min: Optional[int], now_min: int) -> bool:
    """Whether a latest-start deadline has been reached (within the force window)."""
    return (deadline_min is not None
            and deadline_min <= now_min < deadline_min + const.DEADLINE_FORCE_WINDOW_MIN)


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
    satisfied: bool = False
    deadline_min: Optional[int] = None   # latest-start time as minute-of-day
    now_min: int = 0                      # current local minute-of-day


def decide_action(
    now: float, surplus_w: float, consumers: list[ConsumerDecision]
) -> Optional[tuple[str, str, str]]:
    """Return (entity_id, "on"|"off", reason) for one action, or None."""
    on_margin = const.CONTROL_ON_MARGIN_W
    off_margin = const.CONTROL_OFF_MARGIN_W

    # Deadline override: a deferrable load that must start by its "latest start"
    # time is force-started even without surplus, within a window after the
    # deadline. Highest precedence so the appliance reliably runs in time.
    due = [c for c in consumers
           if not c.is_on and not c.satisfied
           and _deadline_due(c.deadline_min, c.now_min)
           and (now - c.last_off) >= c.min_off_s]
    if due:
        due.sort(key=lambda c: -c.priority)
        return (due[0].entity_id, "on", "Deadline – Start erzwungen")

    # A satisfied load (target reached, e.g. vehicle SoC / temperature) is shed
    # first so the surplus is freed for other consumers.
    done = [c for c in consumers
            if c.is_on and c.satisfied and (now - c.last_on) >= c.min_runtime_s]
    if done:
        done.sort(key=lambda c: -c.nominal_power_w)
        return (done[0].entity_id, "off", "Ziel erreicht")

    if surplus_w > on_margin:
        cands = [
            c for c in consumers
            if not c.is_on and not c.satisfied
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
        raw = min(m["max_w"], m["cur_w"] + remaining)
        # Below its minimum power the load switches OFF rather than forcing grid
        # import to sustain it (correct for a wallbox minimum charge current).
        # min_w defaults to 0 for simple loads, so they are unaffected.
        target_w = raw if raw > 0 and raw >= m["min_w"] else 0.0
        remaining -= (target_w - m["cur_w"])
        out.append({"entity": m["entity"], "domain": m["domain"],
                    "unit": round(target_w / wpu, 2), "cur_unit": m["cur_unit"],
                    "power_w": round(target_w, 1)})
    return out


def decide_grid_charge(
    price_ct: Optional[float], charge_max_ct: float,
    soc: Optional[float], soc_min: float, soc_max: float,
) -> bool:
    """Whether to charge storage from the grid now (dynamic-tariff strategy).

    Two reasons: (1) below the reserve floor ``soc_min`` → top up at any price;
    (2) the current price is at/under ``charge_max_ct`` (default 0 = only free or
    negative prices) and the SoC is still below the ``soc_max`` target. Charging
    a battery from a positive-price grid usually loses money to round-trip
    losses, hence the conservative default.
    """
    if soc is None:
        return False
    if soc_min > 0 and soc < soc_min:
        return True
    if price_ct is not None and price_ct <= charge_max_ct and soc < soc_max:
        return True
    return False


class ControlEngine:
    """Builds decision input from the store and executes one action per cycle."""

    def __init__(self, store, call_service: Callable[[str, str, str], Awaitable]):
        self._store = store
        self._call_service = call_service

    def _build(self) -> list[ConsumerDecision]:
        """Build decisions from the wizard-configured devices that opted into
        PV-surplus self-consumption and are switchable."""
        lt = time.localtime()
        now_min = lt.tm_hour * 60 + lt.tm_min
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
                satisfied=bool(d.get("satisfied")),
                deadline_min=_hhmm_to_min(cfg.get("latest_start", "")),
                now_min=now_min,
            ))
        return out

    def _battery_soc(self, d: dict) -> Optional[float]:
        s = d.get("soc")
        if not s:
            return None
        try:
            return float(self._store.live_state(s).get("state"))
        except (TypeError, ValueError):
            return None

    def _mods(self) -> list[dict]:
        """Modulating (setpoint) loads with current setpoint. Includes PV-surplus
        loads plus batteries enabled for tariff grid-charging."""
        out = []
        for d in self._store.strategy_devices():
            cfg = d["cfg"]
            eid = d.get("setpoint")
            if d.get("control_mode") != "setpoint" or not eid:
                continue
            is_batt = d.get("kind") == "battery"
            grid_charge = is_batt and bool(cfg.get("tariff_shift"))
            if not cfg.get("self_consumption") and not grid_charge:
                continue
            max_w = float(cfg.get("max_w", 0) or 0)
            if max_w <= 0:
                continue  # needs an upper power bound to modulate
            try:
                cur_unit = float(self._store.live_state(eid).get("state"))
            except (TypeError, ValueError):
                cur_unit = 0.0
            wpu = float(cfg.get("w_per_unit", 1) or 1) or 1.0
            # A satisfied modulating load (limit reached) is driven to 0 so the
            # surplus is freed for the others.
            eff_max = 0.0 if d.get("satisfied") else max_w
            # Wallbox: only charge while the vehicle is connected/ready.
            ready = cfg.get("ready_entity")
            if eff_max and ready and not self._store.entity_truthy(ready):
                eff_max = 0.0
            # Battery: grid-charge from the dynamic tariff (cheap/negative price or
            # below the reserve floor) takes precedence over surplus modulation.
            force_full = False
            if grid_charge and decide_grid_charge(
                self._store.current_price_ct(),
                float(self._store.tariff().get("charge_max_ct", 0) or 0),
                self._battery_soc(d),
                float(cfg.get("grid_soc_min", 0) or 0),
                float(cfg.get("grid_soc_max", 100) or 100),
            ):
                force_full = True
                eff_max = max_w
            out.append({"entity": eid, "domain": eid.split(".", 1)[0],
                        "cur_unit": cur_unit, "cur_w": cur_unit * wpu, "wpu": wpu,
                        "min_w": float(cfg.get("min_w", 0) or 0), "max_w": eff_max,
                        "priority": int(cfg.get("priority", 5)), "force_full": force_full})
        return out

    async def _set_unit(self, m: dict, unit: float, label: str) -> None:
        if abs(unit - m["cur_unit"]) < 0.1 or m["domain"] not in ("number", "input_number"):
            return
        try:
            await self._call_service(m["domain"], "set_value", m["entity"], {"value": unit})
            _LOGGER.info("Control: %s -> %s (%s)", m["entity"], unit, label)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Modulation failed for %s: %s", m["entity"], err)

    async def _modulate(self, surplus_signed: float) -> None:
        mods = self._mods()
        # Grid-charging batteries are driven to full power (from the grid), and
        # kept out of the surplus allocation below.
        forced = [m for m in mods if m.get("force_full")]
        for m in forced:
            await self._set_unit(m, round(m["max_w"] / (m["wpu"] or 1.0), 2), "Netzladen (Tarif)")
        normal = [m for m in mods if not m.get("force_full")]
        for a in decide_modulation(surplus_signed, normal):
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


def decide_tariff_actions(
    now: float, cheap: bool, loads: list[dict]
) -> list[tuple[str, object, str]]:
    """Plan tariff-shift actions: run deferrable loads while the tariff is cheap,
    OR once their latest-start deadline is reached (then run regardless of price,
    so e.g. a washing machine still finishes in time).

    Returns ``(entity, command, reason)`` where command is ``"on"``/``"off"`` for
    switchable loads or a numeric setpoint (entity's own unit) for modulating
    loads. Respects min-runtime / min-off guards; a satisfied load is idled.
    """
    actions: list[tuple[str, object, str]] = []
    for l in loads:
        due = _deadline_due(l.get("deadline_min"), l.get("now_min", 0))
        want = (cheap or due) and not l.get("satisfied")
        forced = due and not cheap
        if l["mode"] == "switch":
            if want and not l["is_on"] and (now - l["last_off"]) >= l["min_off_s"]:
                actions.append((l["entity"], "on",
                                "Deadline – Start erzwungen" if forced else "günstiger Tarif"))
            elif not want and l["is_on"] and (now - l["last_on"]) >= l["min_runtime_s"]:
                actions.append((l["entity"], "off",
                                "Ziel erreicht" if l.get("satisfied") else "Tarif nicht günstig"))
        else:  # setpoint / modulating
            target = l["max_unit"] if want else 0.0
            if abs(target - l["cur_unit"]) >= 0.1:
                actions.append((l["entity"], round(target, 2),
                                "günstiger Tarif" if want else "Tarif/Ziel"))
    return actions


class TariffEngine:
    """Runs deferrable ``tariff_shift`` loads during cheap tariff periods.

    Universal: the cheap/expensive decision comes from ``store.tariff_cheap_now``
    which adapts to whatever price information is registered (dynamic forecast,
    threshold, or HT/NT window). Only touches devices that opted into tariff
    shifting and are NOT also driven by the PV-surplus engine.
    """

    def __init__(self, store, call_service: Callable[..., Awaitable]):
        self._store = store
        self._call_service = call_service

    def _loads(self) -> list[dict]:
        lt = time.localtime()
        now_min = lt.tm_hour * 60 + lt.tm_min
        out: list[dict] = []
        for d in self._store.strategy_devices():
            cfg = d["cfg"]
            # Batteries are grid-charged by the ControlEngine (SoC-aware), not here.
            if (not cfg.get("tariff_shift") or cfg.get("self_consumption")
                    or d.get("kind") == "battery"):
                continue
            mode = d.get("control_mode")
            if mode == "switch" and d.get("switch"):
                eid = d["switch"]
                rt = self._store.runtime(eid)
                state = str(self._store.live_state(eid).get("state", "")).lower()
                out.append({
                    "entity": eid, "mode": "switch",
                    "is_on": state in ("on", "heat", "true"),
                    "last_on": rt.get("last_on", 0.0), "last_off": rt.get("last_off", 0.0),
                    "min_runtime_s": int(cfg.get("min_runtime_min", 0) or 0) * 60,
                    "min_off_s": int(cfg.get("min_off_min", 0) or 0) * 60,
                    "satisfied": bool(d.get("satisfied")),
                    "deadline_min": _hhmm_to_min(cfg.get("latest_start", "")),
                    "now_min": now_min,
                })
            elif mode == "setpoint" and d.get("setpoint"):
                eid = d["setpoint"]
                wpu = float(cfg.get("w_per_unit", 1) or 1) or 1.0
                try:
                    cur_unit = float(self._store.live_state(eid).get("state"))
                except (TypeError, ValueError):
                    cur_unit = 0.0
                out.append({
                    "entity": eid, "mode": "setpoint", "cur_unit": cur_unit,
                    "max_unit": round(float(cfg.get("max_w", 0) or 0) / wpu, 2),
                    "satisfied": bool(d.get("satisfied")),
                })
        return out

    async def run_once(self, now: float) -> None:
        if not self._store.control_enabled():
            return
        cheap = bool(self._store.tariff_cheap_now().get("cheap"))
        for entity, command, reason in decide_tariff_actions(now, cheap, self._loads()):
            domain = entity.split(".", 1)[0]
            try:
                if command in ("on", "off"):
                    await self._call_service(
                        domain, "turn_on" if command == "on" else "turn_off", entity)
                    self._store.note_switch(entity, command == "on", reason)
                    _LOGGER.info("Tariff: %s %s (%s)", command, entity, reason)
                elif domain in ("number", "input_number"):
                    await self._call_service(domain, "set_value", entity, {"value": command})
                    _LOGGER.info("Tariff: %s -> %s (%s)", entity, command, reason)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Tariff action failed for %s: %s", entity, err)
