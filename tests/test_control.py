"""Tests for the PV-surplus control: switch decision + modulating allocation."""

from __future__ import annotations

import asyncio

from smart_energy_agent.control import (
    ConsumerDecision, ControlEngine, battery_tariff_mode, decide_action,
    decide_grid_charge, decide_grid_discharge, decide_modulation, decide_tariff_actions,
    surplus_signal,
)
from smart_energy_agent.store import Store


def _cd(entity, **kw):
    base = dict(domain=entity.split(".", 1)[0], priority=5, nominal_power_w=800,
                pv_threshold_w=0, is_on=False, last_on=0, last_off=0, starts_today=0,
                max_starts=0, min_runtime_s=0, min_off_s=0, satisfied=False)
    base.update(kw)
    return ConsumerDecision(entity, **base)


def _mod(entity, cur_w=0.0, wpu=1.0, min_w=0.0, max_w=3000.0, priority=5):
    return {"entity": entity, "domain": entity.split(".", 1)[0],
            "cur_unit": cur_w / wpu, "cur_w": cur_w, "wpu": wpu,
            "min_w": min_w, "max_w": max_w, "priority": priority}


def test_modulation_absorbs_export_and_sheds_on_import():
    out = decide_modulation(1500, [_mod("number.hz")])
    assert out[0]["unit"] == 1500.0
    out = decide_modulation(-500, [_mod("number.hz", cur_w=2000)])
    assert out[0]["unit"] == 1500.0


def test_modulation_clamps_to_max():
    out = decide_modulation(9999, [_mod("number.hz", max_w=3000)])
    assert out[0]["unit"] == 3000.0


def test_modulation_priority_split():
    mods = [_mod("number.a", max_w=1000, priority=9), _mod("number.b", max_w=1000, priority=1)]
    out = {o["entity"]: o["unit"] for o in decide_modulation(1500, mods)}
    assert out["number.a"] == 1000.0 and out["number.b"] == 500.0


def test_modulation_w_per_unit_wallbox_amperes():
    out = decide_modulation(3450, [_mod("number.amp", wpu=690, max_w=11040)])
    assert out[0]["unit"] == 5.0  # 3450 W / 690 W/A


def test_modulation_off_below_minimum_power():
    # Wallbox min charge power 4140 W: a 2000 W surplus is below min -> off (0),
    # and the would-be power is freed (not forced from grid).
    out = decide_modulation(2000, [_mod("number.amp", wpu=690, min_w=4140, max_w=11040)])
    assert out[0]["unit"] == 0.0
    # 5000 W surplus is above min -> charge ~7.25 A
    out = decide_modulation(5000, [_mod("number.amp", wpu=690, min_w=4140, max_w=11040)])
    assert out[0]["power_w"] == 5000.0


def test_surplus_signal_no_battery_equals_minus_grid():
    # Without a battery (batt_w == 0) both policies reduce to the old -grid_w.
    assert surplus_signal(-200, 0, False) == 200    # export -> surplus
    assert surplus_signal(-200, 0, True) == 200
    assert surplus_signal(300, 0, False) == -300    # import -> deficit
    assert surplus_signal(300, 0, True) == -300


def test_surplus_signal_discharging_battery_is_never_surplus():
    # Battery discharging 900 W holds the grid at ~0; a naive -grid_w would read
    # 0 (no throttle). Both policies must instead see a deficit so loads back off
    # and are never run from the battery.
    assert surplus_signal(0, -900, False) == -900
    assert surplus_signal(0, -900, True) == -900


def test_surplus_signal_charging_battery_policy_difference():
    # PV surplus 1700 W fully absorbed by battery charging, grid ~0.
    # battery-first: loads stay off (battery keeps the charge).
    assert surplus_signal(0, 1700, False) == 0
    # loads-first: loads may take the charging power directly (no round-trip).
    assert surplus_signal(0, 1700, True) == 1700


def test_surplus_signal_min_soc_holds_charge_until_reserve():
    # loads-first, battery charging 1700 W, grid ~0, min_soc 50 %.
    # Below the reserve the battery keeps charging (battery-first behaviour).
    assert surplus_signal(0, 1700, True, soc=40, min_soc=50) == 0
    # At/above the reserve loads may divert the charge power.
    assert surplus_signal(0, 1700, True, soc=50, min_soc=50) == 1700
    # min_soc has no effect in battery-first mode.
    assert surplus_signal(0, 1700, False, soc=90, min_soc=50) == 0
    # Discharge stays subtracted regardless of the reserve.
    assert surplus_signal(0, -900, True, soc=10, min_soc=50) == -900


def _heater_store(grid_w, batt_w, heater_setpoint_w, *, loads_first=False, pv_w=0.0,
                  batt_soc=None, min_soc=0.0):
    s = Store()
    s._config = {
        "grid": {"power": "sensor.g"},
        "pv": [{"id": "p1", "name": "PV", "powers": [{"entity": "sensor.pv"}]}],
        "battery": [{"id": "b1", "name": "Akku", "power": "sensor.bp", "soc": "sensor.soc"}],
        "water_heater": [{"id": "w1", "name": "Heizstab",
                          "powers": [{"entity": "sensor.hzp"}],
                          "control": {"setpoint": "number.hz"}}],
    }
    s._settings["control_enabled"] = True
    s._settings["surplus_loads_first"] = loads_first
    s._settings["surplus_battery_min_soc"] = min_soc
    s._settings["strategy_loads"] = {
        "water_heater:w1": {"self_consumption": True, "max_w": 3000, "w_per_unit": 1},
    }
    s._live_by_id = {
        "sensor.g": {"state": str(grid_w)},
        "sensor.bp": {"state": str(batt_w)},
        "sensor.soc": ({"state": str(batt_soc)} if batt_soc is not None else {}),
        "sensor.pv": {"state": str(pv_w)},
        "sensor.hzp": {"state": str(heater_setpoint_w)},
        "number.hz": {"state": str(heater_setpoint_w)},
    }
    return s


