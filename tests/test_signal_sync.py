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
    # interval 1 s but current value ~5 s old -> stale (> 3× interval)
    s._samples["sensor.g"] = _dq([(now - 7, 1.0), (now - 6, 1.0), (now - 5, 1.0)])
    assert s.signal_stale(["sensor.g"]) is True
    # fresh again
    s._samples["sensor.g"] = _dq([(now - 2, 1.0), (now - 1, 1.0), (now, 1.0)])
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
