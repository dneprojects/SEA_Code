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

from .models import (
    EnergyEntity, EnergyRole,
    Device, DeviceType, SubRole, DEVICE_DEFAULT_INCLUDE,
)

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
    area = (area_names or {}).get(area_id, area_id) if area_id is not None else None

    role: Optional[EnergyRole] = None
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


# --- device-centric discovery -----------------------------------------------
TEMP_UNITS = {"°C", "°F"}

WASH_HINTS = ("washer", "washing", "wasch")
DRYER_HINTS = ("dryer", "trockner", "tumble")
DISH_HINTS = ("dishwash", "geschirr", "spül", "spuel")
HEATPUMP_HINTS = ("heat pump", "heatpump", "wärmepump", "waermepump", "aquarea",
                  "daikin", "nibe", "viessmann", "vaillant", "stiebel")
WATERHEATER_HINTS = ("heizstab", "boiler", "warmwasser", "water heater", "dhw",
                     "immersion", "elwa")
EV_HINTS = ("wallbox", "wall box", "charger", "evse", "go-e", "goe", "keba",
            "easee", "wallbe", "ev charge", "openevse", "ladestation")
# Identifiers for the *vehicle* itself (reports its own state of charge), as
# opposed to the wallbox/charger. Used together with an SoC sub-role.
VEHICLE_HINTS = ("vehicle", "fahrzeug", "kfz", "e-auto", "elektroauto", "car battery",
                 "tesla", "polestar", "ioniq", "enyaq", "renault zoe", " zoe",
                 "leaf", "model 3", "model y", "model s", "model x",
                 "id.3", "id.4", "id.5", "id.7", "bev", "kona electric",
                 "e-up", "mg4", "smart car", "we connect", "myskoda")
PV_HINTS_DEV = ("pv", "solar", "photovolt", "inverter", "wechselrichter",
                "fronius", "solaredge", "kostal", "huawei", "growatt")
GRID_HINTS_DEV = ("grid", "netz", "smartmeter", "smart meter", "powermeter",
                  "stromzähler", "stromzaehler", "shelly em", "shelly 3em")
BATTERY_HINTS_DEV = ("battery", "batterie", "akku", "speicher", "sonnen",
                     "powerwall", "byd", "victron", "pylontech")
PROGRAM_HINTS = ("program", "programm", "status", "phase", "remaining",
                 "restzeit", "door", "tür", "cycle", "betrieb")


def _subrole(domain: str, device_class: Optional[str], unit: Optional[str],
             name: str, entity_category: Optional[str] = None) -> str:
    # Config/diagnostic entities (device battery level, signal strength, ...)
    # are not energy-relevant and must not drive sub-role or type detection.
    if entity_category in ("config", "diagnostic"):
        return SubRole.OTHER
    # Controllable domains take priority over measurement units: a `number`
    # with unit °C is a target-temperature setpoint, not a temperature reading.
    if domain == "climate":
        return SubRole.CLIMATE
    if domain == "number":
        return SubRole.SETPOINT
    if domain in SWITCH_DOMAINS:
        return SubRole.SWITCH
    if domain == "select":
        return SubRole.PROGRAM
    if device_class == "battery" and unit == "%":
        return SubRole.SOC
    if device_class == "power" or unit in POWER_UNITS:
        return SubRole.POWER
    if device_class == "energy" or unit in ENERGY_UNITS:
        return SubRole.ENERGY
    if device_class == "temperature" or unit in TEMP_UNITS:
        return SubRole.TEMPERATURE
    if domain == "sensor" and any(h in name.lower() for h in PROGRAM_HINTS):
        return SubRole.PROGRAM
    return SubRole.OTHER