def test_engine_heater_backs_off_when_battery_discharges():
    # No PV, battery discharging to cover the house; grid ~0. The heater was left
    # at 600 W -> it must be driven to 0 (not sustained from the battery).
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((entity, data))

    s = _heater_store(grid_w=0, batt_w=-900, heater_setpoint_w=600)
    eng = ControlEngine(s, cs)
    # debounce: a sustained deficit sheds after MOD_SHED_DEBOUNCE cycles (a single
    # out-of-sync sample must not). Second cycle confirms it.
    asyncio.run(eng.run_once(0.0))
    asyncio.run(eng.run_once(60.0))
    assert ("number.hz", {"value": 0.0}) in calls


def test_engine_heater_runs_only_on_real_export():
    # 800 W exported to grid, no battery activity -> heater absorbs the surplus.
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((entity, data))

    s = _heater_store(grid_w=-800, batt_w=0, heater_setpoint_w=0, pv_w=2000)
    asyncio.run(ControlEngine(s, cs).run_once(0.0))
    assert ("number.hz", {"value": 800.0}) in calls


def test_engine_loads_first_diverts_battery_charging_to_heater():
    # PV surplus fully charging the battery, grid ~0. With loads-first the heater
    # is ramped to take the charge power directly.
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((entity, data))

    s = _heater_store(grid_w=0, batt_w=1700, heater_setpoint_w=0, loads_first=True, pv_w=2000)
    asyncio.run(ControlEngine(s, cs).run_once(0.0))
    assert ("number.hz", {"value": 1700.0}) in calls


def test_engine_battery_first_keeps_charging_over_heater():
    # Same situation but battery-first (default): the heater stays off so the
    # battery keeps charging.
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((entity, data))

    s = _heater_store(grid_w=0, batt_w=1700, heater_setpoint_w=0, loads_first=False, pv_w=2000)
    asyncio.run(ControlEngine(s, cs).run_once(0.0))
    # The setpoint is (re)written every cycle (keepalive) but stays at 0 — the
    # heater never gets the charge power; the battery keeps charging.
    assert ("number.hz", {"value": 0.0}) in calls
    assert not any(e == "number.hz" and d != {"value": 0.0} for e, d in calls)


def test_engine_min_soc_keeps_charging_below_reserve_then_diverts():
    # loads-first with a 60 % reserve. At 40 % SoC the battery keeps charging
    # (heater stays off); at 60 % the heater may divert the charge power.
    below, above = [], []

    async def cs_below(domain, service, entity, data=None):
        below.append((entity, data))

    async def cs_above(domain, service, entity, data=None):
        above.append((entity, data))

    s1 = _heater_store(grid_w=0, batt_w=1700, heater_setpoint_w=0,
                       loads_first=True, pv_w=2000, batt_soc=40, min_soc=60)
    asyncio.run(ControlEngine(s1, cs_below).run_once(0.0))
    # below reserve: heater stays at 0 (written each cycle, never the surplus)
    assert not any(e == "number.hz" and d != {"value": 0.0} for e, d in below)

    s2 = _heater_store(grid_w=0, batt_w=1700, heater_setpoint_w=0,
                       loads_first=True, pv_w=2000, batt_soc=60, min_soc=60)
    asyncio.run(ControlEngine(s2, cs_above).run_once(0.0))
    assert ("number.hz", {"value": 1700.0}) in above


def _peak_image(grid_w, *, limit=2000.0, max_w=5000.0, soc=60.0, reserve=20.0,
                discharge="number.bd", charge="number.bc"):
    from smart_energy_agent.control_core import ProcessImage
    img = ProcessImage(now=0.0, grid_w=grid_w)
    img.extra["peak"] = {"limit_w": limit, "batteries": [
        {"discharge": discharge, "charge": charge, "max_w": max_w, "wpu": 1.0,
         "soc": soc, "reserve": reserve}]}
    return img


def test_peak_shaving_discharges_battery_above_cap():
    from smart_energy_agent.control import PeakShavingController
    from smart_energy_agent.control_core import CommandSet
    cmds = CommandSet()
    PeakShavingController().process(_peak_image(3000.0), cmds)   # 1000 W over the 2000 cap
    t = {x["entity"]: x for x in cmds.trace()}
    assert t["number.bd"]["value"] == 1000.0     # discharge exactly the overshoot
    assert t["number.bc"]["value"] == 0.0        # charging forced off


def test_peak_shaving_clamps_to_battery_power_and_respects_reserve_and_cap():
    from smart_energy_agent.control import PeakShavingController
    from smart_energy_agent.control_core import CommandSet
    # overshoot 9000 W but battery max 5000 -> clamp
    cmds = CommandSet()
    PeakShavingController().process(_peak_image(11000.0, max_w=5000.0), cmds)
    assert {x["entity"]: x["value"] for x in cmds.trace()}["number.bd"] == 5000.0
    # under the cap -> nothing
    cmds = CommandSet()
    PeakShavingController().process(_peak_image(1500.0), cmds)
    assert cmds.commands() == []
    # at/below the reserve SoC -> keep reserve, do nothing
    cmds = CommandSet()
    PeakShavingController().process(_peak_image(3000.0, soc=20.0, reserve=20.0), cmds)
    assert cmds.commands() == []


