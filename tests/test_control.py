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
    asyncio.run(ControlEngine(s, cs).run_once(0.0))
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
    assert not any(e == "number.hz" for e, _ in calls)


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
    assert not any(e == "number.hz" for e, _ in below)

    s2 = _heater_store(grid_w=0, batt_w=1700, heater_setpoint_w=0,
                       loads_first=True, pv_w=2000, batt_soc=60, min_soc=60)
    asyncio.run(ControlEngine(s2, cs_above).run_once(0.0))
    assert ("number.hz", {"value": 1700.0}) in above


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
    assert s._device_satisfied({"limit_entity": "sensor.soc", "limit_max": 80}) is True
    assert s._device_satisfied({"limit_entity": "sensor.soc", "limit_max": 90}) is False
    assert s._device_satisfied({}) is False
