"""Tests for the instance-based config energy balance (wizard output)."""

from __future__ import annotations

from smart_energy_agent.aggregator import balance_from_config


def _p(value, unit="W"):
    return {"state": str(value), "attributes": {"unit_of_measurement": unit}}


def _pw(*entities):
    return [{"id": "", "name": "P", "entity": e} for e in entities]


def test_pv_sum_grid_sign_and_house_derivation() -> None:
    config = {
        "pv": [{"id": "pv1", "name": "PV", "powers": _pw("s.pv1", "s.pv2")}],
        "grid": {"power": "s.grid"},
        "heat_pump": [{"id": "h1", "name": "WP", "powers": _pw("s.hp")}],
    }
    live = {"s.pv1": _p(1000), "s.pv2": _p(2.0, "kW"), "s.grid": _p(-500), "s.hp": _p(800)}
    bal = balance_from_config(config, live)
    assert bal["pv_w"] == 3000.0
    assert bal["grid_w"] == -500.0
    assert bal["house_load_w"] == 2500.0   # pv + grid - battery(0)
    assert bal["surplus_w"] == 500.0
    load = next(x for x in bal["loads"] if x["name"] == "WP")
    assert load["power_w"] == 800.0


def test_battery_charge_reduces_surplus() -> None:
    config = {
        "pv": [{"id": "p", "name": "PV", "powers": _pw("s.pv")}],
        "grid": {"power": "s.grid"},
        "battery": [{"id": "b", "name": "Bat", "power": "s.bat", "soc": "s.soc"}],
    }
    live = {"s.pv": _p(3000), "s.grid": _p(-500), "s.bat": _p(1000),
            "s.soc": {"state": "82", "attributes": {"unit_of_measurement": "%"}}}
    bal = balance_from_config(config, live)
    assert bal["battery_w"] == 1000.0
    assert bal["battery_soc"] == 82.0
    assert bal["house_load_w"] == 1500.0   # 3000 - 500 - 1000
    assert bal["surplus_w"] == 1500.0


def test_battery_invert_per_instance() -> None:
    config = {"battery": [{"id": "b", "name": "Bat", "power": "s.bat", "invert": True}]}
    bal = balance_from_config(config, {"s.bat": _p(-700)})
    assert bal["battery_w"] == 700.0


def test_grid_invert_flag() -> None:
    config = {"pv": [{"id": "p", "name": "PV", "powers": _pw("s.pv")}],
              "grid": {"power": "s.grid", "invert": True}}
    live = {"s.pv": _p(1000), "s.grid": _p(-500)}
    bal = balance_from_config(config, live)
    assert bal["grid_w"] == 500.0
    assert bal["house_load_w"] == 1500.0
    assert bal["surplus_w"] == 0.0   # surplus is clamped to >= 0


def test_import_export_pair() -> None:
    config = {"grid": {"import_power": "s.imp", "export_power": "s.exp"}}
    bal = balance_from_config(config, {"s.imp": _p(300), "s.exp": _p(0)})
    assert bal["grid_w"] == 300.0


def test_load_parts_for_expandable_node() -> None:
    config = {"heat_pump": [{"id": "h1", "name": "WP",
                             "powers": [{"id": "", "name": "WP1", "entity": "s.a"},
                                        {"id": "", "name": "Zusatzheizer", "entity": "s.b"}]}]}
    live = {"s.a": _p(300), "s.b": _p(200)}
    bal = balance_from_config(config, live)
    load = next(x for x in bal["loads"] if x["name"] == "WP")
    assert load["power_w"] == 500.0
    assert [p["name"] for p in load["parts"]] == ["WP1", "Zusatzheizer"]
    assert [p["power_w"] for p in load["parts"]] == [300.0, 200.0]


def test_named_consumers_as_loads() -> None:
    config = {"consumers": [
        {"id": "c1", "name": "Allgemein", "powers": _pw("s.a")},
        {"id": "c2", "name": "Wohnungen", "powers": _pw("s.w1", "s.w2")},
    ]}
    live = {"s.a": _p(46), "s.w1": _p(100), "s.w2": _p(147)}
    bal = balance_from_config(config, live)
    loads = {x["name"]: x["power_w"] for x in bal["loads"]}
    assert loads["Allgemein"] == 46.0
    assert loads["Wohnungen"] == 247.0


def test_unavailable_load_still_listed() -> None:
    # Heat-pump power entity configured but no live value (e.g. unavailable):
    # the load must still appear so the diagram keeps the device.
    config = {"heat_pump": [{"id": "h1", "name": "WP", "powers": _pw("s.hp")}]}
    bal = balance_from_config(config, {})  # no live data
    load = next(x for x in bal["loads"] if x["name"] == "WP")
    assert load["configured"] is True
    assert load["power_w"] is None
    assert load["parts"][0]["power_w"] is None


def test_missing_live_values_are_safe() -> None:
    config = {"pv": [{"id": "p", "name": "PV", "powers": _pw("s.pv")}],
              "grid": {"power": "s.grid"}}
    bal = balance_from_config(config, {})
    assert bal["pv_w"] == 0.0
    assert bal["grid_w"] == 0.0
    assert bal["loads"] == []
