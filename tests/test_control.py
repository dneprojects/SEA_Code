"""Tests for the PV-surplus control: switch decision + modulating allocation."""

from __future__ import annotations

from smart_energy_agent.control import (
    ConsumerDecision, decide_action, decide_modulation, decide_tariff_actions,
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