def test_ess_reserve_clamps_discharge_and_charge_as_hard_bounds():
    from smart_energy_agent.control import EssReserveController
    from smart_energy_agent.control_core import Command, CommandSet, ProcessImage

    def _run(batt, target_entity, target_val):
        img = ProcessImage(now=0.0)
        img.extra["ess_batteries"] = [batt]
        cmds = CommandSet()
        cmds.current_priority = 1                       # a lower-priority strategy target
        cmds.add(Command(target_entity, "set", target_val, "strategy"))
        cmds.current_priority = 99                      # reserve runs highest in the chain
        EssReserveController().process(img, cmds)
        return {c.entity: c.value for c in cmds.commands()}[target_entity]

    base = {"discharge": "number.bd", "charge": "number.bc", "soc": 15.0,
            "reserve": 20.0, "soc_max": 100.0}
    # below reserve -> discharge forced to 0 regardless of the strategy's target
    assert _run(base, "number.bd", 500.0) == 0.0
    # above reserve -> discharge target passes through
    assert _run({**base, "soc": 50.0}, "number.bd", 500.0) == 500.0
    # at/above max SoC -> charge forced to 0
    assert _run({**base, "soc": 95.0, "soc_max": 90.0}, "number.bc", 1500.0) == 0.0
    # reserve 0 (default) -> never clamps
    assert _run({**base, "soc": 0.0, "reserve": 0.0}, "number.bd", 500.0) == 500.0


def test_evcs_gate_logic():
    from smart_energy_agent.control import evcs_gate
    base = {"connected_set": True, "connected": True, "satisfied": False,
            "from_grid": False, "deadline_min": None, "now_min": 600}
    assert evcs_gate(base, 0.0) is None                          # surplus-only -> generic decides
    assert evcs_gate({**base, "connected": False}, 0.0)[0] is False     # unplugged -> off
    assert evcs_gate({**base, "satisfied": True}, 0.0)[0] is False      # target SoC reached -> off
    assert evcs_gate({**base, "from_grid": True}, 0.0)[0] is True       # grid allowed + plugged -> on
    assert evcs_gate({**base, "deadline_min": 590}, 0.0)[0] is True     # deadline due -> on
    # connection not configured -> never gates on the plug signal
    assert evcs_gate({**base, "connected_set": False, "connected": False}, 0.0) is None


def test_evcs_controller_switch_and_setpoint():
    from smart_energy_agent.control import EvcsController
    from smart_energy_agent.control_core import CommandSet, ProcessImage
    off = {"switch": "switch.wb", "setpoint": "", "mode": "switch", "min_unit": 6.0,
           "connected_set": True, "connected": False, "satisfied": False,
           "from_grid": False, "deadline_min": None, "now_min": 600}
    img = ProcessImage(now=0.0); img.extra["evcs"] = [off]
    cmds = CommandSet(); EvcsController().process(img, cmds)
    assert {c.entity: c for c in cmds.commands()}["switch.wb"].kind == "off"   # unplugged -> off
    # connected + surplus-only -> no gate, generic path decides
    img2 = ProcessImage(now=0.0); img2.extra["evcs"] = [{**off, "connected": True}]
    cmds2 = CommandSet(); EvcsController().process(img2, cmds2)
    assert cmds2.commands() == []
    # modulating wallbox, grid allowed -> forced to its minimum
    img3 = ProcessImage(now=0.0)
    img3.extra["evcs"] = [{**off, "switch": "", "setpoint": "number.wb", "mode": "setpoint",
                           "connected": True, "from_grid": True}]
    cmds3 = CommandSet(); EvcsController().process(img3, cmds3)
    assert {c.entity: c for c in cmds3.commands()}["number.wb"].value == 6.0


def test_plan_stages_surplus_and_deadline():
    from smart_energy_agent.control import plan_stages
    st = ["switch.s1", "switch.s2", "switch.s3"]
    assert plan_stages(2500, st, 1000, 0) == [("switch.s1", "on"), ("switch.s2", "on")]
    # importing with 3 on: gross = -1500 + 3000 = 1500 -> target 1 -> shed s2, s3
    assert plan_stages(-1500, st, 1000, 3) == [("switch.s2", "off"), ("switch.s3", "off")]
    assert plan_stages(200, st, 1000, 1) == []                  # steady (target == on_count)
    assert plan_stages(-9999, st, 1000, 0, force=True) == [
        ("switch.s1", "on"), ("switch.s2", "on"), ("switch.s3", "on")]   # deadline forces all
    assert plan_stages(5000, [], 1000, 0) == [] and plan_stages(5000, st, 0, 0) == []


def test_staged_controller_emits_stage_changes():
    from smart_energy_agent.control import StagedLoadController
    from smart_energy_agent.control_core import CommandSet, ProcessImage
    img = ProcessImage(now=0.0, surplus_signed=2500.0)
    img.extra["staged"] = [{"stages": ["switch.s1", "switch.s2", "switch.s3"],
                            "stage_power_w": 1000.0, "on_count": 0,
                            "deadline_min": None, "now_min": 600}]
    cmds = CommandSet()
    StagedLoadController().process(img, cmds)
    assert {c.entity: c.kind for c in cmds.commands()} == {"switch.s1": "on", "switch.s2": "on"}


def test_staged_force_daily_minimum():
    from smart_energy_agent.control import staged_force
    # need 5 kWh, none yet, 3 kW total; midnight target. 12:00 -> 12h left vs 1.67h need -> no
    assert staged_force(0, 5, 3000, 720, None) is False
    # 22:30 -> 1.5h left < 1.67h need -> force
    assert staged_force(0, 5, 3000, 1350, None) is True
    assert staged_force(5, 5, 3000, 1350, None) is False        # already met
    assert staged_force(0, 0, 3000, 1350, None) is False        # no minimum
    assert staged_force(0, 5, 0, 1350, None) is False           # no power
    # explicit 18:00 target, 16:30 now -> 1.5h left < 1.67h need -> force
    assert staged_force(0, 5, 3000, 990, 1080) is True


def test_plan_sg_ready_states():
    from smart_energy_agent.control import plan_sg_ready
    assert plan_sg_ready(0, 1000) == (False, False)             # normal
    assert plan_sg_ready(1500, 1000) == (False, True)           # recommendation
    assert plan_sg_ready(2500, 1000) == (True, True)            # forced (>= 2x threshold)
    assert plan_sg_ready(5000, 1000, expensive=True) == (True, False)   # blocked wins
    assert plan_sg_ready(5000, 0) == (False, False)             # no threshold -> normal


