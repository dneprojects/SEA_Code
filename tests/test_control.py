"""Tests for the PV-surplus control: switch decision + modulating allocation."""

from __future__ import annotations

from smart_energy_agent.control import ConsumerDecision, decide_action, decide_modulation


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
