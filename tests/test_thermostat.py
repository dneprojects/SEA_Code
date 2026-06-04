"""Tests for the grouped absence-setback engine + store helpers."""

from __future__ import annotations

import asyncio
import time

from smart_energy_agent.store import Store
from smart_energy_agent.thermostat import ThermostatEngine, minutes_to_time


def test_target_present_absent_frost_and_preheat():
    eng = ThermostatEngine(None, None)
    th = {"comfort_c": 21, "eco_c": 17, "reheat_k": 20}
    assert eng.target(th, True, 7, 18, None) == (21.0, False)
    assert eng.target(th, False, 7, 18, None) == (17.0, False)
    assert eng.target({"comfort_c": 5, "eco_c": 3}, False, 7, None, None) == (7.0, False)
    # lead = 20 min/K * (21-18) = 60 min; 30 <= 60 -> preheat to comfort
    assert eng.target(th, False, 7, 18, 30) == (21.0, True)
    assert eng.target(th, False, 7, 18, 90) == (17.0, False)


def test_minutes_to_time_wraps_next_day():
    tm = time.struct_time((2024, 1, 1, 6, 0, 0, 0, 1, -1))
    assert minutes_to_time(tm, "06:30") == 30
    assert minutes_to_time(tm, "05:30") == 1410  # -30 -> +1440


def test_sanitize_setback_ids_and_types():
    raw = {"enabled": 1, "frost_c": "6.5", "groups": [
        {"name": "EG", "persons": ["person.a", "", None], "comfort_time": "06:30",
         "thermostats": [{"name": "Bad", "climate": "climate.bad", "comfort_c": "22", "eco_c": "16"}]}]}
    out = Store._sanitize_setback(raw, Store._gen_id)
    assert out["enabled"] is True and out["frost_c"] == 6.5
    g = out["groups"][0]
    assert g["id"] and g["persons"] == ["person.a"] and g["comfort_time"] == "06:30"
    t = g["thermostats"][0]
    assert t["id"] and t["climate"] == "climate.bad" and t["comfort_c"] == 22.0 and t["eco_c"] == 16.0


def test_group_present_all_away_logic():
    s = Store()
    g = {"persons": ["person.a", "person.b"]}
    s._live_by_id = {"person.a": {"state": "home"}, "person.b": {"state": "not_home"}}
    assert s.group_present(g) is True                         # any home -> present
    s._live_by_id = {"person.a": {"state": "not_home"}, "person.b": {"state": "away"}}
    assert s.group_present(g) is False                        # all away
    s._live_by_id = {"person.a": {"state": "not_home"}}       # b unknown
    assert s.group_present(g) is None
    assert s.group_present({"persons": []}) is None


def test_run_once_sets_eco_when_group_away():
    calls = []

    async def cs(domain, service, eid, data=None):
        calls.append((eid, data))

    class FakeStore:
        def setback(self): return {"enabled": True, "frost_c": 7}
        def groups(self):
            return [{"id": "g1", "comfort_time": "",
                     "thermostats": [{"id": "t1", "climate": "climate.wz", "comfort_c": 21, "eco_c": 17}]}]
        def group_present(self, g): return False
        def live_state(self, eid):
            return {"attributes": {"temperature": 21, "current_temperature": 20}}
        def set_thermostat_reheat(self, *a): pass

    asyncio.run(ThermostatEngine(FakeStore(), cs).run_once())
    assert calls == [("climate.wz", {"temperature": 17.0})]


def test_run_once_noop_when_disabled():
    calls = []

    async def cs(*a, **k):
        calls.append(a)

    class FakeStore:
        def setback(self): return {"enabled": False}
        def groups(self): return []
    asyncio.run(ThermostatEngine(FakeStore(), cs).run_once())
    assert calls == []