def test_active_peak_limit_time_slots():
    from smart_energy_agent.control import active_peak_limit
    slots = [{"start": "17:00", "end": "20:00", "limit_w": 3000}]
    assert active_peak_limit(18 * 60, 0, slots) == 3000        # inside slot
    assert active_peak_limit(10 * 60, 5000, slots) == 5000     # outside -> default
    night = [{"start": "22:00", "end": "06:00", "limit_w": 2000}]   # overnight wrap
    assert active_peak_limit(23 * 60, 0, night) == 2000
    assert active_peak_limit(2 * 60, 0, night) == 2000
    assert active_peak_limit(12 * 60, 0, night) == 0
    assert active_peak_limit(12 * 60, 4000, []) == 4000        # no slots -> default


def test_plan_feed_in_limit():
    from smart_energy_agent.control import plan_feed_in_limit
    bats = [{"charge": "number.bc", "max_w": 5000, "wpu": 1, "soc": 50, "soc_max": 100}]
    c, ab = plan_feed_in_limit(4000, 1000, bats)               # absorb 3000 into the battery
    assert c and c[0].entity == "number.bc" and c[0].value == 3000 and ab == 3000
    assert plan_feed_in_limit(500, 1000, bats) == ([], 0.0)    # export under limit
    full, _ = plan_feed_in_limit(4000, 1000, [{**bats[0], "soc": 100}])
    assert full == []                                          # battery full
    big, ab2 = plan_feed_in_limit(20000, 0, bats)
    assert big and big[0].value == 5000 and ab2 == 5000        # clamped to battery max


def test_feed_in_controller_curtails_pv_when_battery_full_and_releases():
    from smart_energy_agent.control import FeedInLimitController
    from smart_energy_agent.control_core import CommandSet, ProcessImage
    fi = {"limit_w": 1000, "pv_limit": "number.pv", "pv_limit_max": 10000,
          "batteries": [{"charge": "number.bc", "max_w": 5000, "wpu": 1, "soc": 100, "soc_max": 100}]}
    img = ProcessImage(now=0.0, grid_w=-6000.0, pv_w=7000.0)   # exporting 6000, battery full
    img.extra["feed_in"] = fi
    cmds = CommandSet(); FeedInLimitController().process(img, cmds)
    assert {c.entity: c.value for c in cmds.commands()}["number.pv"] == 2000   # 7000 - (6000-1000)
    img2 = ProcessImage(now=0.0, grid_w=-500.0, pv_w=3000.0)   # under limit -> release
    img2.extra["feed_in"] = fi
    cmds2 = CommandSet(); FeedInLimitController().process(img2, cmds2)
    assert {c.entity: c.value for c in cmds2.commands()}["number.pv"] == 10000


def test_ess_reserve_emergency_forces_charge_and_floor():
    from smart_energy_agent.control import EssReserveController
    from smart_energy_agent.control_core import Command, CommandSet, ProcessImage
    img = ProcessImage(now=0.0)
    img.extra["ess_batteries"] = [{"discharge": "number.bd", "charge": "number.bc", "soc": 15.0,
                                   "reserve": 0.0, "soc_max": 100.0, "max_w": 5000, "wpu": 1,
                                   "emergency": 30.0, "care": False}]
    cmds = CommandSet()
    cmds.current_priority = 1
    cmds.add(Command("number.bd", "set", 2000.0, "strategy discharge"))   # low-prio target
    cmds.current_priority = 99
    EssReserveController().process(img, cmds)
    out = {c.entity: c.value for c in cmds.commands()}
    assert out["number.bc"] == 5000     # soc < emergency -> actively recharge backup
    assert out["number.bd"] == 0        # below reserve floor -> discharge blocked


def test_ess_reserve_emergency_skips_recharge_when_expensive():
    from smart_energy_agent.control import EssReserveController
    from smart_energy_agent.control_core import CommandSet, ProcessImage
    base = {"discharge": "number.bd", "charge": "number.bc", "reserve": 0.0, "soc_max": 100.0,
            "max_w": 5000, "wpu": 1, "emergency": 30.0, "care": False}
    img = ProcessImage(now=0.0); img.extra["ess_batteries"] = [{**base, "soc": 20.0, "expensive": True}]
    cmds = CommandSet(); EssReserveController().process(img, cmds)
    assert "number.bc" not in {c.entity for c in cmds.commands()}   # expensive + not critical -> skip
    img2 = ProcessImage(now=0.0); img2.extra["ess_batteries"] = [{**base, "soc": 10.0, "expensive": True}]
    cmds2 = CommandSet(); EssReserveController().process(img2, cmds2)
    assert {c.entity: c.value for c in cmds2.commands()}["number.bc"] == 5000   # critically low -> charge


def test_battery_care_controller_forces_full_charge():
    from smart_energy_agent.control import BatteryCareController
    from smart_energy_agent.control_core import CommandSet, ProcessImage
    img = ProcessImage(now=0.0)
    img.extra["care"] = [{"charge": "number.bc", "discharge": "number.bd", "max_w": 5000, "wpu": 1}]
    cmds = CommandSet()
    BatteryCareController().process(img, cmds)
    out = {c.entity: c.value for c in cmds.commands()}
    assert out["number.bc"] == 5000 and out["number.bd"] == 0


