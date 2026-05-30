"""Data model for classified energy entities."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


class EnergyRole(str, Enum):
    """The energy role an entity plays in the household."""

    PV = "pv"                      # PV / solar generation
    GRID = "grid"                  # grid import/export (smart meter)
    BATTERY = "battery"            # storage SoC / power
    HOUSE_LOAD = "house_load"      # total household consumption (balance anchor)
    POWER_SENSOR = "power_sensor"  # generic power measurement (W/kW)
    ENERGY_METER = "energy_meter"  # energy counter (Wh/kWh)
    CONSUMER_SWITCH = "consumer_switch"      # on/off controllable load
    CONSUMER_DIMMABLE = "consumer_dimmable"  # modulating controllable load
    APPLIANCE = "appliance"        # smart appliance with program state
    UNKNOWN = "unknown"


# Human-readable group labels (German UI).
ROLE_LABELS: dict[str, str] = {
    EnergyRole.PV: "PV-Erzeugung",
    EnergyRole.GRID: "Netz (Bezug/Einspeisung)",
    EnergyRole.BATTERY: "Batterie/Speicher",
    EnergyRole.HOUSE_LOAD: "Hausverbrauch (gesamt)",
    EnergyRole.POWER_SENSOR: "Leistungssensoren",
    EnergyRole.ENERGY_METER: "Energiezähler",
    EnergyRole.CONSUMER_SWITCH: "Schaltbare Verbraucher",
    EnergyRole.CONSUMER_DIMMABLE: "Regelbare Verbraucher",
    EnergyRole.APPLIANCE: "Smart Appliances",
    EnergyRole.UNKNOWN: "Nicht zugeordnet",
}

# Display order for the UI.
ROLE_ORDER: list[str] = [
    EnergyRole.PV,
    EnergyRole.GRID,
    EnergyRole.BATTERY,
    EnergyRole.HOUSE_LOAD,
    EnergyRole.POWER_SENSOR,
    EnergyRole.ENERGY_METER,
    EnergyRole.CONSUMER_SWITCH,
    EnergyRole.CONSUMER_DIMMABLE,
    EnergyRole.APPLIANCE,
]


# --- Managed consumer configuration (preparation for control, phase 3) -------
# Roles that can be configured as controllable consumers.
CONSUMER_ROLES = (
    EnergyRole.CONSUMER_SWITCH,
    EnergyRole.CONSUMER_DIMMABLE,
    EnergyRole.APPLIANCE,
)

CONSUMER_TYPES = [
    {"value": "simple_switch", "label": "Einfacher Schalter"},
    {"value": "dimmable", "label": "Regelbar"},
    {"value": "ev_charger", "label": "Wallbox / E-Auto"},
    {"value": "water_heater", "label": "Warmwasser / Heizstab"},
    {"value": "heatpump", "label": "Wärmepumpe"},
    {"value": "appliance", "label": "Haushaltsgerät"},
]

CONTROL_MODES = [
    {"value": "off", "label": "Aus (ignorieren)"},
    {"value": "monitor", "label": "Nur beobachten"},
    {"value": "auto", "label": "Automatisch"},
    {"value": "dialog", "label": "Dialog (nachfragen)"},
    {"value": "scheduled", "label": "Geplant"},
]

COMBINE_MODES = [
    {"value": "or", "label": "ODER (PV oder günstig)"},
    {"value": "and", "label": "UND (PV und günstig)"},
]

# Default per-consumer configuration. Persisted as JSON keyed by control entity.
DEFAULT_CONSUMER: dict[str, Any] = {
    "type": "simple_switch",
    "control_mode": "monitor",
    "nominal_power_w": 0,
    "priority": 5,
    "earliest_start": "",      # "HH:MM" comfort window
    "latest_finish": "",       # "HH:MM" deadline
    "min_runtime_min": 0,
    "max_starts_per_day": 0,
    "min_off_min": 0,
    "required_kwh": 0.0,       # e.g. EV charge goal
    "deferrable": True,
    "interruptible": False,
    "pv_surplus_threshold_w": 0,
    "price_threshold_ct": 0.0,
    "soc_min_pct": 0,
    "combine": "or",
}

# Value types used to coerce incoming JSON for each config field.
CONSUMER_FIELD_TYPES: dict[str, type] = {
    "nominal_power_w": int, "priority": int, "min_runtime_min": int,
    "max_starts_per_day": int, "min_off_min": int, "pv_surplus_threshold_w": int,
    "soc_min_pct": int, "required_kwh": float, "price_threshold_ct": float,
    "deferrable": bool, "interruptible": bool,
}


@dataclass
class EnergyEntity:
    """A Home Assistant entity that Smart Energy Agent considers energy-relevant."""

    entity_id: str
    friendly_name: str
    role: str
    domain: str
    device_class: Optional[str] = None
    unit: Optional[str] = None
    state_class: Optional[str] = None
    area: Optional[str] = None
    device_id: Optional[str] = None
    entity_category: Optional[str] = None  # None | "config" | "diagnostic"
    # Live values, updated from state_changed events.
    state: Optional[str] = None
    power_w: Optional[float] = None
    # Heuristic linkage: controllable load -> its power sensor.
    linked_power_entity: Optional[str] = None
    # Confidence of the classification (0..1) and a short reason for the UI.
    confidence: float = 0.0
    reason: str = ""
    # Whether this entity is included in the energy model. Default derives from
    # confidence; the user can override it (curation), see store.overrides.
    include: bool = False
    # True if include/role came from a user override (not the auto-default).
    overridden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- Device-centric model (primary unit of discovery) ------------------------
class DeviceType(str, Enum):
    BATTERY = "battery"            # storage: SoC + charge/discharge power
    PV = "pv"                      # PV inverter / generation
    GRID = "grid"                  # grid connection / smart meter
    HEAT_PUMP = "heat_pump"        # heat pump (thermal store via setpoints)
    WATER_HEATER = "water_heater"  # heating rod / hot water
    WASHING_MACHINE = "washing_machine"
    DRYER = "dryer"
    DISHWASHER = "dishwasher"
    EV_CHARGER = "ev_charger"      # wallbox / EV
    CONSUMER = "consumer"          # other controllable load
    OTHER = "other"


DEVICE_TYPE_LABELS: dict[str, str] = {
    DeviceType.BATTERY: "Batteriespeicher",
    DeviceType.PV: "PV-Anlage",
    DeviceType.GRID: "Netzanschluss",
    DeviceType.HEAT_PUMP: "Wärmepumpe",
    DeviceType.WATER_HEATER: "Heizstab / Warmwasser",
    DeviceType.WASHING_MACHINE: "Waschmaschine",
    DeviceType.DRYER: "Wäschetrockner",
    DeviceType.DISHWASHER: "Geschirrspüler",
    DeviceType.EV_CHARGER: "Wallbox / E-Auto",
    DeviceType.CONSUMER: "Sonstiger Verbraucher",
    DeviceType.OTHER: "Sonstiges Gerät",
}

DEVICE_TYPE_ORDER: list[str] = [
    DeviceType.PV, DeviceType.BATTERY, DeviceType.GRID, DeviceType.HEAT_PUMP,
    DeviceType.WATER_HEATER, DeviceType.EV_CHARGER, DeviceType.WASHING_MACHINE,
    DeviceType.DRYER, DeviceType.DISHWASHER, DeviceType.CONSUMER, DeviceType.OTHER,
]

# Device types that are included in the energy model by default.
DEVICE_DEFAULT_INCLUDE = {
    DeviceType.PV, DeviceType.BATTERY, DeviceType.GRID, DeviceType.HEAT_PUMP,
    DeviceType.WATER_HEATER, DeviceType.EV_CHARGER, DeviceType.WASHING_MACHINE,
    DeviceType.DRYER, DeviceType.DISHWASHER,
}


# Sub-roles an entity plays within its device.
class SubRole(str, Enum):
    POWER = "power"
    ENERGY = "energy"
    SOC = "soc"
    TEMPERATURE = "temperature"
    CLIMATE = "climate"
    SWITCH = "switch"
    SETPOINT = "setpoint"
    PROGRAM = "program"
    OTHER = "other"


SUBROLE_LABELS: dict[str, str] = {
    SubRole.POWER: "Leistung", SubRole.ENERGY: "Energie", SubRole.SOC: "Ladezustand",
    SubRole.TEMPERATURE: "Temperatur", SubRole.CLIMATE: "Klima/Thermostat",
    SubRole.SWITCH: "Schalter", SubRole.SETPOINT: "Sollwert",
    SubRole.PROGRAM: "Programm/Status", SubRole.OTHER: "Sonstige",
}

SUBROLE_ORDER: list[str] = [
    SubRole.POWER, SubRole.ENERGY, SubRole.SOC, SubRole.TEMPERATURE,
    SubRole.CLIMATE, SubRole.SETPOINT, SubRole.SWITCH, SubRole.PROGRAM, SubRole.OTHER,
]


@dataclass
class Device:
    """A Home Assistant device with its grouped, sub-classified entities."""

    device_id: str
    name: str
    device_type: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    area: Optional[str] = None
    integration: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""
    include: bool = False
    overridden: bool = False
    # Each entry: {entity_id, friendly_name, sub_role, domain, device_class,
    #              unit, state, power_w, entity_category}
    entities: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
