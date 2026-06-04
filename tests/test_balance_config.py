"""Tests for the config-driven energy balance (wizard output)."""

from __future__ import annotations

from smart_energy_agent.aggregator import balance_from_config


def _p(value, unit="W"):
    return {"state": str(value), "attributes": {"unit_of_measurement": unit}}


def test_pv_sum_grid_sign_and_house_derivation() -> None:
    config = {
        "pv": {"power": ["s.pv1", "s.pv2"]},
        "grid": {"power": "s.grid"},
        "heat_pump": {"power": "s.hp"},
    }
    live = {
        "s.pv1": _p(1000),
        "s.pv2": _p(2.0, "kW"),   # kW -> 2000 W
        "s.grid": _p(-500),        # exporting 500 W
        "s.hp": _p(800),
    }
    bal = balance_from_config(config, live)
    assert bal["pv_w"] == 3000.0
    assert bal["grid_w"] == -500.0
    assert bal["house_load_w"] == 2500.0   # pv + grid
    assert bal["surplus_w"] == 500.0       # pv - house == -grid
    assert bal["heat_pump_w"] == 800.0
    assert bal["sources"]["pv"] == 2


def test_battery_charge_reduces_surplus() -> None:
    config = {
        "pv": {"power": ["s.pv"]},
        "grid": {"power": "s.grid"},
        "battery": {"power": "s.bat", "soc": "s.soc"},
    }
    live = {
        "s.pv": _p(3000),
        "s.grid": _p(-500),     # exporting 500
        "s.bat": _p(1000),      # charging 1000 (consumes)
        "s.soc": {"state": "82", "attributes": {"unit_of_measurement": "%"}},
    }
    bal = balance_from_config(config, live)
    assert bal["battery_w"] == 1000.0
    assert bal["battery_soc"] == 82.0
    # house = pv + grid - batt = 3000 - 500 - 1000 = 1500
    assert bal["house_load_w"] == 1500.0
    assert bal["surplus_w"] == 1500.0  # pv - house
    assert bal["sources"]["battery"] == 1


def test_heat_pump_multi_power_summed() -> None:
    config = {"pv": {"power": ["s.pv"]}, "grid": {"power": "s.grid"},
              "heat_pump": {"power": ["s.hp1", "s.hp2"]}}
    live = {"s.pv": _p(0), "s.grid": _p(0), "s.hp1": _p(300), "s.hp2": _p(200)}
    bal = balance_from_config(config, live)
    assert bal["heat_pump_w"] == 500.0


def test_grid_invert_flag() -> None:
    config = {"pv": {"power": ["s.pv"]}, "grid": {"power": "s.grid", "invert": True}}
    live = {"s.pv": _p(1000), "s.grid": _p(-500)}
    bal = balance_from_config(config, live)
    assert bal["grid_w"] == 500.0
    assert bal["house_load_w"] == 1500.0
    assert bal["surplus_w"] == -500.0


def test_import_export_pair() -> None:
    config = {"grid": {"import_power": "s.imp", "export_power": "s.exp"}}
    live = {"s.imp": _p(300), "s.exp": _p(0)}
    bal = balance_from_config(config, live)
    assert bal["grid_w"] == 300.0


def test_named_consumers_reported() -> None:
    config = {
        "pv": {"power": ["s.pv"]}, "grid": {"power": "s.grid"},
        "consumers": [
            {"id": "c1", "name": "Allgemein", "power": ["s.a"]},
            {"id": "c2", "name": "Wohnungen", "power": ["s.w1", "s.w2"]},
            {"id": "c3", "name": "Leer", "power": []},
        ],
    }
    live = {"s.pv": _p(0), "s.grid": _p(0), "s.a": _p(46),
            "s.w1": _p(100), "s.w2": _p(147)}
    bal = balance_from_config(config, live)
    cons = {c["name"]: c["power_w"] for c in bal["consumers"]}
    assert cons["Allgemein"] == 46.0
    assert cons["Wohnungen"] == 247.0   # summed
    assert cons["Leer"] is None         # no entities yet


def test_missing_live_values_are_safe() -> None:
    config = {"pv": {"power": ["s.pv"]}, "grid": {"power": "s.grid"}}
    bal = balance_from_config(config, {})  # no live data yet
    assert bal["pv_w"] == 0.0
    assert bal["grid_w"] == 0.0
    assert bal["heat_pump_w"] is None
