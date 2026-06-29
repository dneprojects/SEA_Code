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
from .devices import (
    CAP_CHARGE, CAP_DISCHARGE, CAP_STAGED, ON_STATES, actuator_bounds, devices, select)
from .rules import RuleController, make_resolver

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


class PeakShavingController:
    """Caps grid import by discharging the battery above a configured draw.

    Highest priority: it runs first and locks the battery's discharge/charge
    setpoints, so the self-consumption / tariff controllers can no longer touch
    them this cycle (a hard constraint above self-consumption). Off (no peak
    config, limit ≤ 0, or import under the cap) it adds nothing → the battery is
    handled as before.
    """

    name = "peak_shaving"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        peak = image.extra.get("peak")
        if not peak or peak["limit_w"] <= 0:
            return
        over = image.grid_w - peak["limit_w"]          # import above the cap (W)
        if over <= 0:
            return
        for b in peak["batteries"]:
            if not b["discharge"] or over <= 0:
                continue
            if b["soc"] is not None and b["soc"] <= b["reserve"]:
                continue                               # keep the reserve
            power = min(b["max_w"], over)
            if power <= 0:
                continue
            wpu = b["wpu"] or 1.0
            cmds.add(Command(b["discharge"], "set", round(power / wpu, 2),
                             f"Peak-Shaving (Netzbezug {round(image.grid_w)} W)"))
            if b["charge"]:
                cmds.add(Command(b["charge"], "set", 0.0, "Peak-Shaving (Laden aus)"))
            over -= power


class EssReserveController:
    """Battery reserve / care as hard constraints — the highest priority.

    Below the configured reserve SoC the battery may not be force-discharged by
    ANY controller (peak shaving, tariff arbitrage, the future optimizer); above
    the max SoC it may not be force-charged. It only emits bounds, never targets,
    so it can never *start* an action — it only narrows what others may do. No
    reserve/max configured (reserve 0 / max 100) → adds nothing (behaviour
    equivalent).
    """

    name = "ess_reserve"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        for b in image.extra.get("ess_batteries", []):
            soc = b.get("soc")
            if soc is None:
                continue
            emerg = b.get("emergency", 0.0)
            floor = max(b.get("reserve", 0.0), emerg)
            # never force-discharge below the effective reserve floor
            if b["discharge"] and floor > 0 and soc <= floor:
                cmds.constrain(b["discharge"], hi=0.0, reason=f"Reserve {round(floor)} %")
            # SoC-max charge cap — lifted during a battery-care full charge
            if b["charge"] and not b.get("care") and b["soc_max"] < 100 and soc >= b["soc_max"]:
                cmds.constrain(b["charge"], hi=0.0, reason=f"SoC-Max {round(b['soc_max'])} %")
            # emergency backup: actively recharge to the reserve
            if b["charge"] and emerg > 0 and soc < emerg:
                wpu = b.get("wpu") or 1.0
                cmds.add(Command(b["charge"], "set", round(b.get("max_w", 0.0) / wpu, 2),
                                 f"Notstromreserve laden ({round(emerg)} %)"))


def active_peak_limit(now_min: int, default_limit: float, slots: list) -> float:
    """Effective grid-import cap now: a matching time-slot wins, else the default."""
    for s in slots or []:
        st, en = _hhmm_to_min(s.get("start")), _hhmm_to_min(s.get("end"))
        if st is None or en is None:
            continue
        inside = (st <= now_min < en) if st <= en else (now_min >= st or now_min < en)
        if inside:
            try:
                return float(s.get("limit_w") or 0)
            except (TypeError, ValueError):
                return default_limit
    return default_limit


