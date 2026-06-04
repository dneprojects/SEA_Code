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
    "number": {"device_classes": set(), "units": set(), "domains": {"number"}},
}

# Per-category extra boolean flags (not entity slots).
CATEGORY_FLAGS: dict[str, set[str]] = {"grid": {"invert"}, "battery": {"invert"}}

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
        "key": "battery",
        "label": "Batteriespeicher",
        "hints": list(discovery.BATTERY_HINTS_DEV),
        "slots": [
            {"key": "power", "label": "Lade-/Entladeleistung (+ Laden / − Entladen)",
             "unit_group": "power", "multi": False, "required": True,
             "help": "Vorzeichenbehaftete Batterieleistung."},
            {"key": "soc", "label": "Ladezustand (SoC)", "unit_group": "soc",
             "multi": False, "required": False, "help": "Batterie-% (optional)."},
        ],
    },
    {
        "key": "heat_pump",
        "label": "Wärmepumpe",
        "hints": list(discovery.HEATPUMP_HINTS),
        "slots": [
            {"key": "power", "label": "Elektrische Leistung(en)", "unit_group": "power",
             "multi": True, "required": True,
             "help": "Eine oder mehrere Pumpen; werden summiert. Oft separates Messgeraet (Shelly PM3)."},
            {"key": "energy", "label": "Elektrische Energie", "unit_group": "energy",
             "multi": True, "required": False, "help": "Ein oder mehrere kWh-Zaehler (optional)."},
            {"key": "temperatures", "label": "Temperaturen (Heizkreise, WW-/Pufferspeicher)",
             "unit_group": "temperature", "multi": True, "required": False,
             "help": "Zum Anzeigen; Grundlage fuer Absenkung/Anhebung."},
            {"key": "climate", "label": "Thermostat-/Klima-Entitaeten", "unit_group": "climate",
             "multi": True, "required": False, "control": True,
             "help": "Stellgroesse(n): Solltemperatur je Heizkreis."},
            {"key": "temp_setpoint", "label": "Soll-/Anhebungswerte (Zahlen-Entitaeten)",
             "unit_group": "number", "multi": True, "required": False, "control": True,
             "help": "Alternative/zusaetzliche Stellgroessen je Kreis/Speicher."},
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
    {
        "key": "ev_charger",
        "label": "Wallbox / Ladestation",
        "hints": list(discovery.EV_HINTS),
        "slots": [
            {"key": "power", "label": "Ladeleistung", "unit_group": "power",
             "multi": False, "required": True,
             "help": "Aktuelle Ladeleistung (ggf. von einem separaten Messgeraet)."},
            {"key": "energy", "label": "Geladene Energie", "unit_group": "energy",
             "multi": False, "required": False, "help": "kWh-Zaehler der Wallbox (optional)."},
            {"key": "current_setpoint", "label": "Ladestrom-Sollwert (Zahlen-Entitaet)",
             "unit_group": "number", "multi": False, "required": False, "control": True,
             "help": "Stellgroesse: max. Ladestrom (A)."},
            {"key": "switch", "label": "Laden an/aus", "unit_group": "switch",
             "multi": False, "required": False, "control": True,
             "help": "Stellgroesse: Ladevorgang schalten."},
        ],
    },
]

CATEGORY_KEYS = {c["key"] for c in CATEGORIES}

# Free, user-named consumers (a list, not a fixed category). Each entry has the
# same slot shape; they appear as extra nodes in the power-flow diagram.
CONSUMER_SLOTS: list[dict[str, Any]] = [
    {"key": "power", "label": "Leistung", "unit_group": "power",
     "multi": True, "required": True,
     "help": "Eigene Mess-Entitaet(en) des Verbrauchers (werden summiert)."},
    {"key": "energy", "label": "Energie", "unit_group": "energy",
     "multi": True, "required": False, "help": "kWh-Zaehler (optional)."},
]


def find_consumer_slot(slot_key: str) -> Optional[dict[str, Any]]:
    return next((s for s in CONSUMER_SLOTS if s["key"] == slot_key), None)


def find_category(category_key: str) -> Optional[dict[str, Any]]:
    return next((c for c in CATEGORIES if c["key"] == category_key), None)


def find_slot(category_key: str, slot_key: str) -> Optional[dict[str, Any]]:
    cat = find_category(category_key)
    if not cat:
        return None
    return next((s for s in cat["slots"] if s["key"] == slot_key), None)


def is_flag(category_key: str, key: str) -> bool:
    return key in CATEGORY_FLAGS.get(category_key, set())
