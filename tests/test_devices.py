"""Tests for the Device channel adapter (entity/unit/sign conventions)."""

from __future__ import annotations

from smart_energy_agent.devices import Device, devices


class FakeStore:
    def __init__(self, states=None, runtimes=None, truthy=(), devs=None):
        self._s = states or {}
        self._rt = runtimes or {}
        self._truthy = set(truthy)
        self._devs = devs if devs is not None else [{"key": "x:1", "cfg": {}}]

    def live_state(self, eid):
        return self._s.get(eid, {})

    def entity_truthy(self, eid):
        return eid in self._truthy

    def runtime(self, eid):
        return self._rt.get(eid, {})

    def strategy_devices(self):
        return self._devs


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


def test_category_and_capabilities():
    bat = _dev({"kind": "battery", "setpoint": "number.bc", "discharge": "number.bd",
                "soc": "sensor.soc", "cfg": {}})
    assert bat.category == "ess" and bat.is_ess
    assert bat.can_modulate and bat.can_force_discharge and bat.has_soc
    wb = _dev({"kind": "wallbox", "control_mode": "setpoint", "setpoint": "number.wb", "cfg": {}})
    assert wb.category == "evcs" and wb.is_evcs and not wb.is_ess
    heat = _dev({"kind": "water_heater", "control_mode": "setpoint", "setpoint": "number.hz", "cfg": {}})
    assert heat.category == "setpoint_load" and heat.can_modulate and not heat.can_switch
    pump = _dev({"kind": "pump", "control_mode": "switch", "switch": "switch.p", "cfg": {}})
    assert pump.category == "switch_load" and pump.can_switch and not pump.can_modulate


def test_configured_zero_is_kept_and_wpu_guarded():
    d = _dev({"cfg": {"grid_soc_max": 0, "min_w": 0}})
    assert d.grid_soc_max == 0.0          # configured 0 kept (not the 100 default)
    assert d.min_w == 0.0
    assert _dev({"cfg": {"w_per_unit": 0}}).wpu == 1.0    # 0 guarded -> 1 (no div/0)


def test_actuator_bounds():
    d = _dev({"kind": "battery", "setpoint": "number.bc", "discharge": "number.bd",
              "cfg": {"max_w": 3450, "w_per_unit": 690}})
    b = d.actuator_bounds()
    assert b["number.bc"] == (0.0, 5.0) and b["number.bd"] == (0.0, 5.0)   # 3450/690 = 5
    open_hi = _dev({"control_mode": "setpoint", "setpoint": "number.x", "cfg": {}}).actuator_bounds()
    lo, hi = open_hi["number.x"]
    assert lo == 0.0 and hi == float("inf")               # no max_w -> open upper bound


def test_module_helpers_filter_and_merge_bounds():
    from smart_energy_agent.devices import actuator_bounds, ess_devices, modulating_loads
    store = FakeStore(devs=[
        {"key": "b", "kind": "battery", "setpoint": "number.bc",
         "cfg": {"max_w": 3450, "w_per_unit": 690}},
        {"key": "h", "kind": "water_heater", "control_mode": "setpoint", "setpoint": "number.hz",
         "cfg": {"max_w": 3000}},
        {"key": "p", "kind": "pump", "control_mode": "switch", "switch": "switch.p", "cfg": {}},
    ])
    assert [d.key for d in ess_devices(store)] == ["b"]
    assert [d.key for d in modulating_loads(store)] == ["h"]
    bounds = actuator_bounds(store)
    assert bounds["number.bc"] == (0.0, 5.0) and bounds["number.hz"] == (0.0, 3000.0)
