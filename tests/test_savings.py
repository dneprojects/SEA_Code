"""Tests for the savings estimator, incl. the 'ideal' best-case baseline."""

from __future__ import annotations

import asyncio
import time

import aiosqlite

from smart_energy_agent.store import Store


def _store_with_rows(rows):
    """Build a Store backed by an in-memory DB filled with energy_state rows.

    rows: list of (ts_offset_s, pv_w, grid_w, house_load_w, price_ct); the
    offset is added to "now − 120 s" so the rows fall inside the query window.
    """
    base = int(time.time()) - 120
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


def test_ideal_baseline_shows_extra_cost_under_price_variation():
    # No PV; constant 20 kW import. Prices 40 -> 10 ct/kWh over two 30 s intervals.
    rows = [
        (0, 0.0, 20000.0, 20000.0, 40.0),
        (30, 0.0, 20000.0, 20000.0, 10.0),
        (60, 0.0, 20000.0, 20000.0, 10.0),
    ]
    s = _store_with_rows(rows)
    d = asyncio.run(s.savings(3600, baseline="ideal"))
    assert d["baseline"] == "ideal"
    assert d["price_min_ct"] == 10.0
    # ideal buys everything at the cheapest price -> actual (partly at 40) is higher
    assert d["savings_eur"] < 0          # Mehrkosten vs. ideal
    assert d["baseline_eur"] < d["actual_eur"]
    assert d["import_kwh"] == d["house_kwh"]


def test_ideal_baseline_zero_gap_for_flat_price_no_pv():
    rows = [
        (0, 0.0, 2000.0, 2000.0, 30.0),
        (30, 0.0, 2000.0, 2000.0, 30.0),
        (60, 0.0, 2000.0, 2000.0, 30.0),
    ]
    s = _store_with_rows(rows)
    d = asyncio.run(s.savings(3600, baseline="ideal"))
    assert d["price_min_ct"] == 30.0
    assert abs(d["savings_eur"]) < 1e-6   # nothing to optimize on a flat tariff


def test_ideal_baseline_credits_pv_self_consumption():
    # PV fully covers the house -> net grid demand 0 -> ideal cost 0,
    # while the actual run still imports -> Mehrkosten.
    rows = [
        (0, 2000.0, 2000.0, 2000.0, 30.0),   # PV produced but house still drew from grid
        (30, 2000.0, 2000.0, 2000.0, 30.0),
        (60, 2000.0, 2000.0, 2000.0, 30.0),
    ]
    s = _store_with_rows(rows)
    d = asyncio.run(s.savings(3600, baseline="ideal"))
    # house_kwh == pv_kwh -> net 0 -> ideal baseline cost ~ 0
    assert abs(d["baseline_eur"]) < 1e-6
    assert d["savings_eur"] < 0
