"""Tests for strategy capability detection from the configured entities."""

from __future__ import annotations

from smart_energy_agent.strategies import overview


def _ov(config, settings, groups):
    return {s["key"]: s for s in overview(config, settings, groups)}


def test_self_consumption_available_and_active():
    config = {"pv": [{"powers": [{"entity": "sensor.pv"}]}], "grid": {"power": "sensor.grid"},
              "water_heater": [{"control": {"mode": "switch", "switch": "switch.hz"}}]}
    ov = _ov(config, {"control_enabled": True, "tariff": {}}, [])
    assert ov["self_consumption"]["available"] is True
    assert ov["self_consumption"]["active"] is True
    assert ov["tariff_shift"]["available"] is False
    assert "Preisquelle (dynamischer Tarif oder HT/NT-Fenster)" in ov["tariff_shift"]["missing"]


def test_everything_missing_when_empty():
    ov = _ov({}, {"tariff": {}}, [])
    assert ov["self_consumption"]["available"] is False
    assert ov["setback"]["available"] is False
    assert ov["battery_opt"]["available"] is False  # needs a control entity (not yet)


def test_setback_available_with_group():
    groups = [{"persons": ["person.a"], "thermostats": [{"climate": "climate.x"}]}]
    ov = _ov({}, {"setback": {"enabled": True}, "tariff": {}}, groups)
    assert ov["setback"]["available"] is True and ov["setback"]["active"] is True


def test_tariff_shift_available_with_dynamic_price_and_load():
    config = {"consumers": [{"control": {"mode": "switch", "switch": "switch.x"}}]}
    settings = {"tariff": {"mode": "dynamic", "price_entity": "sensor.price"}}
    ov = _ov(config, settings, [])
    assert ov["tariff_shift"]["available"] is True
    assert ov["tariff_shift"]["engine_ready"] is True
    assert ov["tariff_shift"]["active"] is False  # no master switch / no opted load


def test_tariff_shift_available_with_ht_nt_window():
    config = {"consumers": [{"control": {"mode": "switch", "switch": "switch.x"}}]}
    settings = {"tariff": {"mode": "ht_nt", "nt_start": "22:00", "nt_end": "06:00"}}
    ov = _ov(config, settings, [])
    assert ov["tariff_shift"]["available"] is True


def test_tariff_shift_active_when_opted_and_master_on():
    config = {"consumers": [{"id": "c1", "control": {"mode": "switch", "switch": "switch.x"}}]}
    settings = {"control_enabled": True,
                "tariff": {"mode": "dynamic", "price_entity": "sensor.price"},
                "strategy_loads": {"consumers:c1": {"tariff_shift": True}}}
    ov = _ov(config, settings, [])
    assert ov["tariff_shift"]["active"] is True


def test_ev_surplus_available_and_active():
    config = {"pv": [{"powers": [{"entity": "sensor.pv"}]}], "grid": {"power": "sensor.grid"},
              "ev_charger": [{"id": "wb1", "control": {"mode": "setpoint", "setpoint": "number.amp"}}]}
    settings = {"control_enabled": True, "tariff": {},
                "strategy_loads": {"ev_charger:wb1": {"self_consumption": True}}}
    ov = _ov(config, settings, [])
    assert ov["ev_surplus"]["available"] is True
    assert ov["ev_surplus"]["engine_ready"] is True
    assert ov["ev_surplus"]["active"] is True


def test_ev_surplus_needs_setpoint_wallbox():
    # A switch-only wallbox cannot follow the surplus -> not available.
    config = {"pv": [{"powers": [{"entity": "sensor.pv"}]}], "grid": {"power": "sensor.grid"},
              "ev_charger": [{"id": "wb1", "control": {"mode": "switch", "switch": "switch.wb"}}]}
    ov = _ov(config, {"tariff": {}}, [])
    assert ov["ev_surplus"]["available"] is False
