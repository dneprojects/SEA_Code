"""Tests for the universal tariff price model (cheap-now decision)."""

from __future__ import annotations

from datetime import datetime

from smart_energy_agent import tariff


def _price_state(state, **attrs):
    return {"state": state, "attributes": attrs}


def test_parse_forecast_list_of_numbers():
    attrs = {"today": [10, 20, 30, 5]}
    assert tariff.parse_price_forecast(attrs) == [10, 20, 30, 5]


def test_parse_forecast_list_of_dicts_and_tomorrow():
    attrs = {"raw_today": [{"total": 0.30}, {"total": 0.10}],
             "raw_tomorrow": [{"total": 0.25}]}
    assert tariff.parse_price_forecast(attrs) == [0.30, 0.10, 0.25]


def test_parse_forecast_generic_key():
    assert tariff.parse_price_forecast({"forecast": [1, 2, 3]}) == [1, 2, 3]
    assert tariff.parse_price_forecast({}) == []


def test_dynamic_cheap_by_forecast_rank():
    # current 12 ct sits in the cheapest third of the day -> cheap
    fc = list(range(10, 34))  # 10..33
    res = tariff.cheap_now({"mode": "dynamic"}, _price_state(12, today=fc), datetime(2026, 6, 5, 3))
    assert res["cheap"] is True and res["source"] == "forecast"


def test_dynamic_expensive_by_forecast_rank():
    fc = list(range(10, 34))
    res = tariff.cheap_now({"mode": "dynamic"}, _price_state(30, today=fc), datetime(2026, 6, 5, 18))
    assert res["cheap"] is False and res["source"] == "forecast"


def test_dynamic_threshold_without_forecast():
    t = {"mode": "dynamic", "price_entity": "sensor.p", "cheap_max_ct": 15}
    assert tariff.cheap_now(t, _price_state(12), datetime(2026, 6, 5, 3))["cheap"] is True
    assert tariff.cheap_now(t, _price_state(20), datetime(2026, 6, 5, 3))["cheap"] is False


def test_dynamic_no_forecast_no_threshold():
    res = tariff.cheap_now({"mode": "dynamic"}, _price_state(12), datetime(2026, 6, 5, 3))
    assert res["cheap"] is False and res["source"] == "none"


def test_ht_nt_window_wraps_midnight():
    t = {"mode": "ht_nt", "nt_start": "22:00", "nt_end": "06:00"}
    assert tariff.cheap_now(t, {}, datetime(2026, 6, 5, 23))["cheap"] is True   # in NT
    assert tariff.cheap_now(t, {}, datetime(2026, 6, 5, 3))["cheap"] is True    # in NT
    assert tariff.cheap_now(t, {}, datetime(2026, 6, 5, 12))["cheap"] is False  # HT


def test_static_never_cheap():
    assert tariff.cheap_now({"mode": "static"}, {}, datetime(2026, 6, 5, 3))["cheap"] is False


def test_has_price_source():
    assert tariff.has_price_source({"mode": "dynamic", "price_entity": "sensor.p"}) is True
    assert tariff.has_price_source({"mode": "dynamic"}) is False
    assert tariff.has_price_source({"mode": "ht_nt", "nt_start": "22:00", "nt_end": "06:00"}) is True
    assert tariff.has_price_source({"mode": "static"}) is False