def test_plan_optimized_charge():
    from smart_energy_agent.control import plan_optimized_charge

    def slot(pv, load, price):
        return {"pv_w": pv, "load_w": load, "price_ct": price, "dt_h": 1.0}

    varied = [slot(0, 1000, 10), slot(0, 1000, 15), slot(0, 1000, 20),
              slot(0, 1000, 25), slot(0, 1000, 28), slot(0, 1000, 30)]
    # cheapest slot now, capacity unknown -> grid-charge
    a = plan_optimized_charge(varied, 50, 0, 20, 5000, 1)
    assert a and a["mode"] == "charge" and a["reason"].startswith("Optimierer")
    # most expensive now -> discharge to cover load
    b = plan_optimized_charge([slot(0, 1000, 30)] + varied[:5], 50, 0, 20, 5000, 1)
    assert b and b["mode"] == "discharge"
    # PV surplus now wins regardless of price
    c = plan_optimized_charge([slot(3000, 1000, 30)] + varied[:5], 50, 0, 20, 5000, 1)
    assert c and c["mode"] == "charge" and c["reason"] == "PV-Überschuss"
    # grid-optimized: cheap now but the PV forecast will fill the battery -> skip grid-charge
    pvfill = [slot(0, 500, 10)] + [slot(6000, 500, p) for p in (15, 20, 25, 28, 30)]
    assert plan_optimized_charge(pvfill, 50, 5, 20, 5000, 1) is None
    # flat price, no surplus -> idle
    assert plan_optimized_charge([slot(0, 1000, 20)] * 6, 50, 0, 20, 5000, 1) is None
    # guards
    assert plan_optimized_charge([], 50, 0, 20, 5000, 1) is None
    assert plan_optimized_charge([slot(0, 1000, 20)] * 6, 50, 0, 20, 0, 1) is None


def test_strategy_preset_arms_master_switches():
    s = Store()
    s.set_settings({"strategy": "hybrid"})
    assert s.control_enabled() and s.tariff_enabled() and s.optimizer_enabled()
    s.set_settings({"strategy": "self_consumption"})
    assert s.control_enabled() and not s.tariff_enabled() and not s.optimizer_enabled()
    s.set_settings({"strategy": "cost"})
    assert s.tariff_enabled() and s.optimizer_enabled()
    s.set_settings({"strategy": "autarky"})
    assert s.control_enabled() and not s.tariff_enabled() and not s.optimizer_enabled()


def test_battery_arbitrage_gated_by_tariff_switch_and_optimizer():
    s = Store()
    s._config = {"battery": [{"id": "b1", "name": "B", "charge_power": "number.bc",
                              "discharge_power": "number.bd", "soc": "sensor.soc"}]}
    s._settings["strategy_loads"] = {"battery:b1": {"tariff_shift": True, "self_consumption": False,
                                                    "max_w": 5000, "w_per_unit": 1,
                                                    "grid_soc_min": 10, "grid_soc_max": 90}}
    s._settings["tariff"] = {"mode": "static", "price_ct": 5, "charge_max_ct": 10}  # cheap -> charge
    s._live_by_id = {"number.bc": {"state": "0"}, "sensor.soc": {"state": "50"}}
    eng = ControlEngine(s, None)  # type: ignore[arg-type]
    s._settings["tariff_enabled"] = False
    s._settings["optimizer_enabled"] = False
    assert all(m.get("batt_mode") is None for m in eng._mods())   # tariff off -> no arbitrage
    s._settings["tariff_enabled"] = True
    bm = next((m for m in eng._mods() if m["entity"] == "number.bc"), None)
    assert bm and bm.get("batt_mode") == "charge"                 # tariff on -> arbitrage
    s._settings["optimizer_enabled"] = True
    assert all(m.get("batt_mode") is None for m in eng._mods())   # optimizer owns the battery


def test_run_cycle_charges_battery_from_surplus_end_to_end():
    """Full chain wiring: a configured battery charges from PV surplus."""
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((service, entity, data))

    s = Store()
    s._settings["control_enabled"] = True
    s._config = {"battery": [{"id": "b1", "name": "Bat", "charge_power": "number.bc",
                              "discharge_power": "number.bd", "soc": "sensor.soc"}]}
    s._settings["strategy_loads"] = {
        "battery:b1": {"self_consumption": True, "max_w": 5000, "w_per_unit": 1, "priority": 5}}
    s._live_by_id = {"number.bc": {"state": "0"}, "number.bd": {"state": "0"},
                     "sensor.soc": {"state": "50"}}
    s.balance = lambda: {"grid_w": -3000.0, "battery_w": 0.0, "battery_soc": 50.0, "pv_w": 4000.0}
    s.tariff_cheap_now = lambda: {"cheap": False}        # type: ignore[method-assign]
    eng = ControlEngine(s, cs)
    asyncio.run(eng.run_cycle(1000.0))
    charged = [c for c in calls if c[1] == "number.bc"]
    assert charged and charged[-1][2]["value"] > 0       # battery charged from surplus


def test_run_cycle_folds_in_tariff_and_throttles_to_its_interval():
    from smart_energy_agent import const
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((service, entity))

    s = Store()
    s._settings["control_enabled"] = True
    s._settings["tariff_enabled"] = True
    s._config = {"consumers": [{"id": "c1", "name": "WM", "control": {"switch": "switch.wm"}}]}
    s._settings["strategy_loads"] = {"consumers:c1": {"tariff_shift": True}}
    s._live_by_id = {"switch.wm": {"state": "off"}}
    s.tariff_cheap_now = lambda: {"cheap": True}      # type: ignore[method-assign]
    eng = ControlEngine(s, cs)

    asyncio.run(eng.run_cycle(1000.0))
    assert ("turn_on", "switch.wm") in calls          # tariff controller ran on the first tick
    calls.clear()
    asyncio.run(eng.run_cycle(1060.0))                # 60 s later: tariff not due (interval 300 s)
    assert ("turn_on", "switch.wm") not in calls
    calls.clear()
    asyncio.run(eng.run_cycle(1000.0 + const.TARIFF_INTERVAL))   # due again
    assert ("turn_on", "switch.wm") in calls


