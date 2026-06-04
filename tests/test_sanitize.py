"""Tests for the config sanitiser and old->new migration (Store methods)."""

from __future__ import annotations

from smart_energy_agent.store import Store


def test_sanitize_assigns_ids_and_drops_unknown() -> None:
    s = Store()
    raw = {
        "grid": {"power": "sensor.grid", "invert": True, "bogus": "x"},
        "pv": [{"name": "Dach", "powers": [{"name": "MPPT1", "entity": "sensor.s1"}],
                "junk": 1}],
        "heat_pump": [{"name": "WP", "powers": [{"entity": "sensor.hp"}],
                       "control": {"mode": "setpoint", "setpoint": "number.x"}}],
        "unknown_kind": [{"x": 1}],
    }
    out = s._sanitize_config(raw)
    assert out["grid"] == {"power": "sensor.grid", "import_power": "", "export_power": "", "invert": True}
    assert "bogus" not in out["grid"]
    pv = out["pv"][0]
    assert pv["id"] and pv["name"] == "Dach" and "junk" not in pv
    assert pv["powers"][0]["id"] and pv["powers"][0]["entity"] == "sensor.s1"
    hp = out["heat_pump"][0]
    assert hp["control"] == {"mode": "setpoint", "switch": "", "setpoint": "number.x"}
    assert "unknown_kind" not in out


def test_sanitize_rejects_bad_control_mode() -> None:
    s = Store()
    out = s._sanitize_config({"consumers": [{"name": "X", "control": {"mode": "hack"}}]})
    assert out["consumers"][0]["control"]["mode"] == ""


def test_migrate_old_flat_config() -> None:
    s = Store()
    old = {
        "pv": {"power": ["sensor.pv"], "energy_today": "sensor.pv_e"},
        "battery": {"power": "sensor.bat", "soc": "sensor.soc", "invert": True},
        "heat_pump": {"power": ["sensor.hp1", "sensor.hp2"]},
        "ev_charger": {"power": "sensor.wb", "energy": "sensor.wb_e"},
        "consumers": [{"id": "c1", "name": "Allgemein", "power": ["sensor.a"]}],
    }
    migrated = s._migrate_config(old)
    out = s._sanitize_config(migrated)
    assert out["pv"][0]["powers"][0]["entity"] == "sensor.pv"
    assert out["pv"][0]["energy"][0]["entity"] == "sensor.pv_e"
    assert out["battery"][0]["soc"] == "sensor.soc" and out["battery"][0]["invert"] is True
    assert {p["entity"] for p in out["heat_pump"][0]["powers"]} == {"sensor.hp1", "sensor.hp2"}
    assert out["ev_charger"][0]["powers"][0]["entity"] == "sensor.wb"
    assert out["consumers"][0]["name"] == "Allgemein"
    assert out["consumers"][0]["powers"][0]["entity"] == "sensor.a"


def test_config_entity_ids_recursive() -> None:
    s = Store()
    s._config = s._sanitize_config({
        "grid": {"power": "sensor.grid"},
        "heat_pump": [{"name": "WP", "powers": [{"entity": "sensor.hp"}],
                       "circuits": [{"name": "HK1", "temp": "sensor.t", "setpoint": "number.sp"}],
                       "control": {"mode": "switch", "switch": "switch.hp"}}],
    })
    ids = s.config_entity_ids()
    assert {"sensor.grid", "sensor.hp", "sensor.t", "number.sp", "switch.hp"} <= ids
