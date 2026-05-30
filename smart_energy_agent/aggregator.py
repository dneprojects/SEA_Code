"""Live energy balance aggregation.

Computes the household energy balance from the *included* classified entities.
Pure function over a list of EnergyEntity, so it is unit-testable.

Sign conventions (assumed; configurable per-entity in a later phase):
  * grid_w : positive = import from grid, negative = export/feed-in
  * batt_w : positive = charging (consumes power), negative = discharging
  * pv_w, house_load_w : non-negative magnitudes

Available PV surplus:
  * If a measured house-load entity exists: surplus = pv - house_load
  * Otherwise derived from the balance: house_load = pv + grid - batt,
    hence surplus = pv - house_load = batt - grid.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .models import EnergyEntity, EnergyRole

POWER_UNITS = ("W", "kW", "MW")


def _is_power(entity: EnergyEntity) -> bool:
    return entity.unit in POWER_UNITS or entity.power_w is not None


def _sum_power(entities: Iterable[EnergyEntity], role: str) -> tuple[float, int]:
    total = 0.0
    count = 0
    for e in entities:
        if e.role == role and e.include and _is_power(e):
            total += e.power_w or 0.0
            count += 1
    return total, count


def compute_balance(
    entities: list[EnergyEntity],
    grid_invert: bool = False,
    battery_invert: bool = False,
) -> dict[str, Any]:
    """Return the current energy balance from included entities.

    grid_invert / battery_invert flip the sign of the grid/battery power to
    cope with integrations that report import/charge with the opposite sign.
    """
    pv_w, n_pv = _sum_power(entities, EnergyRole.PV)
    grid_w, n_grid = _sum_power(entities, EnergyRole.GRID)
    batt_w, n_batt = _sum_power(entities, EnergyRole.BATTERY)
    house_w, n_house = _sum_power(entities, EnergyRole.HOUSE_LOAD)

    if grid_invert:
        grid_w = -grid_w
    if battery_invert:
        batt_w = -batt_w

    # Battery state of charge: average of included battery % entities.
    soc_vals: list[float] = []
    for e in entities:
        if e.role == EnergyRole.BATTERY and e.include and e.unit == "%":
            try:
                soc_vals.append(float(e.state))
            except (TypeError, ValueError):
                pass
    batt_soc = round(sum(soc_vals) / len(soc_vals), 1) if soc_vals else None

    house_measured = n_house > 0
    if house_measured:
        house_load_w = house_w
    else:
        # Derive from balance: load = pv + grid_import - grid_export - batt_charge(+)
        house_load_w = pv_w + grid_w - batt_w

    surplus_w = pv_w - house_load_w

    return {
        "pv_w": round(pv_w, 1),
        "grid_w": round(grid_w, 1),
        "battery_w": round(batt_w, 1),
        "battery_soc": batt_soc,
        "house_load_w": round(house_load_w, 1),
        "house_load_measured": house_measured,
        "surplus_w": round(surplus_w, 1),
        "sources": {"pv": n_pv, "grid": n_grid, "battery": n_batt, "house": n_house},
    }
