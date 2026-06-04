"""Declarative catalog of setup categories and their entity slots.

This drives the guided setup wizard, the suggestion ranking and the
category-grouped device view. Each category is a *logical* device genus (PV
system, heat pump, grid) whose slots may be filled with HA entities from *any*
HA device — which is the whole point: the quantities the agent needs are often
spread across many HA devices (a heat pump = several devices) and power is often
measured by a separate meter (e.g. a Shelly PM3 channel).

v1 covers PV, heat pump and grid. Adding battery/house/consumers later is a
data-only change here.
"""

from __future__ import annotations

from typing import Any, Optional

from . import discovery

# Unit groups: the hard filter a candidate entity must satisfy for a slot.
UNIT_GROUPS: dict[str, dict[str, set[str]]] = {
    "power": {"device_classes": {"power"}, "units": set(discovery.POWER_UNITS), "domains": set()},
    "energy": {"device_classes": {"energy"}, "units": set(discovery.ENERGY_UNITS), "domains": set()},
    "soc": {"device_classes": {"battery"}, "units": {"%"}, "domains": set()},
    "temperature": {"device_classes": {"temperature"}, "units": {"°C", "°F"}, "domains": set()},
    "climate": {"device_classes": set(), "units": set(), "domains": {"climate"}},
    "switch": {"device_classes": set(), "units": set(), "domains": {"switch", "input_boolean"}},
}

# Per-category extra boolean flags (not entity slots).
CATEGORY_FLAGS: dict[str, set[str]] = {"grid": {"invert"}}

CATEGORIES: list[dict[str, Any]] = [
    {
        "key": "pv",
        "label": "PV-Anlage",
        "hints": list(discovery.PV_HINTS),
        "slots": [
            {"key": "power", "label": "Aktuelle Leistung", "unit_group": "power",
             "multi": True, "required": True,
             "help": "Wechselrichter-/MPPT-Leistung (mehrere werden summiert)."},
            {"key": "energy_today", "label": "Tagesertrag (Energie)", "unit_group": "energy",
             "multi": False, "required": False, "help": "kWh-Zähler der Erzeugung (optional)."},
        ],
    },
    {
        "key": "heat_pump",
        "label": "Wärmepumpe",
        "hints": list(discovery.HEATPUMP_HINTS),
        "slots": [
            {"key": "power", "label": "Elektrische Leistung", "unit_group": "power",
             "multi": False, "required": True,
             "help": "Oft von einem separaten Messgerät (z. B. Shelly PM3)."},
            {"key": "energy", "label": "Elektrische Energie", "unit_group": "energy",
             "multi": False, "required": False, "help": "kWh-Zähler der WP (optional)."},
        ],
    },
    {
        "key": "grid",
        "label": "Netz (Bezug/Einspeisung)",
        "hints": list(discovery.GRID_HINTS),
        "slots": [
            {"key": "power", "label": "Netzleistung (+ Bezug / − Einspeisung)", "unit_group": "power",
             "multi": False, "required": True,
             "help": "Vorzeichenbehaftete Leistung am Netzanschluss."},
            {"key": "import_power", "label": "Bezugsleistung (nur +, optional)", "unit_group": "power",
             "multi": False, "required": False,
             "help": "Alternative, wenn keine vorzeichenbehaftete Leistung existiert."},
            {"key": "export_power", "label": "Einspeiseleistung (nur +, optional)", "unit_group": "power",
             "multi": False, "required": False, "help": "Gegenstück zur Bezugsleistung."},
        ],
    },
]

CATEGORY_KEYS = {c["key"] for c in CATEGORIES}


def find_category(category_key: str) -> Optional[dict[str, Any]]:
    return next((c for c in CATEGORIES if c["key"] == category_key), None)


def find_slot(category_key: str, slot_key: str) -> Optional[dict[str, Any]]:
    cat = find_category(category_key)
    if not cat:
        return None
    return next((s for s in cat["slots"] if s["key"] == slot_key), None)


def is_flag(category_key: str, key: str) -> bool:
    return key in CATEGORY_FLAGS.get(category_key, set())
