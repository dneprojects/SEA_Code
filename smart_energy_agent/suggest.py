"""Suggestion/ranking engine for the setup wizard (pure, unit-testable).

Reuses the discovery heuristics, but only to *rank* candidates — it never makes
the final assignment (the user does). It also reads the HA Energy-dashboard
preferences (``energy/get_prefs``) to pre-fill slots and to boost the entities
already configured there to the top of the suggestions.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from . import discovery
from .setup_catalog import UNIT_GROUPS

PREFS_BONUS = 100.0
HINT_BONUS = 10.0
DEVICE_HINT_BONUS = 5.0
QUERY_BONUS = 3.0


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else entity_id


def _matches_unit_group(
    domain: str, device_class: Optional[str], unit: Optional[str], unit_group: str
) -> bool:
    spec = UNIT_GROUPS.get(unit_group)
    if not spec:
        return False
    if domain in spec["domains"]:
        return True
    if device_class and device_class in spec["device_classes"]:
        return True
    return bool(unit and unit in spec["units"])


def rank_for_slot(
    states: list[dict[str, Any]],
    entity_registry: Optional[list[dict[str, Any]]] = None,
    device_registry: Optional[list[dict[str, Any]]] = None,
    area_registry: Optional[list[dict[str, Any]]] = None,
    *,
    slot: dict[str, Any],
    category_hints: Iterable[str] = (),
    prefs_entities: Iterable[str] = (),
    query: str = "",
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Return candidate entities for a slot, ranked best-first."""
    meta_by = {e["entity_id"]: e for e in (entity_registry or []) if e.get("entity_id")}
    area_by_device = {
        d["id"]: d.get("area_id") for d in (device_registry or []) if d.get("id")
    }
    dev_name = {
        d["id"]: (d.get("name_by_user") or d.get("name"))
        for d in (device_registry or []) if d.get("id")
    }
    area_names = {
        a["area_id"]: a.get("name", a["area_id"])
        for a in (area_registry or []) if a.get("area_id")
    }
    unit_group = slot["unit_group"]
    hints = tuple(slot.get("hints") or ()) + tuple(category_hints)
    prefs = frozenset(prefs_entities)
    q = (query or "").strip().lower()

    out: list[dict[str, Any]] = []
    for st in states:
        eid = st.get("entity_id")
        if not eid:
            continue
        meta = meta_by.get(eid, {})
        if meta.get("disabled_by") or meta.get("hidden_by"):
            continue
        if meta.get("entity_category") in discovery.SKIP_CATEGORIES:
            continue
        attrs = st.get("attributes", {}) or {}
        domain = _domain(eid)
        device_class = attrs.get("device_class")
        unit = attrs.get("unit_of_measurement")
        if not _matches_unit_group(domain, device_class, unit, unit_group):
            continue

        name = attrs.get("friendly_name", eid)
        hay = f"{name} {eid}".lower()
        if q and q not in hay:
            continue

        did = meta.get("device_id")
        score = 0.0
        reasons: list[str] = []
        if eid in prefs:
            score += PREFS_BONUS
            reasons.append("aus Energy-Dashboard")
        hint_hits = sum(1 for h in hints if h in hay)
        if hint_hits:
            score += HINT_BONUS * hint_hits
            reasons.append("Namensmuster")
        dn = dev_name.get(did) or ""
        if dn and any(h in dn.lower() for h in hints):
            score += DEVICE_HINT_BONUS
        if q:
            score += QUERY_BONUS

        out.append({
            "entity_id": eid,
            "name": name,
            "area": area_names.get(area_by_device.get(did)),
            "device_name": dev_name.get(did),
            "unit": unit,
            "device_class": device_class,
            "state": st.get("state"),
            "score": round(score, 1),
            "reason": ", ".join(reasons),
        })

    out.sort(key=lambda c: (-c["score"], (c["name"] or "").lower()))
    return out[:limit]


# --- HA Energy-dashboard preferences ----------------------------------------

