"""Tests for signal timing: update-rate, staleness gate, time-alignment."""

from __future__ import annotations

import time
from collections import deque

from smart_energy_agent.store import Store


def _dq(pairs):
    return deque(pairs, maxlen=40)


def test_signal_rate_interval_and_age():
    s = Store()
    now = time.monotonic()
    s._samples["sensor.g"] = _dq([(now - 4, 1.0), (now - 2, 2.0), (now, 3.0)])
    interval, age = s.signal_rate("sensor.g")
    assert interval == 2.0          # deltas 2, 2 -> median 2
    assert age is not None and age < 0.5
    assert s.signal_rate("sensor.unknown") == (None, None)


def test_signal_stale_gate():
    s = Store()
    now = time.monotonic()
    # genuinely dead feed: ~30 s cadence but silent well past the absolute floor
    s._samples["sensor.g"] = _dq([(now - 260, 1.0), (now - 230, 1.0), (now - 200, 1.0)])
    assert s.signal_stale(["sensor.g"]) is True
    # fresh again
    s._samples["sensor.g"] = _dq([(now - 2, 1.0), (now - 1, 1.0), (now, 1.0)])
    assert s.signal_stale(["sensor.g"]) is False
    # merely idle: a valid sensor with a longer-than-usual gap (still under the
    # floor) must NOT be flagged stale -> modulating loads stay live
    s._samples["sensor.g"] = _dq([(now - 130, 1.0), (now - 100, 1.0), (now - 80, 1.0)])
    assert s.signal_stale(["sensor.g"]) is False


def test_aligned_live_averages_fast_holds_slow():
    s = Store()
    s._config = {"grid": {"power": "sensor.g"},
                 "battery": [{"id": "b1", "power": "sensor.b"}]}
    s._live_by_id = {"sensor.g": {"state": 100.0}, "sensor.b": {"state": 2000.0}}
    now = time.monotonic()
    # grid: slow (10 s cadence) -> window = 10 s
    s._samples["sensor.g"] = _dq([(now - 10, 100.0), (now, 100.0)])
    # battery: fast, oscillating 0/2000 -> averages to 1000 over the window
    s._samples["sensor.b"] = _dq([(now - 3, 0.0), (now - 2, 2000.0),
                                  (now - 1, 0.0), (now, 2000.0)])
    assert s._align_window() == 10.0
    al = s._aligned_live()
    assert float(al["sensor.b"]["state"]) == 1000.0   # fast sensor time-averaged
    assert float(al["sensor.g"]["state"]) == 100.0     # slow sensor holds


def test_signal_sync_toggle_off_uses_raw():
    s = Store()
    s._settings["signal_sync"] = False
    assert s.signal_sync() is False


def test_categories_include_strategy_control_entities():
    # the device view must show control-relevant entities from strategy_loads
    # (stop/limit sensor, connected, SG-Ready), not only power/energy.
    s = Store()
    s._config = {"water_heater": [{"id": "w1", "name": "ELWA",
                                   "powers": [{"entity": "sensor.elwa_p"}],
                                   "control": {"mode": "setpoint", "setpoint": "number.elwa"}}]}
    s._settings["strategy_loads"] = {
        "water_heater:w1": {"self_consumption": True, "limit_entity": "sensor.elwa_temp",
                            "limit_max": 65}}
    s._live_by_id = {"sensor.elwa_temp": {"state": "49.4",
                                          "attributes": {"unit_of_measurement": "°C"}}}
    grp = next(g for g in s.categories_with_entities() if g["key"] == "water_heater:w1")
    ids = {e["entity_id"] for e in grp["entities"]}
    assert "sensor.elwa_temp" in ids            # the stop/limit temperature is listed


def test_history_entities_include_control_setpoint():
    # the commanded modulation setpoint (e.g. ELWA number.hz) must be plottable/
    # exportable, so the CSV export shows what the controller actually commanded.
    s = Store()
    s._config = {"water_heater": [{"id": "w1", "name": "ELWA",
                                   "powers": [{"entity": "sensor.elwa_p"}],
                                   "control": {"setpoint": "number.hz"}}]}
    grp = next(g for g in s.history_entities() if g["key"] == "water_heater:w1")
    ids = {e["entity_id"] for e in grp["entities"]}
    assert "number.hz" in ids                    # commanded setpoint is listed
    assert "sensor.elwa_p" in ids                # measured power still listed


def test_strategy_priorities_unique_and_sorted():
    # Each device gets a unique, contiguous priority (top-first); no hidden ties.
    s = Store(); s._save_settings = lambda: None
    s._settings["vehicles"] = []; s._settings["strategy_loads"] = {}
    s._config = {"water_heater": [{"id": "w1", "name": "ELWA", "control": {"setpoint": "number.e"}}],
                 "consumers": [{"id": "c1", "name": "A", "control": {"switch": "switch.a"}},
                               {"id": "c2", "name": "B", "control": {"switch": "switch.b"}}]}
    devs = s.strategy_devices()
    assert [d["cfg"]["priority"] for d in devs] == [3, 2, 1]     # unique, contiguous
    assert [d["name"] for d in devs] == ["ELWA", "A", "B"]        # config order, top first


def test_move_strategy_priority_swaps_order():
    s = Store(); s._save_settings = lambda: None
    s._settings["vehicles"] = []; s._settings["strategy_loads"] = {}
    s._config = {"consumers": [{"id": "c1", "name": "A", "control": {"switch": "switch.a"}},
                               {"id": "c2", "name": "B", "control": {"switch": "switch.b"}}]}
    assert [d["name"] for d in s.strategy_devices()] == ["A", "B"]
    s.move_strategy_priority("consumers:c2", "up")
    assert [d["name"] for d in s.strategy_devices()] == ["B", "A"]
    s.move_strategy_priority("consumers:c2", "down")
    assert [d["name"] for d in s.strategy_devices()] == ["A", "B"]


def test_strategies_operativ_from_control_trace():
    # "operativ" = active AND the strategy's controller emitted a command last cycle.
    s = Store(); s._save_settings = lambda: None
    s._config = {"pv": [{"powers": [{"entity": "sensor.pv"}]}], "grid": {"power": "sensor.g"},
                 "water_heater": [{"id": "w1", "control": {"switch": "switch.hz"}}]}
    s._settings["control_enabled"] = True
    s._control_trace = {"items": [{"entity": "switch.hz", "source": "pv_surplus_switch"}]}
    ov = {x["key"]: x for x in s.strategies_overview()}
    assert ov["self_consumption"]["operativ"] is True
    assert ov["peak_shaving"]["operativ"] is False
