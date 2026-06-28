"""Strategy capability detection.

Derives which energy-saving strategies are *possible* from the configured
entities (universal app: not every user has every option). Each strategy lists
whether it is available, what is still missing, whether an execution engine
already exists, and whether it is currently active.
"""

from __future__ import annotations

from typing import Any

from . import tariff as tariff_mod

LOAD_KINDS = ("heat_pump", "water_heater", "ev_charger", "consumers")


def _has_pv(config: dict[str, Any]) -> bool:
    return any(isinstance(i, dict) and i.get("powers") for i in (config.get("pv") or []))


def _has_grid(config: dict[str, Any]) -> bool:
    g = config.get("grid") or {}
    return bool(g.get("power") or g.get("import_power") or g.get("export_power"))


def _controllable_loads(config: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for kind in LOAD_KINDS:
        for inst in config.get(kind) or []:
            if not isinstance(inst, dict):
                continue
            c = inst.get("control") or {}
            if c.get("switch") or c.get("setpoint"):
                out.append((kind, inst))
    return out


def overview(config: dict[str, Any], settings: dict[str, Any],
             groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    surplus = _has_pv(config) and _has_grid(config)
    ctrl = _controllable_loads(config)
    # Wallbox that can follow the surplus needs a modulating (setpoint) actuator.
    tariff = settings.get("tariff", {}) or {}
    price_source = tariff_mod.has_price_source(tariff)
    has_thermo = any(g.get("persons") and g.get("thermostats") for g in (groups or []))
    control_on = bool(settings.get("control_enabled"))
    tariff_on = bool(settings.get("tariff_enabled"))
    sl = settings.get("strategy_loads", {}) or {}

    out: list[dict[str, Any]] = []

    def add(key, name, missing, engine_ready, active, desc):
        out.append({"key": key, "name": name, "available": not missing,
                    "missing": missing, "engine_ready": engine_ready,
                    "active": bool(active), "desc": desc})

    # PV surplus: one strategy covering ALL controllable devices — switchable and
    # modulating loads, the wallbox AND the battery (storage) — by priority.
    miss = []
    if not surplus:
        miss.append("PV + Netz konfigurieren")
    if not ctrl:
        miss.append("mind. einen steuerbaren Verbraucher (Schalt-/Sollwert-Entität)")
    add("self_consumption", "PV-Überschuss: Eigenverbrauch und Speicherung", miss, True,
        control_on,
        "Alle steuerbaren Geräte – inkl. Wallbox und Batterie – folgen dem PV-Überschuss (nach Priorität).")

    miss = []
    if not price_source:
        miss.append("Preisquelle (dynamischer Tarif oder HT/NT-Fenster)")
    if not ctrl:
        miss.append("steuerbare/verschiebbare Last")
    add("tariff_shift", "Dynamischer Tarif: Lastverschiebung", miss, True,
        tariff_on and price_source and any(
            isinstance(v, dict) and v.get("tariff_shift") for v in sl.values()),
        "Verschiebbare Lasten und Speicher (Batterie/thermisch) in günstige Zeiten legen – "
        "besonders bei sehr niedrigen oder negativen Preisen.")

    miss = [] if has_thermo else ["Thermostat-Gruppe mit Personen + Räumen anlegen"]
    add("setback", "Temperaturabsenkung bei Abwesenheit", miss, True,
        (settings.get("setback") or {}).get("enabled"),
        "Heizen absenken, wenn alle abwesend; vorausschauend vorheizen.")

    return out