def classify_device(meta: dict[str, Any], ents: list[dict[str, Any]]) -> tuple[DeviceType, float, str]:
    """Determine the semantic device type from registry meta + entity composition."""
    parts = [meta.get("name"), meta.get("model"), meta.get("manufacturer"), meta.get("integration")]
    # Only let non-diagnostic/config entity names influence the name matching.
    named = [e for e in ents if e.get("entity_category") not in ("config", "diagnostic")]
    parts += [e["friendly_name"] for e in named] + [e["entity_id"] for e in named]
    hay = " ".join(p for p in parts if p).lower()

    roles = {e["sub_role"] for e in ents}
    has_soc = SubRole.SOC in roles
    has_power = SubRole.POWER in roles
    has_energy = SubRole.ENERGY in roles
    has_temp = SubRole.TEMPERATURE in roles
    has_climate = SubRole.CLIMATE in roles
    has_switch = SubRole.SWITCH in roles
    has_number = SubRole.SETPOINT in roles

    def m(hints: tuple[str, ...]) -> bool:
        return any(h in hay for h in hints)

    if m(WASH_HINTS):
        return DeviceType.WASHING_MACHINE, 0.8, "Namensmuster Waschmaschine"
    if m(DRYER_HINTS):
        return DeviceType.DRYER, 0.8, "Namensmuster Trockner"
    if m(DISH_HINTS):
        return DeviceType.DISHWASHER, 0.8, "Namensmuster Geschirrspüler"
    # Electric vehicle: its own state of charge + a vehicle identifier. Checked
    # before the home battery so a car's SoC is not mistaken for a stationary
    # storage. A charger without SoC stays an EV charger (below).
    if has_soc and m(VEHICLE_HINTS):
        return DeviceType.VEHICLE, 0.85, "Fahrzeug mit Ladezustand"
    if has_soc and (has_power or m(BATTERY_HINTS_DEV)):
        return DeviceType.BATTERY, 0.85, "Ladezustand + Leistung"
    if m(BATTERY_HINTS_DEV) and (has_power or has_soc):
        return DeviceType.BATTERY, 0.75, "Namensmuster Batterie"
    # Heat pump: a name hint, OR climate/temperature combined with a real
    # electrical load (power/energy). Climate+temperature *without* power is a
    # room thermostat (radiator/floor valve), handled separately below.
    if m(HEATPUMP_HINTS):
        return DeviceType.HEAT_PUMP, 0.8, "Namensmuster Wärmepumpe"
    if has_climate and has_temp and (has_power or has_energy):
        return DeviceType.HEAT_PUMP, 0.6, "Klima + Temperatur + Leistung"
    if m(WATERHEATER_HINTS) or (has_temp and has_switch):
        return DeviceType.WATER_HEATER, 0.8 if m(WATERHEATER_HINTS) else 0.55, "Heizstab/Warmwasser"
    if m(EV_HINTS):
        return DeviceType.EV_CHARGER, 0.8, "Namensmuster Wallbox"
    if m(PV_HINTS_DEV) and (has_power or has_energy):
        return DeviceType.PV, 0.8, "PV-Namensmuster + Leistung"
    if m(GRID_HINTS_DEV) and has_power:
        return DeviceType.GRID, 0.75, "Netz-Namensmuster + Leistung"
    # Room thermostat: a climate entity (or temperature setpoint) with no
    # electrical load. Useful for absence setback, but not a heat pump/battery.
    if has_climate or (has_temp and has_number and not has_power):
        return DeviceType.THERMOSTAT, 0.6, "Raumthermostat (Klima ohne Last)"
    if has_switch or has_number:
        return DeviceType.CONSUMER, 0.5, "Schalt-/regelbares Gerät"
    return DeviceType.OTHER, 0.3, "nicht eindeutig"


def discover_devices(
    states: list[dict[str, Any]],
    entity_registry: Optional[list[dict[str, Any]]] = None,
    device_registry: Optional[list[dict[str, Any]]] = None,
    area_registry: Optional[list[dict[str, Any]]] = None,
) -> list[Device]:
    """Group entities by HA device and classify each device semantically."""
    state_by_id = {s.get("entity_id"): s for s in states if s.get("entity_id")}
    meta_by_entity = {e["entity_id"]: e for e in (entity_registry or []) if e.get("entity_id")}
    area_names = {
        a["area_id"]: a.get("name", a["area_id"])
        for a in (area_registry or []) if a.get("area_id")
    }
    devmeta = {d["id"]: d for d in (device_registry or []) if d.get("id")}

    ents_by_dev: dict[str, list[dict[str, Any]]] = {}
    for eid, em in meta_by_entity.items():
        did = em.get("device_id")
        if not did or em.get("disabled_by") or em.get("hidden_by"):
            continue
        st = state_by_id.get(eid, {})
        attrs = st.get("attributes", {}) or {}
        domain = _domain(eid)
        dc = attrs.get("device_class")
        unit = attrs.get("unit_of_measurement")
        name = attrs.get("friendly_name", eid)
        sr = _subrole(domain, dc, unit, name, em.get("entity_category"))
        ent = {
            "entity_id": eid, "friendly_name": name, "sub_role": sr,
            "domain": domain, "device_class": dc, "unit": unit,
            "state": st.get("state"), "power_w": None,
            "entity_category": em.get("entity_category"),
        }
        if sr == SubRole.SOC or sr == SubRole.POWER:
            pass
        if sr == SubRole.POWER:
            pw = _to_float(st.get("state"))
            if pw is not None and unit == "kW":
                pw *= 1000.0
            ent["power_w"] = pw
        ents_by_dev.setdefault(did, []).append(ent)

    relevant_roles = {
        SubRole.POWER, SubRole.ENERGY, SubRole.SOC, SubRole.CLIMATE,
        SubRole.SWITCH, SubRole.SETPOINT, SubRole.PROGRAM,
    }
    devices: list[Device] = []
    for did, ents in ents_by_dev.items():
        dm = devmeta.get(did, {})
        integration = None
        ids = dm.get("identifiers")
        if isinstance(ids, list) and ids and isinstance(ids[0], (list, tuple)) and ids[0]:
            integration = ids[0][0]
        if not integration:
            integration = meta_by_entity.get(ents[0]["entity_id"], {}).get("platform")
        name = dm.get("name_by_user") or dm.get("name") or ents[0]["friendly_name"]
        area = area_names.get(dm.get("area_id"), dm.get("area_id"))
        meta = {
            "name": name, "model": dm.get("model"),
            "manufacturer": dm.get("manufacturer"), "integration": integration,
        }
        dtype, conf, reason = classify_device(meta, ents)
        relevant = any(
            e["sub_role"] in relevant_roles
            and e["entity_category"] not in ("config", "diagnostic")
            for e in ents
        )
        if dtype == DeviceType.OTHER and not relevant:
            continue
        devices.append(Device(
            device_id=did, name=name, device_type=dtype.value,
            manufacturer=dm.get("manufacturer"), model=dm.get("model"),
            area=area, integration=integration, confidence=conf, reason=reason,
            include=(dtype in DEVICE_DEFAULT_INCLUDE),
            entities=sorted(ents, key=lambda e: str(e["sub_role"])),
        ))
    devices.sort(key=lambda d: (d.name or "").lower())
    _LOGGER.info("Discovery found %d devices", len(devices))
    return devices
