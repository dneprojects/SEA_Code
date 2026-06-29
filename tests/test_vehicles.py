"""Tests for first-class vehicles (soc_only / chargeable / bidirectional)."""

from __future__ import annotations

from smart_energy_agent.devices import CAP_CHARGE, CAP_DISCHARGE, devices
from smart_energy_agent.store import Store


def _store():
    s = Store()
    s._config = {}                      # no wizard devices
    s._settings["vehicles"] = []
    return s


def test_set_vehicles_stamps_id_and_roundtrips():
    s = _store()
    out = s.set_vehicles([{"name": "Auto"}])
    assert len(out) == 1 and out[0]["name"] == "Auto" and out[0]["id"]
    assert s.vehicles()[0]["id"] == out[0]["id"]


def test_vehicle_for_charger_present_filter():
    s = _store()
    s._settings["vehicles"] = [
        {"id": "v1", "charger_key": "ev_charger:1"},                       # no present entity -> present
        {"id": "v2", "charger_key": "ev_charger:2", "present_entity": "binary_sensor.x"},
    ]
    assert s.vehicle_for_charger("ev_charger:1")["id"] == "v1"
    assert s.vehicle_for_charger("ev_charger:2") is None                    # present entity not truthy
    assert s.vehicle_for_charger("ev_charger:9") is None                    # nothing linked


def test_bidirectional_present_vehicle_becomes_storage_device():
    s = _store()
    s._settings["vehicles"] = [{
        "id": "v1", "name": "EV", "capability": "bidirectional",
        "charge_entity": "number.vc", "discharge_entity": "number.vd", "soc_entity": "sensor.car",
        "max_w": 11000, "w_per_unit": 1, "reserve_soc": 20, "target_soc": 80,
    }]
    dev = {d.key: d for d in devices(s)}["vehicle:v1"]
    assert dev.has_cap(CAP_DISCHARGE) and dev.has_cap(CAP_CHARGE)           # storage -> reserve/peak pick it up
    assert dev.max_w == 11000 and dev.grid_soc_min == 20 and dev.grid_soc_max == 80
    assert dev.self_consumption is True


def test_soc_only_and_chargeable_vehicles_are_not_storage_devices():
    s = _store()
    s._settings["vehicles"] = [
        {"id": "v1", "capability": "soc_only", "soc_entity": "sensor.car"},
        {"id": "v2", "capability": "chargeable", "soc_entity": "sensor.car2", "discharge_entity": "number.x"},
    ]
    assert all(not d.key.startswith("vehicle:") for d in devices(s))
