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


def prefill_from_prefs(prefs: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Derive an instance-shaped pre-fill from the HA energy preferences.

    solar -> one PV instance (named powers from stat_rate); battery -> a battery
    instance; grid power + price; named device_consumption -> a heat_pump /
    water_heater / ev_charger instance with a named power. Returns a config-shaped
    dict (lists of instances) plus a ``tariff`` hint.
    """
    out: dict[str, Any] = {
        "grid": {}, "pv": [], "battery": [], "heat_pump": [],
        "water_heater": [], "ev_charger": [], "consumers": [], "tariff": {},
    }
    if not isinstance(prefs, dict):
        return out

    def named(entity: str, label: str) -> dict[str, Any]:
        return {"id": "", "name": label, "entity": entity}

    pv_powers: list[dict[str, Any]] = []
    pv_energy: list[dict[str, Any]] = []
    for s in prefs.get("energy_sources") or []:
        if not isinstance(s, dict):
            continue
        stype = s.get("type")
        if stype == "solar":
            if s.get("stat_rate"):
                pv_powers.append(named(s["stat_rate"], "PV"))
            if s.get("stat_energy_from"):
                pv_energy.append(named(s["stat_energy_from"], "Ertrag"))
        elif stype == "battery":
            # Recognise the battery even if only energy entities are configured;
            # the power/SoC are derived from the same device later (store._prefill).
            if (s.get("stat_rate") or s.get("stat_soc")
                    or s.get("stat_energy_from") or s.get("stat_energy_to")):
                out["battery"].append({
                    "id": "", "name": "Batterie",
                    "power": s.get("stat_rate", ""), "soc": s.get("stat_soc", ""),
                    "invert": False,
                })
        elif stype == "grid":
            gf = _grid_fields(s)
            if gf.get("power"):
                out["grid"]["power"] = gf["power"]
            if gf.get("price_entity"):
                out["tariff"]["price_entity"] = gf["price_entity"]
    if pv_powers or pv_energy:
        out["pv"].append({"id": "", "name": "PV-Anlage", "powers": pv_powers, "energy": pv_energy})

    consumption_map = (
        ("heat_pump", discovery.HEATPUMP_HINTS),
        ("ev_charger", discovery.EV_HINTS),
        ("water_heater", discovery.WATERHEATER_HINTS),
    )
    for dc in prefs.get("device_consumption") or []:
        if not isinstance(dc, dict):
            continue
        name = (dc.get("name") or "").lower()
        for kind, hints in consumption_map:
            if any(h in name for h in hints):
                ekey = "energies" if kind in ("heat_pump", "water_heater") else "energy"
                inst: dict[str, Any] = {"id": "", "name": dc.get("name") or kind, "powers": [], ekey: []}
                if dc.get("stat_rate"):
                    inst["powers"].append(named(dc["stat_rate"], "Leistung"))
                if dc.get("stat_consumption"):
                    inst[ekey].append(named(dc["stat_consumption"], "Energie"))
                out[kind].append(inst)
                break
    return out


def derive_on_device(
    states: list[dict[str, Any]],
    entity_registry: Optional[list[dict[str, Any]]],
    ref_entity_id: Optional[str],
    unit_group: str,
    hints: Iterable[str] = (),
) -> Optional[str]:
    """Find an entity of ``unit_group`` on the *same HA device* as ``ref_entity_id``.

    Used to derive e.g. the battery power entity from a battery energy/SoC entity
    that the Energy dashboard already references (same naming/device logic).
    Prefers candidates whose name matches ``hints``.
    """
    if not ref_entity_id:
        return None
    meta_by = {e["entity_id"]: e for e in (entity_registry or []) if e.get("entity_id")}
    did = (meta_by.get(ref_entity_id) or {}).get("device_id")
    if not did:
        return None
    hints = tuple(hints)
    best: Optional[str] = None
    best_score = -1
    for st in states or []:
        eid = st.get("entity_id")
        if not eid:
            continue
        m = meta_by.get(eid, {})
        if m.get("device_id") != did or m.get("disabled_by") or m.get("hidden_by"):
            continue
        if m.get("entity_category") in discovery.SKIP_CATEGORIES:
            continue
        attrs = st.get("attributes", {}) or {}
        if not _matches_unit_group(_domain(eid), attrs.get("device_class"),
                                   attrs.get("unit_of_measurement"), unit_group):
            continue
        hay = f"{attrs.get('friendly_name', '')} {eid}".lower()
        score = sum(1 for h in hints if h in hay)
        if score > best_score:
            best_score, best = score, eid
    return best


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