def test_run_cycle_tariff_runs_independently_of_pv_master():
    from smart_energy_agent import const
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((service, entity))

    s = Store()
    s._settings["control_enabled"] = False    # PV master OFF
    s._settings["tariff_enabled"] = True       # tariff switch ON
    s._config = {"consumers": [{"id": "c1", "name": "WM", "control": {"switch": "switch.wm"}}]}
    s._settings["strategy_loads"] = {"consumers:c1": {"tariff_shift": True}}
    s._live_by_id = {"switch.wm": {"state": "off"}}
    s.tariff_cheap_now = lambda: {"cheap": True}      # type: ignore[method-assign]
    eng = ControlEngine(s, cs)

    asyncio.run(eng.run_cycle(1000.0))
    assert ("turn_on", "switch.wm") in calls           # tariff ran despite master off
    # both switches off -> nothing happens at all
    s._settings["tariff_enabled"] = False
    calls.clear()
    asyncio.run(eng.run_cycle(1000.0 + const.TARIFF_INTERVAL * 2))
    assert calls == []


def test_tariff_switches_on_when_cheap_and_off_when_not():
    sw = {"entity": "switch.boiler", "mode": "switch", "is_on": False,
          "last_on": 0, "last_off": 0, "min_runtime_s": 0, "min_off_s": 0, "satisfied": False}
    on = decide_tariff_actions(1000, True, [sw])
    assert on == [("switch.boiler", "on", "günstiger Tarif")]
    sw_on = {**sw, "is_on": True}
    off = decide_tariff_actions(1000, False, [sw_on])
    assert off and off[0][1] == "off"
    # cheap but satisfied (target reached) -> stays off
    assert decide_tariff_actions(1000, True, [{**sw, "satisfied": True}]) == []


def test_tariff_deadline_forces_on_even_when_not_cheap():
    sw = {"entity": "switch.wm", "mode": "switch", "is_on": False, "last_on": 0, "last_off": 0,
          "min_runtime_s": 0, "min_off_s": 0, "satisfied": False, "deadline_min": 600, "now_min": 630}
    assert decide_tariff_actions(1000, False, [sw]) == [("switch.wm", "on", "Deadline – Start erzwungen")]


def test_tariff_no_force_before_deadline_when_not_cheap():
    sw = {"entity": "switch.wm", "mode": "switch", "is_on": False, "last_on": 0, "last_off": 0,
          "min_runtime_s": 0, "min_off_s": 0, "satisfied": False, "deadline_min": 600, "now_min": 500}
    assert decide_tariff_actions(1000, False, [sw]) == []


def test_grid_charge_threshold_soc_and_reserve():
    # default threshold 0 -> only free/negative prices
    assert decide_grid_charge(-2.0, 0.0, 50, 0, 100) is True
    assert decide_grid_charge(5.0, 0.0, 50, 0, 100) is False
    # custom 8 ct ceiling
    assert decide_grid_charge(6.0, 8.0, 50, 0, 100) is True
    # stop at the max target
    assert decide_grid_charge(-2.0, 0.0, 90, 0, 90) is False
    # reserve floor: top up from grid at any price below soc_min
    assert decide_grid_charge(25.0, 0.0, 10, 20, 100) is True
    # unknown SoC -> never
    assert decide_grid_charge(-5.0, 0.0, None, 0, 100) is False


def test_engine_grid_charges_battery_at_negative_price():
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((entity, data))

    s = Store()
    s._config = {"battery": [{"id": "b1", "name": "Akku", "power": "sensor.bp",
                              "soc": "sensor.soc", "charge_power": "number.bc"}]}
    s._settings["tariff"] = {"mode": "dynamic", "price_entity": "sensor.price", "charge_max_ct": 0.0}
    s._settings["strategy_loads"] = {"battery:b1": {"self_consumption": True, "tariff_shift": True,
                                                    "max_w": 5000, "w_per_unit": 1, "grid_soc_max": 90}}
    s._live_by_id = {"sensor.price": {"state": "-3", "attributes": {"unit_of_measurement": "ct/kWh"}},
                     "sensor.soc": {"state": "50"}, "number.bc": {"state": "0"}}
    asyncio.run(ControlEngine(s, cs)._modulate(0.0))
    assert ("number.bc", {"value": 5000.0}) in calls   # grid-charged to full power


def test_engine_no_grid_charge_at_positive_price():
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((entity, data))

    s = Store()
    s._config = {"battery": [{"id": "b1", "name": "Akku", "power": "sensor.bp",
                              "soc": "sensor.soc", "charge_power": "number.bc"}]}
    s._settings["tariff"] = {"mode": "dynamic", "price_entity": "sensor.price", "charge_max_ct": 0.0}
    s._settings["strategy_loads"] = {"battery:b1": {"self_consumption": True, "tariff_shift": True,
                                                    "max_w": 5000, "w_per_unit": 1}}
    s._live_by_id = {"sensor.price": {"state": "12", "attributes": {"unit_of_measurement": "ct/kWh"}},
                     "sensor.soc": {"state": "50"}, "number.bc": {"state": "0"}}
    asyncio.run(ControlEngine(s, cs)._modulate(0.0))
    assert ("number.bc", {"value": 5000.0}) not in calls   # no grid-charge at 12 ct


def test_grid_discharge_threshold_and_reserve():
    assert decide_grid_discharge(40.0, 30.0, 60, 20) is True
    assert decide_grid_discharge(25.0, 30.0, 60, 20) is False   # below threshold
    assert decide_grid_discharge(40.0, 30.0, 20, 20) is False   # at reserve floor
    assert decide_grid_discharge(40.0, 0.0, 60, 20) is False    # disabled (0)
    assert decide_grid_discharge(40.0, 30.0, None, 20) is False  # unknown SoC


def test_battery_tariff_mode_precedence():
    assert battery_tariff_mode(-2.0, 0.0, 30.0, 50, 0, 100) == "charge"     # cheap/negative
    assert battery_tariff_mode(40.0, 0.0, 30.0, 50, 20, 100) == "discharge"  # expensive
    assert battery_tariff_mode(15.0, 0.0, 30.0, 50, 20, 100) is None         # mid -> surplus


