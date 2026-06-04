"""Unit tests for the history-based consumption forecast (concept ch. 5a.2).

The synthetic load pattern is a pure function of (hour-of-day, weekday/weekend),
so the recency-weighted profile reproduces it exactly and the assertions stay
timezone-independent: both profile building and prediction map a timestamp to a
bucket via ``time.localtime``, so they agree regardless of the machine's TZ.
"""

from __future__ import annotations

import time

import pytest

from smart_energy_agent.forecast import (
    build_load_profile,
    build_surplus_forecast,
    forecast_consumption,
    parse_pv_forecast,
    profile_backtest,
)

# Fixed reference instant so tests never depend on the wall clock.
NOW = 1_700_000_000  # 2023-11-14T22:13:20Z


def _pattern_w(hour: int, wday: int) -> float:
    """Deterministic load: rises with the hour, higher on weekends."""
    return 200.0 + hour * 50.0 + (1000.0 if wday >= 5 else 0.0)


def _hourly_rows(days: float, now: int = NOW) -> list[tuple[int, float]]:
    """Hourly (ts, watt) samples for the `days` ending just before `now`."""
    rows: list[tuple[int, float]] = []
    t = int(now) - int(days * 86400)
    while t < int(now):
        tm = time.localtime(t)
        rows.append((t, _pattern_w(tm.tm_hour, tm.tm_wday)))
        t += 3600
    return rows


def test_profile_reproduces_deterministic_pattern() -> None:
    profile = build_load_profile(_hourly_rows(14), now=NOW)
    assert profile.samples == 14 * 24
    assert profile.span_days >= 13.0
    # Every populated bucket equals the exact pattern value for that slot.
    for (day_class, hour), watt in profile.buckets.items():
        wday = 6 if day_class == "weekend" else 2  # any representative day
        assert watt == pytest.approx(_pattern_w(hour, wday))


def test_forecast_matches_pattern_and_full_coverage() -> None:
    fc = forecast_consumption(_hourly_rows(14), hours=24, now=NOW)
    assert fc["horizon_h"] == 24
    assert fc["coverage"] == 1.0
    assert len(fc["points"]) == 24
    assert fc["start_ts"] == (NOW // 3600 + 1) * 3600
    for point in fc["points"]:
        tm = time.localtime(point["ts"])
        assert point["source"] == "profile"
        assert point["watt"] == pytest.approx(_pattern_w(tm.tm_hour, tm.tm_wday))
    assert fc["kwh"] > 0.0


def test_backtest_is_near_zero_for_deterministic_pattern() -> None:
    acc = profile_backtest(_hourly_rows(14), now=NOW)
    assert acc["samples"] > 0
    assert acc["mae_w"] is not None and acc["mae_w"] < 1.0
    assert acc["mape_pct"] is not None and acc["mape_pct"] < 1.0


def test_recency_weighting_favours_recent_days() -> None:
    # Old days read 100 W at hour 10; the most recent day reads 1000 W.
    rows: list[tuple[int, float]] = []
    t = NOW - 14 * 86400
    while t < NOW:
        tm = time.localtime(t)
        recent = t >= NOW - 86400
        rows.append((t, 1000.0 if (tm.tm_hour == 10 and recent) else 100.0))
        t += 3600
    profile = build_load_profile(rows, half_life_days=2.0, now=NOW)
    tm10 = next(
        r[0] for r in rows if time.localtime(r[0]).tm_hour == 10 and r[0] >= NOW - 86400
    )
    day_class = "weekend" if time.localtime(tm10).tm_wday >= 5 else "weekday"
    # With a 2-day half-life the recent 1000 W pulls the hour-10 bucket well above
    # the old 100 W baseline.
    assert profile.buckets[(day_class, 10)] > 300.0


def test_empty_history_yields_no_forecast() -> None:
    fc = forecast_consumption([], hours=24, now=NOW)
    assert fc["samples"] == 0
    assert fc["coverage"] == 0.0
    assert all(p["watt"] is None and p["source"] == "none" for p in fc["points"])
    acc = profile_backtest([], now=NOW)
    assert acc == {"samples": 0, "mae_w": None, "mape_pct": None}


def test_none_and_invalid_values_are_skipped() -> None:
    rows = [(NOW - 7200, None), (NOW - 3600, "bad"), (NOW - 1800, 500.0)]
    profile = build_load_profile(rows, now=NOW)
    assert profile.samples == 1
    assert profile.overall_w == 500.0


# --- PV / surplus forecast ---------------------------------------------------

def test_parse_pv_forecast_solar_watts_dict() -> None:
    state = {
        "state": "5.0",
        "attributes": {
            "watts": {
                "2023-11-15T10:00:00+00:00": 3000,
                "2023-11-15T09:00:00+00:00": 1500,  # out of order on purpose
            }
        },
    }
    pts = parse_pv_forecast(state)
    assert [w for _ts, w in pts] == [1500.0, 3000.0]  # sorted by timestamp


def test_parse_pv_forecast_solcast_kw_to_w() -> None:
    state = {
        "attributes": {
            "detailedForecast": [
                {"period_start": "2023-11-15T09:00:00+00:00", "pv_estimate": 1.5},
                {"period_start": "2023-11-15T10:00:00+00:00", "pv_estimate": 3.0},
            ]
        }
    }
    pts = parse_pv_forecast(state)
    assert [w for _ts, w in pts] == [1500.0, 3000.0]  # kW -> W


def test_parse_pv_forecast_generic_and_empty() -> None:
    state = {"attributes": {"forecast": [
        {"datetime": "2023-11-15T09:00:00Z", "power": 800},
    ]}}
    assert [w for _ts, w in parse_pv_forecast(state)] == [800.0]
    assert parse_pv_forecast(None) == []
    assert parse_pv_forecast({}) == []


def test_build_surplus_forecast_combines_pv_and_load() -> None:
    t0 = (NOW // 3600 + 1) * 3600
    consumption = {"points": [
        {"ts": t0, "watt": 300.0},
        {"ts": t0 + 3600, "watt": 500.0},
    ]}
    pv_points = [(t0, 1000.0), (t0 + 3600, 200.0)]
    sur = build_surplus_forecast(consumption, pv_points)
    assert [p["surplus_w"] for p in sur["points"]] == [700.0, -300.0]
    assert sur["pv_available"] is True
    assert sur["pv_coverage"] == 1.0
    assert sur["pv_kwh"] == 1.2
    assert sur["load_kwh"] == 0.8
    assert sur["surplus_kwh"] == 0.4


def test_build_surplus_forecast_without_pv() -> None:
    t0 = (NOW // 3600 + 1) * 3600
    consumption = {"points": [{"ts": t0, "watt": 300.0}]}
    sur = build_surplus_forecast(consumption, [])
    assert sur["pv_available"] is False
    assert sur["pv_coverage"] == 0.0
    assert sur["points"][0]["pv_w"] is None
    assert sur["points"][0]["surplus_w"] is None
