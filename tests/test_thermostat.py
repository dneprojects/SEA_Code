"""Tests for the absence-setback thermostat engine."""

from __future__ import annotations

import asyncio

from smart_energy_agent.thermostat import ThermostatEngine


class FakeStore:
    def __init__(self, enabled, present, thermostats, live, frost=7.0):
        self._sb = {"enabled": enabled, "frost_c": frost}
        self._present = present
        self._th = thermostats
        self._live = live

    def setback(self):
        return dict(self._sb)

    def presence_is_home(self):
        return self._present

    def thermostats(self):
        return list(self._th)

    def live_state(self, eid):
        return self._live.get(eid, {})


def test_decide_present_absent_and_frost_guard():
    eng = ThermostatEngine(None, None)
    th = {"comfort_c": 21, "eco_c": 17}
    assert eng.decide(th, True, 7) == 21
    assert eng.decide(th, False, 7) == 17
    assert eng.decide({"comfort_c": 5, "eco_c": 3}, False, 7) == 7  # frost guard


def test_run_once_sets_eco_when_away():
    calls = []

    async def cs(domain, service, eid, data=None):
        calls.append((domain, service, eid, data))

    store = FakeStore(True, False, [{"climate": "climate.wz", "comfort_c": 21, "eco_c": 17}],
                      {"climate.wz": {"attributes": {"temperature": 21}}})
    asyncio.run(ThermostatEngine(store, cs).run_once())
    assert calls == [("climate", "set_temperature", "climate.wz", {"temperature": 17.0})]


def test_run_once_noop_when_disabled_or_presence_unknown():
    calls = []

    async def cs(*a, **k):
        calls.append(a)

    th = [{"climate": "c", "comfort_c": 21, "eco_c": 17}]
    asyncio.run(ThermostatEngine(FakeStore(False, False, th, {}), cs).run_once())
    asyncio.run(ThermostatEngine(FakeStore(True, None, th, {}), cs).run_once())
    assert calls == []


def test_run_once_skips_when_already_at_target():
    calls = []

    async def cs(domain, service, eid, data=None):
        calls.append(eid)

    store = FakeStore(True, False, [{"climate": "c", "comfort_c": 21, "eco_c": 17}],
                      {"c": {"attributes": {"temperature": 17}}})
    asyncio.run(ThermostatEngine(store, cs).run_once())
    assert calls == []