def plan_feed_in_limit(export_w: float, limit_w: float, batteries: list[dict]) -> list[Command]:
    """Cap grid export at ``limit_w`` by force-charging the battery with the
    excess. batteries: ``{charge, max_w, wpu, soc, soc_max}``."""
    out: list[Command] = []
    over = export_w - limit_w
    if over <= 0:
        return out
    for b in batteries:
        if not b["charge"] or over <= 0:
            continue
        if b["soc"] is not None and b["soc"] >= b["soc_max"]:
            continue                                       # already full
        power = min(b["max_w"], over)
        if power <= 0:
            continue
        wpu = b["wpu"] or 1.0
        out.append(Command(b["charge"], "set", round(power / wpu, 2),
                           f"Einspeise-Limit (Export {round(export_w)} W)"))
        over -= power
    return out


class FeedInLimitController:
    """Caps grid export at a configured limit by force-charging the battery
    (grid-code feed-in limitation). No-op without a limit/over-export."""

    name = "feed_in_limit"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        fi = image.extra.get("feed_in")
        if not fi or fi["limit_w"] <= 0:
            return
        export = max(0.0, -image.grid_w)
        for cmd in plan_feed_in_limit(export, fi["limit_w"], fi["batteries"]):
            cmds.add(cmd)


class BatteryCareController:
    """Battery care: a periodic full charge (SoC calibration). The engine decides
    which batteries are due; here we just drive them to full (the SoC-max cap is
    lifted for them in EssReserveController)."""

    name = "battery_care"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        for b in image.extra.get("care", []):
            wpu = b["wpu"] or 1.0
            cmds.add(Command(b["charge"], "set", round(b["max_w"] / wpu, 2),
                             "Batteriepflege: Vollladung"))
            if b["discharge"]:
                cmds.add(Command(b["discharge"], "set", 0.0, "Batteriepflege"))


def evcs_gate(e: dict, now: float) -> Optional[tuple[bool, str]]:
    """EV charging gate for one wallbox: ``(force_on, reason)`` or ``None``.

    Returns a hard override only when a configured signal demands it; otherwise
    ``None`` so the generic PV-surplus path keeps deciding (surplus-only charging
    via the device's nominal power as the on threshold). Order: not-connected and
    target-SoC force OFF; a charge deadline forces ON; 'charge from grid' charges
    whenever connected.
    """
    if e["connected_set"] and not e["connected"]:
        return (False, "nicht angesteckt")
    if e["satisfied"]:
        return (False, "Ziel-SoC erreicht")
    if _deadline_due(e["deadline_min"], e["now_min"]):
        return (True, "Deadline – Laden erzwungen")
    if e["from_grid"] and e["connected"]:
        return (True, "Laden (Netz erlaubt)")
    return None


class EvcsController:
    """EV-charging gates layered on top of the generic surplus control.

    Highest priority among the load controllers, but it only emits an override
    when a configured signal demands it (see :func:`evcs_gate`) — otherwise it
    adds nothing and the wallbox is driven by the normal PV-surplus path (so the
    'surplus only' mode and the ~11 kW on-threshold come for free from the device
    nominal power). Switch wallboxes are turned on/off; a modulating wallbox is
    constrained to 0 (off) or to at least its minimum (forced on).
    """

    name = "evcs"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        for e in image.extra.get("evcs", []):
            gate = evcs_gate(e, image.now)
            if gate is None:
                continue                                   # generic path decides
            on, reason = gate
            label = f"Wallbox: {reason}"
            if e["mode"] == "switch" and e["switch"]:
                cmds.add(Command(e["switch"], "on" if on else "off", reason=label))
            elif e["setpoint"]:
                if on:
                    cmds.add(Command(e["setpoint"], "set", e["min_unit"], label))
                else:
                    cmds.add(Command(e["setpoint"], "set", 0.0, label))


