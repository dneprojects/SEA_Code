"""Entity discovery and energy-role classification.

Pure functions over HA states + registries, so they are easy to unit test
without a live connection. Produces a list of EnergyEntity *suggestions* - the
user later confirms/overrides these in the UI (curation).

Design goals (after first real-world run):
  * Be conservative. The HA instance contains a lot of config/diagnostic
    helper entities (LED indicators, flags, setpoints, timeouts, device battery
    levels). These are filtered out via the entity registry's entity_category.
  * Do not promote a power sensor to "battery" just because its name contains
    "Speicher" (that is a generic word for tank/storage and matched a
    power-to-heat device). Battery is detected via device_class instead.
  * switch/number/light are only treated as controllable consumers when there
    is an actual power relation (power unit or a linked power sensor), so plain
    setpoint numbers and indicator switches are not mistaken for loads.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .models import EnergyEntity, EnergyRole

_LOGGER = logging.getLogger(__name__)

# Units that indicate a power measurement.
POWER_UNITS = {"W", "kW", "MW"}
ENERGY_UNITS = {"Wh", "kWh", "MWh"}

# Name hints (lower-case substrings) per role.
PV_HINTS = ("pv", "solar", "photovolt", "wechselrichter", "erzeugung", "einspeis")
GRID_HINTS = ("grid", "netz", "bezug", "import", "export", "smartmeter", "smart meter")
HOUSE_HINTS = ("hausverbrauch", "gesamtverbrauch", "house load", "house consumption",
               "total load", "total consumption", "verbrauch gesamt", "hauslast")
# NOTE: deliberately NOT including "speicher" (means tank/storage in general and
# caused a power-to-heat device to be classified as battery).
BATTERY_HINTS = ("battery", "batterie", "akku")

# Controllable domains.
SWITCH_DOMAINS = {"switch", "input_boolean"}
DIMMABLE_DOMAINS = {"light", "fan", "climate"}
APPLIANCE_HINTS = ("washer", "wasch", "dryer", "trockner", "dishwasher", "spül", "spuel", "geschirr")

# Entity registry categories that are not energy-relevant loads/measurements.
SKIP_CATEGORIES = {"config", "diagnostic"}

# Default include threshold: measurement roles are included by default,
# controllable candidates require explicit user opt-in (curation).
INCLUDE_CONFIDENCE = 0.6


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else entity_id


def _name_matches(name: str, entity_id: str, hints: tuple[str, ...]) -> bool:
    hay = f"{name} {entity_id}".lower()
    return any(h in hay for h in hints)


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_state(
    state: dict[str, Any],
    meta: Optional[dict[str, Any]] = None,
    area_by_device: Optional[dict[str, str]] = None,
    area_names: Optional[dict[str, str]] = None,
) -> Optional[EnergyEntity]:
    """Classify a single HA state object into an EnergyEntity, or None.

    `meta` is the entity-registry record (entity_category, device_id,
    disabled_by, hidden_by, area_id).
    """
    entity_id = state.get("entity_id", "")
    if not entity_id:
        return None
    meta = meta or {}

    # Skip disabled/hidden and config/diagnostic helper entities.
    if meta.get("disabled_by") or meta.get("hidden_by"):
        return None
    entity_category = meta.get("entity_category")
    if entity_category in SKIP_CATEGORIES:
        return None

    attrs = state.get("attributes", {}) or {}
    domain = _domain(entity_id)
    name = attrs.get("friendly_name", entity_id)
    device_class = attrs.get("device_class")
    unit = attrs.get("unit_of_measurement")
    state_class = attrs.get("state_class")

    device_id = meta.get("device_id")
    area_id = meta.get("area_id") or (
        (area_by_device or {}).get(device_id) if device_id else None
    )
    area = (area_names or {}).get(area_id, area_id)

    role: Optional[str] = None
    confidence = 0.0
    reason = ""

    is_power = device_class == "power" or unit in POWER_UNITS
    is_energy = device_class == "energy" or unit in ENERGY_UNITS

    if device_class == "battery" and unit == "%":
        role, confidence, reason = EnergyRole.BATTERY, 0.8, "Batterie-Ladezustand (%)"
    elif is_power:
        if _name_matches(name, entity_id, HOUSE_HINTS):
            role, confidence, reason = EnergyRole.HOUSE_LOAD, 0.75, "Leistung + Hausverbrauch-Namensmuster"
        elif _name_matches(name, entity_id, PV_HINTS):
            role, confidence, reason = EnergyRole.PV, 0.8, "Leistung + PV-Namensmuster"
        elif _name_matches(name, entity_id, GRID_HINTS):
            role, confidence, reason = EnergyRole.GRID, 0.75, "Leistung + Netz-Namensmuster"
        elif _name_matches(name, entity_id, BATTERY_HINTS):
            role, confidence, reason = EnergyRole.BATTERY, 0.7, "Leistung + Batterie-Namensmuster"
        elif domain == "number":
            # A number with a power unit is a controllable power setpoint.
            role, confidence, reason = EnergyRole.CONSUMER_DIMMABLE, 0.6, "Regelbare Leistung (W)"
        else:
            role, confidence, reason = EnergyRole.POWER_SENSOR, 0.6, "Leistungssensor (W/kW)"
    elif is_energy:
        role, confidence, reason = EnergyRole.ENERGY_METER, 0.7, "Energiezähler (kWh)"
    elif domain in SWITCH_DOMAINS:
        # Real controllable output, but low confidence (could be an indicator).
        role, confidence, reason = EnergyRole.CONSUMER_SWITCH, 0.45, "Schaltbarer Ausgang"
    elif domain in DIMMABLE_DOMAINS:
        role, confidence, reason = EnergyRole.CONSUMER_DIMMABLE, 0.4, "Regelbare Domain"
    elif domain == "number":
        # Non-power numbers (temperature, timeouts, ...) are not loads -> skip.
        return None

    if role is None and _name_matches(name, entity_id, APPLIANCE_HINTS):
        role, confidence, reason = EnergyRole.APPLIANCE, 0.5, "Appliance-Namensmuster"

    if role is None:
        return None

    entity = EnergyEntity(
        entity_id=entity_id,
        friendly_name=name,
        role=role.value,
        domain=domain,
        device_class=device_class,
        unit=unit,
        state_class=state_class,
        area=area,
        device_id=device_id,
        entity_category=entity_category,
        state=state.get("state"),
        confidence=confidence,
        reason=reason,
        include=confidence >= INCLUDE_CONFIDENCE,
    )
    if is_power and domain != "number":
        pw = _to_float(state.get("state"))
        if pw is not None and unit == "kW":
            pw *= 1000.0
        entity.power_w = pw
    return entity


def link_power_sensors(entities: list[EnergyEntity]) -> None:
    """Heuristically link controllable loads to a power sensor on same device."""
    power_by_device: dict[str, str] = {}
    for e in entities:
        if e.role in (EnergyRole.POWER_SENSOR, EnergyRole.ENERGY_METER) and e.device_id:
            power_by_device.setdefault(e.device_id, e.entity_id)
    for e in entities:
        if e.role in (EnergyRole.CONSUMER_SWITCH, EnergyRole.CONSUMER_DIMMABLE, EnergyRole.APPLIANCE):
            if e.device_id and e.device_id in power_by_device:
                e.linked_power_entity = power_by_device[e.device_id]


def discover(
    states: list[dict[str, Any]],
    entity_registry: Optional[list[dict[str, Any]]] = None,
    device_registry: Optional[list[dict[str, Any]]] = None,
    area_registry: Optional[list[dict[str, Any]]] = None,
) -> list[EnergyEntity]:
    """Run full classification over a snapshot of states + registries."""
    meta_by_entity: dict[str, dict[str, Any]] = {}
    for ent in entity_registry or []:
        eid = ent.get("entity_id")
        if eid:
            meta_by_entity[eid] = ent

    area_by_device: dict[str, str] = {}
    for dev in device_registry or []:
        if dev.get("id") and dev.get("area_id"):
            area_by_device[dev["id"]] = dev["area_id"]

    area_names: dict[str, str] = {}
    for area in area_registry or []:
        if area.get("area_id"):
            area_names[area["area_id"]] = area.get("name", area["area_id"])

    result: list[EnergyEntity] = []
    for state in states:
        entity = classify_state(
            state,
            meta_by_entity.get(state.get("entity_id", "")),
            area_by_device,
            area_names,
        )
        if entity is not None:
            result.append(entity)

    link_power_sensors(result)
    n_incl = sum(1 for e in result if e.include)
    _LOGGER.info(
        "Discovery classified %d energy entities (%d included by default)",
        len(result), n_incl,
    )
    return result
