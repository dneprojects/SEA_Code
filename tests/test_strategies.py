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
    assert ov["tariff_shift"]["available"] is False


def test_ev_and_battery_strategies_merged_into_self_consumption():
    # ev_surplus / battery_opt no longer exist as separate strategies.
    ov = _ov({}, {"tariff": {}}, [])
    assert "ev_surplus" not in ov
    assert "battery_opt" not in ov
    assert ov["self_consumption"]["name"] == "PV-Überschuss: Eigenverbrauch und Speicherung"
    assert ov["tariff_shift"]["name"] == "Dynamischer Tarif: Lastverschiebung"


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


def test_self_consumption_available_with_wallbox_or_battery():
    # The wallbox/battery now count as controllable loads for self_consumption.
    config = {"pv": [{"powers": [{"entity": "sensor.pv"}]}], "grid": {"power": "sensor.grid"},
              "ev_charger": [{"id": "wb1", "control": {"mode": "setpoint", "setpoint": "number.amp"}}]}
    ov = _ov(config, {"control_enabled": True, "tariff": {}}, [])
    assert ov["self_consumption"]["available"] is True
    assert ov["self_consumption"]["active"] is True
