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


def _state_power_w(state: Optional[dict[str, Any]]) -> Optional[float]:
    """Convert a raw HA state dict into watts (handles W/kW/MW)."""
    if not state:
        return None
    try:
        val = float(state.get("state"))
    except (TypeError, ValueError):
        return None
    unit = (state.get("attributes") or {}).get("unit_of_measurement")
    if unit == "kW":
        val *= 1000.0
    elif unit == "MW":
        val *= 1_000_000.0
    return val


LOAD_KINDS = ("heat_pump", "water_heater", "ev_charger", "consumers")


def _powers_of(inst: dict[str, Any]) -> list[str]:
    """Entity ids of an instance's named power sub-list."""
    return [p.get("entity") for p in (inst.get("powers") or []) if isinstance(p, dict) and p.get("entity")]


def balance_from_config(
    config: dict[str, Any],
    live_by_id: dict[str, dict[str, Any]],
    *,
    grid_invert: bool = False,
) -> dict[str, Any]:
    """Energy balance from the instance-based wizard configuration.

    house_load = pv + grid − battery_charge; surplus = pv − house_load.
    ``loads`` carries one node per load instance (summed) plus its ``parts``
    (named individual powers) for the expandable power-flow diagram.
    """
    def val(entity_id: Optional[str]) -> Optional[float]:
        return _state_power_w(live_by_id.get(entity_id)) if entity_id else None

    def num(entity_id: Optional[str]) -> Optional[float]:
        st = live_by_id.get(entity_id) if entity_id else None
        try:
            return float(st.get("state")) if st else None
        except (TypeError, ValueError):
            return None

    # PV: sum all powers of all PV instances.
    pv_w = 0.0
    n_pv = 0
    for inst in config.get("pv") or []:
        for eid in _powers_of(inst):
            v = val(eid)
            if v is not None:
                pv_w += v
                n_pv += 1

    # Battery: sum power (±invert) over instances; SoC = mean of available.
    batt_w = 0.0
    soc_vals: list[float] = []
    n_batt = 0
    for inst in config.get("battery") or []:
        v = val(inst.get("power"))
        if v is not None:
            batt_w += -v if inst.get("invert") else v
            n_batt += 1
        s = num(inst.get("soc"))
        if s is not None:
            soc_vals.append(s)
    batt_soc = round(sum(soc_vals) / len(soc_vals), 1) if soc_vals else None

    grid_cfg = config.get("grid") or {}
    grid_w: Optional[float] = None
    if grid_cfg.get("power"):
        grid_w = val(grid_cfg["power"])
        if grid_w is not None and (grid_cfg.get("invert") or grid_invert):
            grid_w = -grid_w
    elif grid_cfg.get("import_power") or grid_cfg.get("export_power"):
        grid_w = (val(grid_cfg.get("import_power")) or 0.0) - (val(grid_cfg.get("export_power")) or 0.0)
    grid_w = grid_w or 0.0

    house_load_w = pv_w + grid_w - batt_w
    surplus_w = pv_w - house_load_w

    loads: list[dict[str, Any]] = []
    for kind in LOAD_KINDS:
        for inst in config.get(kind) or []:
            parts = []
            total = 0.0
            have = False
            for p in inst.get("powers") or []:
                if not isinstance(p, dict):
                    continue
                v = val(p.get("entity"))
                if v is not None:
                    parts.append({"name": p.get("name", "Leistung"), "power_w": round(v, 1)})
                    total += v
                    have = True
            loads.append({
                "key": kind + ":" + str(inst.get("id")),
                "name": inst.get("name", kind),
                "kind": kind,
                "power_w": round(total, 1) if have else None,
                "parts": parts,
            })

    has_grid = bool(grid_cfg.get("power") or grid_cfg.get("import_power") or grid_cfg.get("export_power"))
    return {
        "pv_w": round(pv_w, 1),
        "grid_w": round(grid_w, 1),
        "battery_w": round(batt_w, 1),
        "battery_soc": batt_soc,
        "house_load_w": round(house_load_w, 1),
        "house_load_measured": False,
        "surplus_w": round(surplus_w, 1),
        "loads": loads,
        "sources": {"pv": n_pv, "grid": 1 if has_grid else 0, "battery": n_batt, "house": 0},
    }


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