def plan_stages(surplus_signed: float, stages: list[str], stage_power_w: float,
                on_count: int, force: bool = False) -> list[tuple[str, str]]:
    """Pure planner for a staged load (e.g. a 3-relay heating rod).

    The number of stages the PV can support = ``gross_surplus // stage_power``,
    where ``gross`` adds back the power the currently-on stages already draw (the
    live surplus already nets them out). Stages fill bottom-up; returns only the
    *changed* stage switches as ``(entity, "on"/"off")``. ``force`` (a deadline)
    turns all stages on regardless of surplus."""
    n = len(stages)
    if n == 0 or stage_power_w <= 0:
        return []
    if force:
        target = n
    else:
        gross = surplus_signed + on_count * stage_power_w
        target = max(0, min(n, int(gross // stage_power_w)))
    if target > on_count:
        return [(stages[i], "on") for i in range(on_count, target)]
    if target < on_count:
        return [(stages[i], "off") for i in range(target, on_count)]
    return []


def staged_force(today_kwh: float, min_kwh_day: float, total_power_w: float,
                 now_min: int, deadline_min: Optional[int]) -> bool:
    """Whether a staged load must run now to still reach its daily minimum energy.

    Forces on once the time left until the target time (latest_start, else
    midnight) is only just enough to deliver the remaining deficit at full power
    — the energy analog of a 'latest start'. 0 minimum / no power → never."""
    if min_kwh_day <= 0 or total_power_w <= 0:
        return False
    deficit = min_kwh_day - today_kwh
    if deficit <= 0:
        return False
    end = deadline_min if deadline_min else 24 * 60
    time_left_h = max(0.0, (end - now_min) / 60.0)
    need_h = deficit / (total_power_w / 1000.0)
    return time_left_h <= need_h


class StagedLoadController:
    """Switches a multi-stage load on/off by stage from the PV surplus (the
    full-range heating-rod option: switch / setpoint are handled by the generic
    controllers, N relays here). A daily-minimum energy target (min_kwh_day) or a
    latest-start deadline forces all stages on. No-op without staged devices."""

    name = "staged_load"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        for s in image.extra.get("staged", []):
            force = _deadline_due(s["deadline_min"], s["now_min"]) or staged_force(
                s.get("today_kwh", 0.0), s.get("min_kwh_day", 0.0),
                s.get("total_power_w", 0.0), s["now_min"], s["deadline_min"])
            reason = "Heizstufe (Mindestmenge/Deadline)" if force else "Heizstufe (PV-Überschuss)"
            for entity, what in plan_stages(image.surplus_signed, s["stages"],
                                            s["stage_power_w"], s["on_count"], force):
                cmds.add(Command(entity, what, reason=reason))


def plan_sg_ready(surplus_w: float, threshold_w: float,
                  expensive: bool = False) -> tuple[bool, bool]:
    """Heat-pump SG-Ready relay pair ``(relay1, relay2)`` for the 4 states:
    ``[0,0]`` normal · ``[0,1]`` recommendation (soak surplus) · ``[1,1]`` forced
    on (lots of surplus) · ``[1,0]`` blocked (expensive/peak tariff)."""
    if expensive:
        return (True, False)                              # state 1: EVU lock
    if threshold_w > 0 and surplus_w >= 2 * threshold_w:
        return (True, True)                               # state 4: forced on
    if threshold_w > 0 and surplus_w >= threshold_w:
        return (False, True)                              # state 3: recommendation
    return (False, False)                                 # state 2: normal


class HeatPumpController:
    """SG-Ready heat pump: maps the PV surplus (and an expensive tariff) onto the
    2-relay / 4-state SG-Ready signal. No-op without sg-relay devices."""

    name = "heat_pump"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        for h in image.extra.get("heatpumps", []):
            r1, r2 = plan_sg_ready(image.surplus_signed, h["threshold_w"], h.get("expensive", False))
            cmds.add(Command(h["relay1"], "on" if r1 else "off", reason="WP SG-Ready"))
            cmds.add(Command(h["relay2"], "on" if r2 else "off", reason="WP SG-Ready"))


def plan_optimized_charge(slots: list[dict], soc: float, capacity_kwh: float,
                          reserve_soc: float, max_w: float, wpu: float,
                          soc_max: float = 100.0) -> Optional[dict]:
    """Forecast-aware battery action for the current slot (slots[0] = now).

    Each slot: ``pv_w``, ``load_w``, ``price_ct``, ``dt_h``. Returns
    ``{mode, power_w, reason}`` or ``None`` (idle). PV surplus always charges
    (free). For ToU it uses the horizon price *distribution* (cheap/expensive
    terciles) rather than a fixed threshold — and grid-optimized: it won't
    grid-charge in a cheap slot if the PV forecast will fill the battery anyway
    (needs a known capacity; otherwise that check is skipped)."""
    if not slots or max_w <= 0:
        return None
    now = slots[0]
    pv = now.get("pv_w") or 0.0
    load = now.get("load_w") or 0.0
    if pv - load > 50 and soc < soc_max:                       # PV surplus -> charge free
        return {"mode": "charge", "power_w": min(pv - load, max_w), "reason": "PV-Überschuss"}
    prices = [float(s["price_ct"]) for s in slots if s.get("price_ct") is not None]
    if len(prices) < 3:
        return None
    srt = sorted(prices)
    cheap, exp = srt[len(srt) // 3], srt[2 * len(srt) // 3]
    if now.get("price_ct") is None or exp <= cheap + 0.01:     # no usable price spread
        return None
    p = float(now["price_ct"])
    if p <= cheap and soc < soc_max:                           # cheap -> consider grid-charge
        if capacity_kwh > 0:
            room = capacity_kwh * (soc_max - soc) / 100.0
            pv_kwh = sum(max(0.0, (s.get("pv_w") or 0) - (s.get("load_w") or 0)) / 1000.0
                         * s.get("dt_h", 0.25) for s in slots)
            if pv_kwh >= room:
                return None                                    # PV will fill it -> don't grid-charge
        return {"mode": "charge", "power_w": max_w, "reason": "Optimierer: günstig laden"}
    if p >= exp and soc > reserve_soc and load > 0:            # expensive -> discharge to cover load
        return {"mode": "discharge", "power_w": min(max_w, load), "reason": "Optimierer: teuer entladen"}
    return None


class OptimizingController:
    """Forecast-based battery optimizer (ToU arbitrage + grid-optimized charge).

    Runs at the tariff cadence; emits a battery charge/discharge target that the
    reserve/peak constraints still bound. Off (no optimizer inputs) it adds
    nothing. The plan is computed in :func:`plan_optimized_charge`."""

    name = "optimizer"
    interval = const.TARIFF_INTERVAL

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        o = image.extra.get("optimizer")
        if not o:
            return
        act = plan_optimized_charge(o["slots"], o["soc"], o["capacity_kwh"],
                                    o["reserve"], o["max_w"], o["wpu"], o["soc_max"])
        if not act:
            return
        wpu = o["wpu"] or 1.0
        if act["mode"] == "charge" and o["charge"]:
            cmds.add(Command(o["charge"], "set", round(act["power_w"] / wpu, 2), act["reason"]))
            if o["discharge"]:
                cmds.add(Command(o["discharge"], "set", 0.0, "Optimierer"))
        elif act["mode"] == "discharge" and o["discharge"]:
            cmds.add(Command(o["discharge"], "set", round(act["power_w"] / wpu, 2), act["reason"]))
            if o["charge"]:
                cmds.add(Command(o["charge"], "set", 0.0, "Optimierer"))


class ControlEngine:
    """Builds the process image from the store and runs the unified control cycle
    (PV-surplus every tick + tariff shifting at its slower cadence)."""

    def __init__(self, store, call_service: Callable[[str, str, str], Awaitable]):
        self._store = store
        self._call_service = call_service
        self._last_run: dict[str, float] = {}   # controller name -> last run time
        self.last_trace: list[dict] = []        # last cycle's command trace (debug)
        self._staged_energy: dict[str, dict] = {}   # staged device key -> {day, kwh}
        self._soh_state: dict[str, int] = {}        # battery key -> day-ordinal of last full charge

    def _build(self) -> list[ConsumerDecision]:
        """Switchable PV-surplus auto-consumers, as decision records."""
        lt = time.localtime()
        now_min = lt.tm_hour * 60 + lt.tm_min
        out: list[ConsumerDecision] = []
        for dev in devices(self._store):
            if dev.mode != "switch" or not dev.switch_entity or not dev.self_consumption:
                continue
            rt = dev.runtime
            out.append(ConsumerDecision(
                entity_id=dev.switch_entity,
                domain=dev.switch_entity.split(".", 1)[0],
                priority=dev.priority,
                nominal_power_w=dev.power_w,
                pv_threshold_w=dev.pv_threshold_w,
                is_on=dev.is_on,
                last_on=rt.get("last_on", 0.0),
                last_off=rt.get("last_off", 0.0),
                starts_today=rt.get("starts", 0),
                max_starts=dev.max_starts,
                min_runtime_s=dev.min_runtime_s,
                min_off_s=dev.min_off_s,
                satisfied=dev.satisfied,
                deadline_min=_hhmm_to_min(dev.latest_start),
                now_min=now_min,
                interruptible=dev.interruptible,
            ))
        return out

    def _mods(self) -> list[dict]:
        """Modulating (setpoint) loads with current setpoint. Includes PV-surplus
        loads plus batteries enabled for tariff grid-charging."""
        out = []
        tariff: Optional[dict] = None
        for dev in devices(self._store):
            if dev.mode != "setpoint" or not dev.setpoint_entity:
                continue
            grid_charge = dev.is_battery and dev.tariff_shift
            if not dev.self_consumption and not grid_charge:
                continue
            if dev.max_w <= 0:
                continue  # needs an upper power bound to modulate
            # A satisfied modulating load (limit reached) is driven to 0 so the
            # surplus is freed for the others.
            eff_max = 0.0 if dev.satisfied else dev.max_w
            # Wallbox: only charge while the vehicle is connected/ready.
            if eff_max and dev.ready_entity and not dev.ready:
                eff_max = 0.0
            # Battery on the dynamic tariff: charge (cheap/negative or below the
            # reserve), discharge (expensive), or None → follow PV surplus.
            batt_mode = None
            if grid_charge:
                if tariff is None:
                    tariff = self._store.tariff()
                batt_mode = battery_tariff_mode(
                    self._store.current_price_ct(),
                    float(tariff.get("charge_max_ct", 0) or 0),
                    float(tariff.get("discharge_min_ct", 0) or 0),
                    dev.soc, dev.grid_soc_min, dev.grid_soc_max,
                )
                if batt_mode in ("charge", "discharge"):
                    eff_max = dev.max_w   # full power; charge-stop "satisfied" doesn't apply
            out.append({"entity": dev.setpoint_entity,
                        "domain": dev.setpoint_entity.split(".", 1)[0],
                        "cur_unit": dev.cur_unit, "cur_w": dev.cur_unit * dev.wpu,
                        "wpu": dev.wpu, "min_w": dev.min_w, "max_w": eff_max,
                        "priority": dev.priority, "is_batt": dev.is_battery,
                        "batt_mode": batt_mode, "discharge": dev.discharge_entity})
        return out

    # Controller chains (highest priority first). run_once() uses the PV-only
    # chain (stable for direct/unit-test callers); run_cycle() adds peak shaving
    # (a hard constraint, first) and tariff shifting (last, slow cadence).
    CHAIN: list[Controller] = [PvSurplusSwitchController(), PvSurplusModulationController()]
    FULL_CHAIN: list[Controller] = [
        BatteryCareController(), EssReserveController(), FeedInLimitController(),
        PeakShavingController(), OptimizingController(), EvcsController(),
        StagedLoadController(), HeatPumpController(), *CHAIN,
        TariffShiftController(), RuleController()]

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
            grid_w=float(balance.get("grid_w", 0.0) or 0.0),
            consumers=self._build(),
            mods=self._mods(),
        )

    def _peak_batteries(self) -> list[dict]:
        """Storage usable for peak shaving — anything with a forced-discharge
        actuator (battery today, a V2G vehicle later)."""
        return [{"discharge": dev.discharge_entity, "charge": dev.setpoint_entity,
                 "max_w": dev.max_w, "wpu": dev.wpu, "soc": dev.soc,
                 "reserve": dev.grid_soc_min}
                for dev in select(self._store, CAP_DISCHARGE)]

    def _storage_inputs(self) -> tuple[list[dict], list[dict]]:
        """Reserve/emergency/care inputs for all storage. Tracks each battery's
        last full-charge day for the periodic battery-care cycle, and returns
        ``(ess_batteries, care_batteries)``."""
        lt = time.localtime()
        day_ord = lt.tm_year * 366 + lt.tm_yday
        emerg = self._store.emergency_reserve_soc()
        cycle_days = self._store.soh_cycle_days()
        ess, care = [], []
        for d in devices(self._store):
            if not (d.has_cap(CAP_CHARGE) or d.has_cap(CAP_DISCHARGE)):
                continue
            soc = d.soc
            care_active = False
            if cycle_days > 0 and d.setpoint_entity:
                if soc is not None and soc >= 99:
                    self._soh_state[d.key] = day_ord            # full today -> done
                else:
                    last = self._soh_state.get(d.key)
                    care_active = last is None or (day_ord - last) >= cycle_days
            if care_active:
                care.append({"charge": d.setpoint_entity, "discharge": d.discharge_entity,
                             "max_w": d.max_w, "wpu": d.wpu})
            ess.append({"discharge": d.discharge_entity, "charge": d.setpoint_entity, "soc": soc,
                        "reserve": d.grid_soc_min, "soc_max": d.grid_soc_max, "max_w": d.max_w,
                        "wpu": d.wpu, "emergency": emerg, "care": care_active})
        return ess, care

    def _is_on(self, entity: str) -> bool:
        return str(self._store.live_state(entity).get("state", "")).lower() in ON_STATES

    def _staged_inputs(self) -> list[dict]:
        """Inputs for staged (multi-relay) loads participating in PV surplus.
        Also accumulates today's delivered energy (per device, reset at midnight)
        so the daily-minimum guarantee can force stages on in time."""
        lt = time.localtime()
        now_min = lt.tm_hour * 60 + lt.tm_min
        day = lt.tm_year * 10000 + lt.tm_mon * 100 + lt.tm_mday
        dt_h = const.CONTROL_INTERVAL / 3600.0
        out = []
        for dev in select(self._store, CAP_STAGED):
            if not dev.self_consumption:
                continue
            stages = dev.stages
            if not stages or dev.max_w <= 0:
                continue
            sp = dev.max_w / len(stages)
            on_count = sum(1 for e in stages if self._is_on(e))
            acc = self._staged_energy.get(dev.key)
            if not acc or acc.get("day") != day:
                acc = {"day": day, "kwh": 0.0}
            acc["kwh"] += on_count * sp / 1000.0 * dt_h     # estimate from on stages
            self._staged_energy[dev.key] = acc
            out.append({
                "stages": stages, "stage_power_w": sp, "on_count": on_count,
                "deadline_min": _hhmm_to_min(dev.latest_start), "now_min": now_min,
                "min_kwh_day": dev.min_kwh_day, "today_kwh": acc["kwh"],
                "total_power_w": dev.max_w,
            })
        return out

    def _heatpump_inputs(self, expensive: bool) -> list[dict]:
        """SG-Ready heat pumps (two relay entities) participating in PV surplus."""
        out = []
        for dev in devices(self._store):
            if not dev.self_consumption or not (dev.sg_relay1 and dev.sg_relay2):
                continue
            out.append({"relay1": dev.sg_relay1, "relay2": dev.sg_relay2,
                        "threshold_w": dev.pv_threshold_w or dev.max_w, "expensive": expensive})
        return out

    async def _optimizer_inputs(self) -> Optional[dict]:
        """Build the optimizer horizon: PV/load forecast slots + per-slot price,
        plus the first storage battery's state/limits. None if no battery or
        forecast is available."""
        bat = next((d for d in devices(self._store)
                    if d.has_cap(CAP_CHARGE) or d.has_cap(CAP_DISCHARGE)), None)
        if bat is None:
            return None
        try:
            fb = await self._store.forecast_bundle(hours=24)
            pts = (fb.get("surplus") or {}).get("points") or []
        except Exception:  # noqa: BLE001
            pts = []
        if not pts:
            return None
        slots = []
        for i, pt in enumerate(pts):
            ts = pt.get("ts")
            nxt = pts[i + 1].get("ts") if i + 1 < len(pts) else (ts + 900 if ts else None)
            dt_h = max(0.05, (nxt - ts) / 3600.0) if (ts and nxt) else 0.25
            slots.append({"pv_w": pt.get("pv_w") or 0.0,
                          "load_w": pt.get("load_w", pt.get("watt")) or 0.0,
                          "price_ct": self._store.price_at(ts), "dt_h": dt_h})
        return {"slots": slots, "soc": bat.soc or 0.0, "capacity_kwh": bat.capacity_kwh,
                "reserve": bat.grid_soc_min, "soc_max": bat.grid_soc_max,
                "max_w": bat.max_w, "wpu": bat.wpu,
                "charge": bat.setpoint_entity, "discharge": bat.discharge_entity}

    def _state_num(self, entity: str) -> Optional[float]:
        if not entity:
            return None
        try:
            return float(self._store.live_state(entity).get("state"))
        except (TypeError, ValueError):
            return None

    def _evcs_inputs(self) -> list[dict]:
        """Gate inputs for participating EV chargers (wallboxes). When a vehicle
        is linked and present, its SoC/target/deadline drive the stop condition;
        otherwise the charger's own config applies (backward compatible)."""
        lt = time.localtime()
        now_min = lt.tm_hour * 60 + lt.tm_min
        out = []
        for dev in devices(self._store):
            if not dev.is_evcs or not dev.self_consumption:
                continue
            veh = self._store.vehicle_for_charger(dev.key)
            if veh:
                soc = self._state_num(veh.get("soc_entity", ""))
                target = float(veh.get("target_soc") or 0)
                satisfied = bool(target > 0 and soc is not None and soc >= target)
                deadline_min = _hhmm_to_min(str(veh.get("deadline") or ""))
            else:
                satisfied = dev.satisfied
                deadline_min = _hhmm_to_min(dev.latest_start)
            out.append({
                "switch": dev.switch_entity, "setpoint": dev.setpoint_entity, "mode": dev.mode,
                "min_unit": round(dev.min_w / dev.wpu, 2),
                "connected_set": bool(dev.ready_entity), "connected": dev.ready,
                "satisfied": satisfied, "from_grid": dev.charge_from_grid,
                "deadline_min": deadline_min, "now_min": now_min,
            })
        return out

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
        cmds.bounds = actuator_bounds(self._store)    # device hard limits
        self.last_trace = cmds.trace()                # who decided what (debug)
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
        for dev in devices(self._store):
            if not dev.tariff_shift or dev.self_consumption or dev.is_battery:
                continue
            if dev.mode == "switch" and dev.switch_entity:
                rt = dev.runtime
                out.append({
                    "entity": dev.switch_entity, "mode": "switch", "is_on": dev.is_on,
                    "last_on": rt.get("last_on", 0.0), "last_off": rt.get("last_off", 0.0),
                    "min_runtime_s": dev.min_runtime_s, "min_off_s": dev.min_off_s,
                    "satisfied": dev.satisfied, "deadline_min": _hhmm_to_min(dev.latest_start),
                    "now_min": now_min, "interruptible": dev.interruptible,
                })
            elif dev.mode == "setpoint" and dev.setpoint_entity:
                out.append({
                    "entity": dev.setpoint_entity, "mode": "setpoint", "cur_unit": dev.cur_unit,
                    "max_unit": round(dev.max_w / dev.wpu, 2), "satisfied": dev.satisfied,
                })
        return out

    async def run_cycle(self, now: float) -> None:
        """Unified Input → Process → Output cycle. The PV-surplus controllers run
        every tick; the tariff controller runs only at its slower interval."""
        # Two independent switches: the master for PV-surplus + peak shaving, and
        # a separate one for tariff load-shifting. Either may run on its own.
        surplus_on = self._store.control_enabled()
        tariff_on = self._store.tariff_enabled()
        optimizer_on = self._store.optimizer_enabled()
        if not (surplus_on or tariff_on or optimizer_on):
            return
        image = self.build_image(now)                       # Input
        lt = time.localtime(now)
        now_min = lt.tm_hour * 60 + lt.tm_min
        # Storage reserve / emergency-backup / care (runs whenever we control).
        ess, care = self._storage_inputs()
        if ess:
            image.extra["ess_batteries"] = ess
            fi_limit = self._store.feed_in_limit_w()
            if fi_limit > 0:                                # grid feed-in limit
                image.extra["feed_in"] = {"limit_w": fi_limit, "batteries": [
                    {"charge": e["charge"], "max_w": e["max_w"], "wpu": e["wpu"],
                     "soc": e["soc"], "soc_max": e["soc_max"]} for e in ess]}
        if care:
            image.extra["care"] = care
        # Peak shaving inputs — the effective cap may vary by time slot.
        peak_limit = active_peak_limit(now_min, self._store.peak_limit_w(), self._store.peak_slots())
        if surplus_on and peak_limit > 0:
            image.extra["peak"] = {"limit_w": peak_limit, "batteries": self._peak_batteries()}
        # EVCS (wallbox) gate inputs for participating chargers.
        if surplus_on:
            evcs = self._evcs_inputs()
            if evcs:
                image.extra["evcs"] = evcs
            staged = self._staged_inputs()
            if staged:
                image.extra["staged"] = staged
            expensive = tariff_on and not bool(self._store.tariff_cheap_now().get("cheap"))
            hp = self._heatpump_inputs(expensive)
            if hp:
                image.extra["heatpumps"] = hp
        # Tariff inputs are gathered only when tariff shifting is on and due.
        tariff_due = (now - self._last_run.get(TariffShiftController.name, -1e18)
                      ) >= (const.TARIFF_INTERVAL - 1)
        if tariff_on and tariff_due:
            image.extra["tariff_cheap"] = bool(self._store.tariff_cheap_now().get("cheap"))
            image.extra["tariff_loads"] = self._tariff_loads()
        # Optimizer inputs (forecast-based) at the tariff cadence.
        opt_due = (now - self._last_run.get(OptimizingController.name, -1e18)
                   ) >= (const.TARIFF_INTERVAL - 1)
        if optimizer_on and opt_due:
            o = await self._optimizer_inputs()
            if o:
                image.extra["optimizer"] = o
        # Declarative rules (only when any are configured).
        rules = self._store.control_rules()
        if rules:
            image.extra["rules"] = rules
            image.extra["rule_resolve"] = make_resolver(image, self._store)
        cmds = CommandSet()                                 # Process (cadence-gated)
        n = len(self.FULL_CHAIN)
        for idx, c in enumerate(self.FULL_CHAIN):
            # Reserve is a safety guard (runs whenever the cycle runs); the tariff
            # controller follows tariff_enabled; everything else the PV master.
            if c.name in (EssReserveController.name, BatteryCareController.name,
                          FeedInLimitController.name):
                active = surplus_on or tariff_on or optimizer_on   # battery safety/grid guards
            elif c.name == TariffShiftController.name:
                active = tariff_on
            elif c.name == OptimizingController.name:
                active = optimizer_on
            else:
                active = surplus_on
            if not active:
                continue
            interval = getattr(c, "interval", const.CONTROL_INTERVAL)
            if now - self._last_run.get(c.name, -1e18) >= interval - 1:
                cmds.current_source = c.name
                cmds.current_priority = n - idx          # chain order -> priority
                try:
                    c.process(image, cmds)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("Controller %s failed: %s", c.name, err)
                self._last_run[c.name] = now
        cmds.bounds = actuator_bounds(self._store)          # device hard limits
        self.last_trace = cmds.trace()                      # who decided what (debug)
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
