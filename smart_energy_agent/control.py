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
from .control_core import (
    Command, CommandSet, Controller, Cycle, ProcessImage, apply_commands,
)

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
    interruptible: bool = True            # may be switched off mid-run when surplus drops


def surplus_signal(
    grid_w: float, batt_w: float, loads_first: bool,
    soc: Optional[float] = None, min_soc: float = 0.0,
) -> float:
    """Signed PV-surplus signal for modulation (+export available / −deficit).

    The raw ``−grid_w`` ("regulate grid to zero") is wrong when a battery is
    present: a *discharging* battery holds the grid at ~0 by itself, so the
    controller would never throttle a load back and would happily run it off the
    battery. We therefore fold the battery power in (sign: + = charging,
    − = discharging):

      * ``loads_first``  → ``−grid_w + batt_w`` (= pv − house_load): loads may
        also absorb power that would otherwise charge the battery (no round-trip
        loss); the battery charges only with what loads leave.
      * battery first (default) → ``−grid_w + min(0, batt_w)``: only battery
        *discharge* is subtracted, so loads get the export overflow but never
        pre-empt charging.

    ``min_soc`` is a charge-priority floor: in ``loads_first`` mode the battery
    keeps its charging power (behaves battery-first) while its SoC is still below
    ``min_soc``, so the storage is filled to that reserve before controllable
    loads may divert the charge power. It has no effect in battery-first mode or
    when ``min_soc <= 0``. Discharge is always subtracted regardless, so a
    controllable load is never sustained from the battery. With no battery
    (``batt_w == 0``) the signal reduces to the original ``−grid_w``.
    """
    divert_charge = loads_first and (
        min_soc <= 0 or (soc is not None and soc >= min_soc))
    batt_term = batt_w if divert_charge else min(0.0, batt_w)
    return -grid_w + batt_term


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
        # Only interruptible loads are shed on import; a non-interruptible load
        # (e.g. a washing-machine program) keeps running until it is satisfied
        # (handled by the "done" branch above).
        cands = [
            c for c in consumers
            if c.is_on and c.interruptible and (now - c.last_on) >= c.min_runtime_s
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


def decide_grid_discharge(
    price_ct: Optional[float], discharge_min_ct: float,
    soc: Optional[float], soc_min: float,
) -> bool:
    """Whether to force-discharge the battery now (dynamic-tariff arbitrage).

    Discharge when the price is at/above ``discharge_min_ct`` (0 = disabled) and
    the SoC is still above the reserve floor ``soc_min``.
    """
    if soc is None or discharge_min_ct <= 0:
        return False
    if soc <= soc_min:
        return False  # keep the reserve
    return price_ct is not None and price_ct >= discharge_min_ct


def battery_tariff_mode(
    price_ct: Optional[float], charge_max_ct: float, discharge_min_ct: float,
    soc: Optional[float], soc_min: float, soc_max: float,
) -> Optional[str]:
    """Tariff-driven battery action: 'charge', 'discharge', or None (idle/surplus).
    Charging takes precedence if both conditions somehow apply."""
    if decide_grid_charge(price_ct, charge_max_ct, soc, soc_min, soc_max):
        return "charge"
    if decide_grid_discharge(price_ct, discharge_min_ct, soc, soc_min):
        return "discharge"
    return None


def plan_modulation(mods: list[dict], surplus_signed: float) -> list[Command]:
    """Pure planner: turn modulating loads + the signed surplus into setpoint
    commands (no IO). Batteries with a tariff mode are commanded explicitly
    (charge / forced discharge); idle batteries join the PV-surplus allocation.
    Same ordering/semantics as the previous ``_modulate``."""
    out: list[Command] = []
    normal = [m for m in mods if not m.get("is_batt")]
    for m in [m for m in mods if m.get("is_batt")]:
        mode, disc, wpu = m.get("batt_mode"), m.get("discharge", ""), (m["wpu"] or 1.0)
        if mode == "charge":
            out.append(Command(m["entity"], "set", round(m["max_w"] / wpu, 2), "Netzladen (Tarif)"))
            out.append(Command(disc, "set", 0.0, "Entladen aus"))
        elif mode == "discharge":
            out.append(Command(disc, "set", round(m["max_w"] / wpu, 2), "Entladen (Tarif)"))
            out.append(Command(m["entity"], "set", 0.0, "Laden aus (Entladen)"))
        else:
            out.append(Command(disc, "set", 0.0, "Entladen aus"))
            normal.append(m)   # idle battery follows the PV surplus
    for a in decide_modulation(surplus_signed, normal):
        out.append(Command(a["entity"], "set", a["unit"], f"regelbar, {round(a['power_w'])} W"))
    return out


class PvSurplusSwitchController:
    """Switches auto-consumers on/off from the PV-surplus signal (one per cycle)."""

    name = "pv_surplus_switch"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        if not image.consumers:
            return
        action = decide_action(image.now, image.surplus_signed, image.consumers)
        if action is not None:
            entity, what, reason = action
            cmds.add(Command(entity, "on" if what == "on" else "off", reason=reason))


class PvSurplusModulationController:
    """Distributes the signed PV surplus across modulating loads + batteries."""

    name = "pv_surplus_modulation"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        for cmd in plan_modulation(image.mods, image.surplus_signed):
            cmds.add(cmd)


def plan_tariff(now: float, cheap: bool, loads: list[dict]) -> list[Command]:
    """Pure planner: turn tariff-shift decisions into on/off/set commands."""
    out: list[Command] = []
    for entity, command, reason in decide_tariff_actions(now, cheap, loads):
        if command == "on":
            out.append(Command(entity, "on", reason=reason))
        elif command == "off":
            out.append(Command(entity, "off", reason=reason))
        elif isinstance(command, (int, float)):
            out.append(Command(entity, "set", float(command), reason))  # setpoint
    return out


class TariffShiftController:
    """Runs deferrable tariff-shift loads in cheap windows. Lower priority than
    the PV-surplus controllers (it only touches devices they don't), and runs at
    the slower tariff cadence."""

    name = "tariff_shift"
    interval = const.TARIFF_INTERVAL

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        for cmd in plan_tariff(image.now, bool(image.extra.get("tariff_cheap")),
                               image.extra.get("tariff_loads", [])):
            cmds.add(cmd)


class ControlEngine:
    """Builds the process image from the store and runs the unified control cycle
    (PV-surplus every tick + tariff shifting at its slower cadence)."""

    def __init__(self, store, call_service: Callable[[str, str, str], Awaitable]):
        self._store = store
        self._call_service = call_service
        self._last_run: dict[str, float] = {}   # controller name -> last run time

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
                interruptible=bool(cfg.get("interruptible", True)),
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
            # Battery on the dynamic tariff: charge (cheap/negative or below the
            # reserve), discharge (expensive), or None → follow PV surplus.
            batt_mode = None
            if grid_charge:
                t = self._store.tariff()
                batt_mode = battery_tariff_mode(
                    self._store.current_price_ct(),
                    float(t.get("charge_max_ct", 0) or 0),
                    float(t.get("discharge_min_ct", 0) or 0),
                    self._battery_soc(d),
                    float(cfg.get("grid_soc_min", 0) or 0),
                    float(cfg.get("grid_soc_max", 100) or 100),
                )
                if batt_mode in ("charge", "discharge"):
                    eff_max = max_w   # full power; charge-stop "satisfied" doesn't apply here
            out.append({"entity": eid, "domain": eid.split(".", 1)[0],
                        "cur_unit": cur_unit, "cur_w": cur_unit * wpu, "wpu": wpu,
                        "min_w": float(cfg.get("min_w", 0) or 0), "max_w": eff_max,
                        "priority": int(cfg.get("priority", 5)),
                        "is_batt": is_batt, "batt_mode": batt_mode,
                        "discharge": d.get("discharge", "")})
        return out

    # Controller chains (highest priority first). run_once() uses the PV-only
    # chain (stable for direct/unit-test callers); run_cycle() adds tariff.
    CHAIN: list[Controller] = [PvSurplusSwitchController(), PvSurplusModulationController()]
    FULL_CHAIN: list[Controller] = CHAIN + [TariffShiftController()]

    def _surplus_signed(self, balance: dict) -> float:
        # PV-surplus signal: + = export available, − = deficit. Folds in battery
        # power so a discharging battery is never mistaken for surplus (which
        # would let a load run off the battery); see surplus_signal().
        soc = balance.get("battery_soc")
        return surplus_signal(
            float(balance.get("grid_w", 0.0) or 0.0),
            float(balance.get("battery_w", 0.0) or 0.0),
            self._store.surplus_loads_first(),
            float(soc) if soc is not None else None,
            self._store.surplus_battery_min_soc(),
        )

    def build_image(self, now: float) -> ProcessImage:
        """The Input phase: one consistent snapshot of all controller inputs."""
        balance = self._store.balance()
        return ProcessImage(
            now=now,
            surplus_signed=self._surplus_signed(balance),
            consumers=self._build(),
            mods=self._mods(),
        )

    async def _modulate(self, surplus_signed: float) -> None:
        """Plan + write only the modulating setpoints (kept for direct callers)."""
        cmds = CommandSet()
        for cmd in plan_modulation(self._mods(), surplus_signed):
            cmds.add(cmd)
        await apply_commands(self._call_service, self._store, cmds)

    async def run_once(self, now: float) -> Optional[tuple[str, str, str]]:
        if not self._store.control_enabled():
            return None
        image = self.build_image(now)                 # Input
        cmds = Cycle(self.CHAIN).run(image)           # Process
        await apply_commands(self._call_service, self._store, cmds)   # Output
        # Return the switch action (if any) for compatibility / logging.
        for cmd in cmds.commands():
            if cmd.kind in ("on", "off"):
                return (cmd.entity, cmd.kind, cmd.reason)
        return None

    def _tariff_loads(self) -> list[dict]:
        """Deferrable tariff-shift loads — those NOT also driven by the PV-surplus
        engine (batteries are grid-charged by the PV controllers, not here)."""
        lt = time.localtime()
        now_min = lt.tm_hour * 60 + lt.tm_min
        out: list[dict] = []
        for d in self._store.strategy_devices():
            cfg = d["cfg"]
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
                    "interruptible": bool(cfg.get("interruptible", True)),
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

    async def run_cycle(self, now: float) -> None:
        """Unified Input → Process → Output cycle. The PV-surplus controllers run
        every tick; the tariff controller runs only at its slower interval."""
        if not self._store.control_enabled():
            return
        image = self.build_image(now)                       # Input
        # Tariff inputs are only gathered when the tariff controller is due.
        tariff_due = (now - self._last_run.get(TariffShiftController.name, -1e18)
                      ) >= (const.TARIFF_INTERVAL - 1)
        if tariff_due:
            image.extra["tariff_cheap"] = bool(self._store.tariff_cheap_now().get("cheap"))
            image.extra["tariff_loads"] = self._tariff_loads()
        cmds = CommandSet()                                 # Process (cadence-gated)
        for c in self.FULL_CHAIN:
            interval = getattr(c, "interval", const.CONTROL_INTERVAL)
            if now - self._last_run.get(c.name, -1e18) >= interval - 1:
                try:
                    c.process(image, cmds)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Controller %s failed: %s", c.name, err)
                self._last_run[c.name] = now
        await apply_commands(self._call_service, self._store, cmds)   # Output


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
            elif (not want and l["is_on"] and (now - l["last_on"]) >= l["min_runtime_s"]
                  # A non-interruptible load is only stopped once satisfied (done),
                  # never merely because the tariff stopped being cheap.
                  and (l.get("interruptible", True) or l.get("satisfied"))):
                actions.append((l["entity"], "off",
                                "Ziel erreicht" if l.get("satisfied") else "Tarif nicht günstig"))
        else:  # setpoint / modulating
            target = l["max_unit"] if want else 0.0
            if abs(target - l["cur_unit"]) >= 0.1:
                actions.append((l["entity"], round(target, 2),
                                "günstiger Tarif" if want else "Tarif/Ziel"))
    return actions
