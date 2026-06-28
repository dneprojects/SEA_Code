"""Tests for the Device channel adapter (entity/unit/sign conventions)."""

from __future__ import annotations

from smart_energy_agent.devices import Device, devices


class FakeStore:
    def __init__(self, states=None, runtimes=None, truthy=()):
        self._s = states or {}
        self._rt = runtimes or {}
        self._truthy = set(truthy)

    def live_state(self, eid):
        return self._s.get(eid, {})

    def entity_truthy(self, eid):
        return eid in self._truthy

    def runtime(self, eid):
        return self._rt.get(eid, {})

    def strategy_devices(self):
        return [{"key": "x:1", "cfg": {}}]


def _dev(d, **store_kw):
    return Device(FakeStore(**store_kw), d)


def test_defaults_and_config_numbers():
    d = _dev({"key": "water_heater:w1", "kind": "water_heater", "cfg": {}})
    assert d.priority == 5                      # default
    assert d.interruptible is True
    assert d.wpu == 1.0                          # 0/missing -> 1
    assert d.grid_soc_max == 100.0              # 0/missing -> 100 (not 0)
    assert d.grid_soc_min == 0.0
    assert d.max_w == 0.0 and d.min_w == 0.0
    assert not d.is_battery


def test_switch_on_states_and_runtime():
    d = _dev(
        {"control_mode": "switch", "switch": "switch.wm", "cfg": {"min_runtime_min": 2}},
        states={"switch.wm": {"state": "Heat"}},
        runtimes={"switch.wm": {"last_on": 5.0, "starts": 3}},
    )
    assert d.is_on is True                       # "heat" counts as on (case-insensitive)
    assert d.min_runtime_s == 120                # minutes -> seconds
    assert d.runtime.get("starts") == 3
    off = _dev({"control_mode": "switch", "switch": "switch.wm", "cfg": {}},
               states={"switch.wm": {"state": "off"}})
    assert off.is_on is False


def test_setpoint_cur_unit_and_power_and_soc():
    d = _dev(
        {"kind": "battery", "control_mode": "setpoint", "setpoint": "number.bc",
         "soc": "sensor.soc", "power_w": 812.0, "cfg": {"w_per_unit": 690}},
        states={"number.bc": {"state": "5.0"}, "sensor.soc": {"state": "64"}},
    )
    assert d.is_battery and d.cur_unit == 5.0
    assert d.power_w == 812.0 and d.soc == 64.0
    assert d.wpu == 690.0
    missing = _dev({"control_mode": "setpoint", "setpoint": "number.x", "cfg": {}})
    assert missing.cur_unit == 0.0 and missing.soc is None   # unavailable -> safe defaults


def test_ready_requires_entity_and_truthy():
    base = {"kind": "ev_charger", "cfg": {"ready_entity": "binary_sensor.plug"}}
    assert _dev(base, truthy=("binary_sensor.plug",)).ready is True
    assert _dev(base).ready is False                          # set but not truthy
    assert _dev({"kind": "ev_charger", "cfg": {}}).ready is False   # unset


def test_devices_wraps_each_strategy_device():
    out = devices(FakeStore())
    assert len(out) == 1 and isinstance(out[0], Device) and out[0].key == "x:1"
