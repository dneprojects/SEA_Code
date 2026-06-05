"""Round-trip persistence: settings, strategy-loads and wizard config survive
a process restart (a fresh Store reloads them from disk)."""

from __future__ import annotations

from smart_energy_agent.store import Store


def test_settings_strategy_and_config_persist(tmp_path, monkeypatch):
    monkeypatch.setenv("SEA_HISTORY_DB", str(tmp_path / "h.db"))

    s = Store()
    s.set_full_config({"battery": [{"id": "b1", "name": "Akku", "power": "sensor.bp",
                                    "charge_power": "number.bchg"}]})
    s.set_strategy_load("battery:b1", {"self_consumption": True, "max_w": 5000, "w_per_unit": 1})
    s.set_strategy_load("ev_charger:wb1", {"self_consumption": True, "min_w": 4140,
                                           "ready_entity": "binary_sensor.plug"})
    s.set_settings({"tariff": {"mode": "dynamic", "price_entity": "sensor.price"},
                    "control_enabled": True})

    # A fresh Store reloads everything from the persisted JSON files.
    s2 = Store()
    assert s2.config()["battery"][0]["charge_power"] == "number.bchg"
    sl = s2.strategy_loads()
    assert sl["battery:b1"]["max_w"] == 5000.0
    assert sl["ev_charger:wb1"]["min_w"] == 4140.0
    assert sl["ev_charger:wb1"]["ready_entity"] == "binary_sensor.plug"
    assert s2.tariff()["mode"] == "dynamic"
    assert s2.tariff()["price_entity"] == "sensor.price"
    assert s2.control_enabled() is True