def test_engine_force_discharges_battery_at_expensive_price():
    calls = []

    async def cs(domain, service, entity, data=None):
        calls.append((entity, data))

    s = Store()
    s._config = {"battery": [{"id": "b1", "name": "Akku", "power": "sensor.bp", "soc": "sensor.soc",
                              "charge_power": "number.bc", "discharge_power": "number.bd"}]}
    s._settings["tariff"] = {"mode": "dynamic", "price_entity": "sensor.price",
                             "charge_max_ct": 0.0, "discharge_min_ct": 35.0}
    s._settings["strategy_loads"] = {"battery:b1": {"self_consumption": True, "tariff_shift": True,
                                                    "max_w": 5000, "w_per_unit": 1, "grid_soc_min": 20}}
    s._live_by_id = {"sensor.price": {"state": "42", "attributes": {"unit_of_measurement": "ct/kWh"}},
                     "sensor.soc": {"state": "60"}, "number.bc": {"state": "0"}, "number.bd": {"state": "0"}}
    asyncio.run(ControlEngine(s, cs)._modulate(0.0))
    assert ("number.bd", {"value": 5000.0}) in calls   # forced discharge to full power


def test_battery_stop_uses_soc_and_threshold_guard():
    s = Store()
    s._config = {"battery": [{"id": "b1", "name": "Akku", "power": "sensor.bp",
                              "soc": "sensor.soc", "charge_power": "number.bc"}]}
    s._live_by_id = {"sensor.soc": {"state": "82"}}
    d = {x["key"]: x for x in s.strategy_devices()}["battery:b1"]
    assert d["cfg"]["limit_entity"] == "sensor.soc"   # SoC auto-filled as stop signal
    assert d["satisfied"] is False                     # threshold 0 -> stop disabled
    s._settings["strategy_loads"] = {"battery:b1": {"limit_max": 80}}
    d2 = {x["key"]: x for x in s.strategy_devices()}["battery:b1"]
    assert d2["satisfied"] is True                      # 82 >= 80


def test_tariff_setpoint_to_max_when_cheap():
    sp = {"entity": "number.x", "mode": "setpoint", "cur_unit": 0.0,
          "max_unit": 16.0, "satisfied": False}
    out = decide_tariff_actions(0, True, [sp])
    assert out == [("number.x", 16.0, "günstiger Tarif")]
    out = decide_tariff_actions(0, False, [{**sp, "cur_unit": 16.0}])
    assert out == [("number.x", 0.0, "Tarif/Ziel")]


def test_switch_decision_on_and_off():
    c = ConsumerDecision("switch.hz", "switch", priority=5, nominal_power_w=800,
                         pv_threshold_w=0, is_on=False, last_on=0, last_off=0,
                         starts_today=0, max_starts=0, min_runtime_s=0, min_off_s=0)
    on = decide_action(0, 1000, [c])
    assert on and on[1] == "on"
    c2 = ConsumerDecision("switch.hz", "switch", priority=5, nominal_power_w=800,
                          pv_threshold_w=0, is_on=True, last_on=0, last_off=0,
                          starts_today=0, max_starts=0, min_runtime_s=0, min_off_s=0)
    off = decide_action(1000, -500, [c2])
    assert off and off[1] == "off"


def test_non_interruptible_load_not_shed_on_import():
    # A running non-interruptible load keeps running on grid import; an
    # interruptible one of equal priority is shed instead.
    ni = _cd("switch.wm", is_on=True, interruptible=False, nominal_power_w=2000)
    inter = _cd("switch.hz", is_on=True, interruptible=True, nominal_power_w=2000)
    out = decide_action(1000, -800, [ni, inter])
    assert out == ("switch.hz", "off", "Netzbezug 800 W, schalte ab")
    # With only the non-interruptible load running, nothing is shed.
    assert decide_action(1000, -800, [ni]) is None


def test_non_interruptible_still_shed_when_satisfied():
    # Target reached overrides interruptibility (the run is done).
    ni_done = _cd("switch.wm", is_on=True, interruptible=False, satisfied=True, nominal_power_w=2000)
    assert decide_action(1000, -800, [ni_done]) == ("switch.wm", "off", "Ziel erreicht")


def test_tariff_keeps_non_interruptible_running_until_satisfied():
    base = {"entity": "switch.wm", "mode": "switch", "is_on": True, "last_on": 0, "last_off": 0,
            "min_runtime_s": 0, "min_off_s": 0, "interruptible": False}
    # tariff no longer cheap, not satisfied -> non-interruptible load stays on
    assert decide_tariff_actions(1000, False, [{**base, "satisfied": False}]) == []
    # satisfied -> turned off (done)
    off = decide_tariff_actions(1000, False, [{**base, "satisfied": True}])
    assert off == [("switch.wm", "off", "Ziel erreicht")]
    # an interruptible load is turned off when not cheap
    assert decide_tariff_actions(1000, False, [{**base, "interruptible": True, "satisfied": False}]) \
        == [("switch.wm", "off", "Tarif nicht günstig")]


def test_deadline_forces_start_without_surplus():
    # 10:30 now, deadline 10:00, no surplus -> forced on within the window
    c = _cd("switch.wm", is_on=False, deadline_min=600, now_min=630)
    assert decide_action(0, 0, [c]) == ("switch.wm", "on", "Deadline – Start erzwungen")


def test_deadline_not_forced_before_or_after_window():
    before = _cd("switch.wm", is_on=False, deadline_min=600, now_min=540)
    assert decide_action(0, 0, [before]) is None          # before the deadline
    after = _cd("switch.wm", is_on=False, deadline_min=600, now_min=800)
    assert decide_action(0, 0, [after]) is None            # past the force window


def test_deadline_skipped_when_satisfied():
    c = _cd("switch.wm", is_on=False, satisfied=True, deadline_min=600, now_min=630)
    assert decide_action(0, 0, [c]) is None


