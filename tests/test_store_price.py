"""Tests for the dynamic-tariff price-forecast parser."""

from __future__ import annotations

from smart_energy_agent.store import Store


def _store(attrs):
    s = Store()
    s._settings["tariff"] = {"mode": "dynamic", "price_entity": "sensor.price"}
    s._live_by_id = {"sensor.price": {"state": "30", "attributes": attrs}}
    return s


def test_price_forecast_nordpool_shape():
    s = _store({"unit_of_measurement": "ct/kWh",
                "raw_today": [{"start": 1700000000, "value": 10}],
                "raw_tomorrow": [{"start": 1700086400, "value": 20}]})
    assert s.price_forecast() == [(1700000000.0, 10.0), (1700086400.0, 20.0)]


def test_price_forecast_generic_iso_with_unit_scaling():
    s = _store({"unit_of_measurement": "EUR/kWh",
                "forecast": [{"start": "2025-01-01T00:00:00", "price": 0.10},
                             {"start": "2025-01-01T01:00:00", "price": 0.25}]})
    fc = s.price_forecast()
    assert len(fc) == 2 and fc[0][1] == 10.0 and fc[1][1] == 25.0   # EUR/kWh -> ct/kWh


def test_price_forecast_empty_when_not_dynamic_or_unparseable():
    s = _store({"raw_today": [{"start": 1, "value": 5}]})
    s._settings["tariff"] = {"mode": "static"}
    assert s.price_forecast() == []
    assert _store({"junk": 1}).price_forecast() == []
