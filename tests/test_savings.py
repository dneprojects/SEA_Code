"""Tests for the savings estimator: dynamic battery simulation + sink cap."""

from __future__ import annotations

import asyncio
import time

import aiosqlite

from smart_energy_agent.store import Store


def _store_with_rows(rows):
    """Build a Store backed by an in-memory DB filled with energy_state rows.

    rows: list of (ts_offset_s, pv_w, grid_w, house_load_w, price_ct); the
    offset is added to "now − 300 s" so the rows fall inside the query window.
    """
    base = int(time.time()) - 300
    rows = [(base + r[0], *r[1:]) for r in rows]
    s = Store()
    s._settings["tariff"] = {"mode": "static", "price_ct": 30.0, "feed_in_ct": 8.0}

    async def setup():
        s._db = await aiosqlite.connect(":memory:")
        await s._db.execute(
            "CREATE TABLE energy_state (ts INTEGER, pv_w REAL, grid_w REAL, "
            "battery_w REAL, battery_soc REAL, house_load_w REAL, surplus_w REAL, price_ct REAL)"
        )
        await s._db.executemany(
            "INSERT INTO energy_state (ts, pv_w, grid_w, house_load_w, price_ct) "
            "VALUES (?,?,?,?,?)",
            rows,
        )
        await s._db.commit()

    asyncio.run(setup())
    return s


def _sav(store, **kw):
    return asyncio.run(store.savings(3600, baseline="dynamic", **kw))


def test_savings_eur_is_baseline_minus_actual():
    rows = [(i * 30, 1000.0, 1000.0, 2000.0, 30.0) for i in range(5)]
    d = _sav(_store_with_rows(rows))
    assert d["baseline"] == "dynamic"
    assert d["savings_eur"] == round(d["baseline_eur"] - d["actual_eur"], 2)


def test_dynamic_defaults_to_current_installation():
    # no battery devices -> current capacity 0; pv_kw defaults to observed peak
    rows = [(i * 30, 2000.0, -1000.0, 1000.0, 30.0) for i in range(4)]
    d = _sav(_store_with_rows(rows))
    assert d["battery_kwh_current"] == 0.0
    assert d["batt_kwh"] == 0.0          # defaulted to current battery size
    assert d["pv_peak_kw"] == 2.0        # max pv_w = 2000 W
    assert d["pv_kw"] == 2.0             # defaulted to the observed peak


def test_dynamic_pv_factor_scales_generation_by_dreisatz():
    rows = [(i * 30, 2000.0, -1000.0, 1000.0, 30.0) for i in range(4)]
    s = _store_with_rows(rows)
    base = _sav(s)                       # pv_kw = peak (2 kWp) -> factor 1
    doubled = _sav(s, pv_kw=4.0)         # 4 kWp -> factor 2
    assert abs(doubled["pv_sim_kwh"] - 2 * base["pv_sim_kwh"]) < 1e-6
    assert abs(base["pv_sim_kwh"] - base["pv_kwh"]) < 1e-6


def test_flat_tariff_no_lossy_grid_charge_with_bigger_battery():
    # Flat price (no spread): a bigger battery must never raise the baseline cost
    # (no loss-making night grid-charging). Mix of surplus and deficit.
    rows = []
    for i in range(40):
        pv, grid = (3000.0, -2000.0) if i % 2 == 0 else (0.0, 1000.0)
        rows.append((i * 30, pv, grid, 1000.0, 30.0))
    s = _store_with_rows(rows)
    small = asyncio.run(s.savings(3600, baseline="dynamic", batt_kwh=5.0,
                                  p_high=30.0, p_normal=30.0, p_low=30.0))
    big = asyncio.run(s.savings(3600, baseline="dynamic", batt_kwh=20.0,
                                p_high=30.0, p_normal=30.0, p_low=30.0))
    assert big["baseline_eur"] <= small["baseline_eur"] + 1e-9


def test_surplus_sink_daily_energy_cap_limits_absorption():
    # steady PV surplus (4 kW PV, 1 kW house) -> lots of exportable surplus
    rows = [(i * 30, 4000.0, -3000.0, 1000.0, 30.0) for i in range(20)]
    s = _store_with_rows(rows)
    uncapped = asyncio.run(s.savings(3600, baseline="surplus_sink", sink_cap_w=5000.0))
    capped = asyncio.run(
        s.savings(3600, baseline="surplus_sink", sink_cap_w=5000.0, sink_kwh_day=0.01)
    )
    assert capped["sink_kwh"] < uncapped["sink_kwh"]
    assert capped["sink_kwh"] <= 0.01 + 1e-9