def _grid_fields(source: dict[str, Any]) -> dict[str, Any]:
    """Extract import/export/price from a grid source (unified or legacy flow)."""
    out: dict[str, Any] = {}
    if source.get("stat_rate"):
        out["power"] = source["stat_rate"]
    # Unified (current) grid source.
    if source.get("stat_energy_from"):
        out["import_energy"] = source["stat_energy_from"]
    if source.get("stat_energy_to"):
        out["export_energy"] = source["stat_energy_to"]
    if source.get("entity_energy_price"):
        out["price_entity"] = source["entity_energy_price"]
    # Legacy flow_from / flow_to lists.
    ff = source.get("flow_from")
    if isinstance(ff, list) and ff:
        if ff[0].get("stat_energy_from"):
            out.setdefault("import_energy", ff[0]["stat_energy_from"])
        if ff[0].get("entity_energy_price"):
            out.setdefault("price_entity", ff[0]["entity_energy_price"])
    ft = source.get("flow_to")
    if isinstance(ft, list) and ft and ft[0].get("stat_energy_to"):
        out.setdefault("export_energy", ft[0]["stat_energy_to"])
    return out


def prefill_from_prefs(prefs: Optional[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Derive slot pre-fills from the HA energy preferences.

    Returns ``{category: {slot: value}}`` plus a ``tariff`` hint. ``stat_rate``
    is the power entity (preferred for our power slots); energy stats fill the
    energy slots; ``device_consumption`` named like a heat pump fills heat_pump.
    """
    out: dict[str, dict[str, Any]] = {
        "pv": {}, "battery": {}, "grid": {}, "heat_pump": {}, "tariff": {}
    }
    if not isinstance(prefs, dict):
        return out

    sources = prefs.get("energy_sources") or []
    pv_rates: list[str] = []
    for s in sources:
        if not isinstance(s, dict):
            continue
        stype = s.get("type")
        if stype == "solar":
            if s.get("stat_rate"):
                pv_rates.append(s["stat_rate"])
            if s.get("stat_energy_from"):
                out["pv"].setdefault("energy_today", s["stat_energy_from"])
        elif stype == "battery":
            if s.get("stat_rate"):
                out["battery"]["power"] = s["stat_rate"]
            if s.get("stat_soc"):
                out["battery"]["soc"] = s["stat_soc"]
        elif stype == "grid":
            gf = _grid_fields(s)
            if gf.get("power"):
                out["grid"]["power"] = gf["power"]
            if gf.get("import_energy"):
                out["grid"]["import_energy"] = gf["import_energy"]
            if gf.get("export_energy"):
                out["grid"]["export_energy"] = gf["export_energy"]
            if gf.get("price_entity"):
                out["tariff"]["price_entity"] = gf["price_entity"]
    if pv_rates:
        out["pv"]["power"] = pv_rates

    for dc in prefs.get("device_consumption") or []:
        if not isinstance(dc, dict):
            continue
        name = (dc.get("name") or "").lower()
        if any(h in name for h in discovery.HEATPUMP_HINTS):
            if dc.get("stat_rate"):
                out["heat_pump"]["power"] = dc["stat_rate"]
            if dc.get("stat_consumption"):
                out["heat_pump"].setdefault("energy", dc["stat_consumption"])
    return out


def prefs_entity_set(prefs: Optional[dict[str, Any]]) -> set[str]:
    """All entity ids referenced anywhere in the energy preferences."""
    found: set[str] = set()
    if not isinstance(prefs, dict):
        return found
    for s in prefs.get("energy_sources") or []:
        if not isinstance(s, dict):
            continue
        for key in ("stat_rate", "stat_energy_from", "stat_energy_to",
                    "stat_soc", "entity_energy_price"):
            if s.get(key):
                found.add(s[key])
        for flow_key in ("flow_from", "flow_to"):
            for f in s.get(flow_key) or []:
                if isinstance(f, dict):
                    for key in ("stat_energy_from", "stat_energy_to", "entity_energy_price"):
                        if f.get(key):
                            found.add(f[key])
    for dc in prefs.get("device_consumption") or []:
        if isinstance(dc, dict):
            for key in ("stat_consumption", "stat_rate"):
                if dc.get(key):
                    found.add(dc[key])
    return found
