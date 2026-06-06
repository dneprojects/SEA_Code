"""Declarative, instance-based setup schema.

Every device genus except the grid is an *instance list*: the user adds named
instances (PV system, battery, heat pump, heating rod, wallbox, consumer),
each removable and renameable. The grid stays a single fixed section.

The schema drives the wizard UI, the full-config sanitiser, the suggestion
ranking and the category-grouped device view — so adding a genus or a field is
a data-only change here.

Field kinds:
  * ``entity``       – a single HA entity (unit_group filter)
  * ``named_power`` / ``named_energy`` – a named sub-list ``[{id,name,entity}]``
    (e.g. two heat pumps + an auxiliary heater)
  * ``circuits``     – ``[{id,name,temp,setpoint}]`` heating circuits
  * ``flag``         – a boolean (e.g. invert sign)
Instances of a ``control`` genus also carry a control block
``{mode: ''|'switch'|'setpoint', switch, setpoint}``.
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
    "switch": {"device_classes": set(), "units": set(), "domains": {"switch", "input_boolean"}},
    # Setpoint actuator: a number or a climate entity.
    "setpoint": {"device_classes": set(), "units": set(), "domains": {"number", "climate"}},
    # Writable number actuator (e.g. battery charge-power setpoint).
    "number": {"device_classes": set(), "units": set(), "domains": {"number", "input_number"}},
    "climate": {"device_classes": set(), "units": set(), "domains": {"climate"}},
    # Presence/occupancy source (person, device_tracker, zone, occupancy, ...).
    "presence": {"device_classes": {"occupancy", "presence", "motion"},
                 "units": set(),
                 "domains": {"person", "device_tracker", "zone", "input_boolean", "binary_sensor", "group"}},
    # Broad numeric value (stop conditions: vehicle SoC, temperature, ...).
    "value": {"device_classes": set(), "units": set(),
              "domains": {"sensor", "number", "input_number"}},
    # Electricity price sensor — universal: any monetary entity, OR a price-per-
    # kWh/MWh unit in common currencies, OR a number/input_number helper.
    "price": {"device_classes": {"monetary"},
              "units": {"ct/kWh", "Cent/kWh", "cent/kWh", "c/kWh", "¢/kWh", "p/kWh",
                        "EUR/kWh", "€/kWh", "EUR/MWh", "€/MWh", "GBP/kWh", "£/kWh",
                        "öre/kWh", "Öre/kWh", "SEK/kWh", "NOK/kWh", "DKK/kWh",
                        "USD/kWh", "$/kWh", "ct", "Cent", "EUR", "¢"},
              "domains": {"input_number", "number"}},
}

# Name hints for price entities (used by the price picker ranking).
PRICE_HINTS = ["preis", "price", "tarif", "strompreis", "epex", "spot", "tibber", "awattar"]

# Single, fixed grid section (not an instance list).
GRID = {
    "key": "grid",
    "label": "Netz (Bezug/Einspeisung)",
    "hints": list(discovery.GRID_HINTS),
    "fields": [
        {"key": "power", "kind": "entity", "unit_group": "power",
         "label": "Netzleistung (+ Bezug / − Einspeisung)"},
        {"key": "import_power", "kind": "entity", "unit_group": "power",
         "label": "Bezugsleistung (optional)", "optional": True},
        {"key": "export_power", "kind": "entity", "unit_group": "power",
         "label": "Einspeiseleistung (optional)", "optional": True},
        {"key": "invert", "kind": "flag", "label": "Vorzeichen umkehren"},
    ],
}

_POWERS = {"key": "powers", "kind": "named_power", "unit_group": "power",
           "label": "Leistung(en)", "item_label": "Leistung"}
_ENERGY = {"key": "energy", "kind": "named_energy", "unit_group": "energy",
           "label": "Energie", "item_label": "Energie", "optional": True}
_ENERGIES = {"key": "energies", "kind": "named_energy", "unit_group": "energy",
             "label": "Energie(n)", "item_label": "Energie", "optional": True}

INSTANCE_KINDS: list[dict[str, Any]] = [
    {"key": "pv", "label": "PV-Anlage", "add_label": "PV-Anlage hinzufügen",
     "anchor": "pv", "hints": list(discovery.PV_HINTS_DEV),
     "fields": [_POWERS, _ENERGY]},
    {"key": "battery", "label": "Batteriespeicher", "add_label": "Batteriespeicher hinzufügen",
     "anchor": "battery", "hints": list(discovery.BATTERY_HINTS_DEV),
     "fields": [
         {"key": "power", "kind": "entity", "unit_group": "power",
          "label": "Lade-/Entladeleistung (+ Laden / − Entladen)"},
         {"key": "soc", "kind": "entity", "unit_group": "soc",
          "label": "Ladezustand (SoC)", "optional": True},
         {"key": "charge_power", "kind": "entity", "unit_group": "number", "control": True,
          "label": "Ladeleistungs-Sollwert (regelbar, optional)", "optional": True,
          "help": "Zahlen-Entität für die Ladeleistung – Batterie nimmt PV-Überschuss auf."},
         {"key": "discharge_power", "kind": "entity", "unit_group": "number", "control": True,
          "label": "Entladeleistungs-Sollwert (erzwungen, optional)", "optional": True,
          "help": "Zahlen-Entität für erzwungenes Entladen – Batterie entlädt bei teurem Tarif."},
         {"key": "invert", "kind": "flag", "label": "Vorzeichen umkehren"},
     ]},
    {"key": "heat_pump", "label": "Wärmepumpe", "add_label": "Wärmepumpe hinzufügen",
     "load": True, "control": True, "hints": list(discovery.HEATPUMP_HINTS),
     "fields": [
         _POWERS, _ENERGIES,
         {"key": "circuits", "kind": "circuits", "label": "Heizkreise", "optional": True},
     ]},
    {"key": "water_heater", "label": "Heizstab / Warmwasser", "add_label": "Heizstab hinzufügen",
     "load": True, "control": True, "hints": list(discovery.WATERHEATER_HINTS),
     "fields": [_POWERS, _ENERGIES]},
    {"key": "ev_charger", "label": "Wallbox / Ladestation", "add_label": "Wallbox hinzufügen",
     "load": True, "control": True, "hints": list(discovery.EV_HINTS),
     "fields": [_POWERS, _ENERGY]},
    {"key": "consumers", "label": "Verbraucher", "add_label": "Verbraucher hinzufügen",
     "load": True, "control": True, "hints": [],
     "fields": [_POWERS, _ENERGY]},
]

# Heating-circuit sub-fields (used by the 'circuits' field kind).
CIRCUIT_FIELDS = [
    {"key": "temp", "kind": "entity", "unit_group": "temperature",
     "label": "Temperatur", "optional": True},
    {"key": "setpoint", "kind": "entity", "unit_group": "setpoint",
     "label": "Sollwert", "optional": True, "control": True},
]

KIND_KEYS = {k["key"] for k in INSTANCE_KINDS}


def find_kind(key: str) -> Optional[dict[str, Any]]:
    return next((k for k in INSTANCE_KINDS if k["key"] == key), None)


def kind_hints(key: str) -> list[str]:
    if key == "grid":
        return GRID["hints"]
    if key == "price":
        return PRICE_HINTS
    k = find_kind(key)
    return k.get("hints", []) if k else []
