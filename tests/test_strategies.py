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
    assert "dynamischer Tarif mit Preis-Entität" in ov["tariff_shift"]["missing"]


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
    assert ov["tariff_shift"]["engine_ready"] is False  # execution still to come