def test_satisfied_load_shed_first_and_not_turned_on():
    on_sat = _cd("switch.wb", is_on=True, satisfied=True, nominal_power_w=2000)
    out = decide_action(100, 3000, [on_sat])   # big surplus, but target reached -> off
    assert out == ("switch.wb", "off", "Ziel erreicht")
    off_sat = _cd("switch.wb", is_on=False, satisfied=True)
    assert decide_action(0, 3000, [off_sat]) is None  # not turned on


def test_modulating_battery_device_and_satisfied_limit():
    s = Store()
    s._config = {"battery": [{"id": "b1", "name": "Akku", "power": "sensor.bp",
                              "charge_power": "number.bcharge"}]}
    devs = {d["key"]: d for d in s.controllable_devices()}
    assert "battery:b1" in devs
    assert devs["battery:b1"]["control_mode"] == "setpoint"
    assert devs["battery:b1"]["setpoint"] == "number.bcharge"
    # stop limit: vehicle SoC >= 80
    s._live_by_id = {"sensor.soc": {"state": "82"}}
    assert s._device_satisfied("k1", {"limit_entity": "sensor.soc", "limit_max": 80}) is True
    assert s._device_satisfied("k2", {"limit_entity": "sensor.soc", "limit_max": 90}) is False
    assert s._device_satisfied("k3", {}) is False
    # hysteresis/deadband: stop at limit, release only below (limit - hyst)
    cfg = {"limit_entity": "sensor.t", "limit_max": 65, "limit_hyst": 5}
    s._live_by_id = {"sensor.t": {"state": "66"}}
    assert s._device_satisfied("h", cfg) is True          # triggers at >= 65
    s._live_by_id = {"sensor.t": {"state": "62"}}
    assert s._device_satisfied("h", cfg) is True          # latched (>= 60)
    s._live_by_id = {"sensor.t": {"state": "59"}}
    assert s._device_satisfied("h", cfg) is False         # releases (< 60)
    s._live_by_id = {"sensor.t": {"state": "64"}}
    assert s._device_satisfied("h", cfg) is False         # stays off until >= 65


def test_modulation_surplus_smoothing_rides_through_spikes():
    # EWMA smoothing: a single deep-import spike must not yank the smoothed
    # surplus to the spike value (which would shed a modulating load to 0).
    s = Store()
    s._settings["modulation_smoothing_s"] = 60.0   # tau = 60 s
    eng = ControlEngine(s, None)  # type: ignore[arg-type]
    assert eng._smooth_surplus(0.0, 3000.0) == 3000.0      # first call seeds
    # 60 s later a −2000 W spike: alpha = 60/(60+60) = 0.5
    assert eng._smooth_surplus(60.0, -2000.0) == 500.0     # stays positive, no full shed
    # disabled -> passthrough
    s2 = Store()
    s2._settings["modulation_smoothing_s"] = 0.0
    eng2 = ControlEngine(s2, None)  # type: ignore[arg-type]
    assert eng2._smooth_surplus(0.0, 3000.0) == 3000.0
    assert eng2._smooth_surplus(60.0, -2000.0) == -2000.0


def test_decide_modulation_allow_shed_holds_load_on_unconfirmed_import():
    mods = [{"entity": "number.hz", "domain": "number", "cur_unit": 3000.0,
             "cur_w": 3000.0, "wpu": 1.0, "min_w": 0.0, "max_w": 3600.0,
             "priority": 5, "is_batt": False, "batt_mode": None, "discharge": ""}]
    # deep (possibly glitchy) import: with shedding allowed the load drops to 0 ...
    assert decide_modulation(-5000.0, mods, allow_shed=True)[0]["power_w"] == 0.0
    # ... but while import is unconfirmed (export guard / debounce) it is held.
    assert decide_modulation(-5000.0, mods, allow_shed=False)[0]["power_w"] == 3000.0
    # surplus available: load rises regardless of the flag (guard only blocks cuts)
    assert decide_modulation(600.0, mods, allow_shed=False)[0]["power_w"] == 3600.0


def test_decide_modulation_hold_freezes_setpoint():
    mods = [{"entity": "number.hz", "domain": "number", "cur_unit": 2200.0,
             "cur_w": 2200.0, "wpu": 1.0, "min_w": 0.0, "max_w": 3600.0,
             "priority": 5, "is_batt": False, "batt_mode": None, "discharge": ""}]
    # stale/unreliable data -> hold at the current setpoint regardless of surplus
    assert decide_modulation(5000.0, mods, hold=True)[0]["power_w"] == 2200.0
    assert decide_modulation(-9000.0, mods, hold=True)[0]["power_w"] == 2200.0


def test_build_image_snaps_smoothed_down_on_confirmed_deficit():
    # A sustained import must not "ride" the (lagging) smoothing up — once the
    # deficit is confirmed the modulation regulates on the actual (negative) value
    # so loads shed instead of importing.
    s = Store()
    s._settings["control_enabled"] = True
    s._settings["modulation_smoothing_s"] = 120.0
    s._config = {"grid": {"power": "sensor.g"},
                 "battery": [{"id": "b1", "name": "Bat", "power": "sensor.b"}]}
    eng = ControlEngine(s, None)  # type: ignore[arg-type]

    def bal(g):
        s._live_by_id = {"sensor.g": {"state": str(g)}, "sensor.b": {"state": "0"}}
    bal(-3000); img1 = eng.build_image(0.0)          # export -> smoothed positive
    assert img1.surplus_smoothed > 0
    bal(5000); eng.build_image(10.0)                 # import, streak 1 -> still smoothed
    bal(5000); img3 = eng.build_image(20.0)          # streak 2 -> confirmed deficit
    assert img3.allow_mod_shed is True
    assert img3.surplus_smoothed == img3.surplus_signed < 0   # snapped to the real deficit
