"""Strategy capability detection.

Derives which energy-saving strategies are *possible* from the configured
entities (universal app: not every user has every option). Each strategy lists
whether it is available, what is still missing, whether an execution engine
already exists, and whether it is currently active.
"""

from __future__ import annotations

from typing import Any

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
    ev = [i for k, i in ctrl if k == "ev_charger"]
    tariff = settings.get("tariff", {}) or {}
    dynamic = tariff.get("mode") == "dynamic" and bool(tariff.get("price_entity"))
    has_thermo = any(g.get("persons") and g.get("thermostats") for g in (groups or []))

    out: list[dict[str, Any]] = []

    def add(key, name, missing, engine_ready, active, desc):
        out.append({"key": key, "name": name, "available": not missing,
                    "missing": missing, "engine_ready": engine_ready,
                    "active": bool(active), "desc": desc})

    miss = []
    if not surplus:
        miss.append("PV + Netz konfigurieren")
    if not ctrl:
        miss.append("mind. einen steuerbaren Verbraucher (Schalt-/Sollwert-Entität)")
    add("self_consumption", "PV-Überschuss-Eigenverbrauch", miss, True,
        settings.get("control_enabled"),
        "Schaltbare/regelbare Lasten bei PV-Überschuss zuschalten.")

    miss = []
    if not surplus:
        miss.append("PV + Netz")
    if not ev:
        miss.append("Wallbox mit Steuerung")
    add("ev_surplus", "Wallbox PV-Überschussladen", miss, False, False,
        "E-Auto bevorzugt aus PV-Überschuss laden.")

    miss = []
    if not dynamic:
        miss.append("dynamischer Tarif mit Preis-Entität")
    if not ctrl:
        miss.append("steuerbare/verschiebbare Last")
    add("tariff_shift", "Dynamischer Tarif – Lastverschiebung", miss, False, False,
        "Verschiebbare Lasten in die günstigsten Stunden legen.")

    add("battery_opt", "Batterie-Optimierung",
        ["steuerbare Batterie-Entität (Lademodus/Ziel-SoC) – noch nicht konfigurierbar"],
        False, False, "Batterieladung an PV/Tarif ausrichten.")

    miss = [] if has_thermo else ["Thermostat-Gruppe mit Personen + Räumen anlegen"]
    add("setback", "Temperaturabsenkung bei Abwesenheit", miss, True,
        (settings.get("setback") or {}).get("enabled"),
        "Heizen absenken, wenn alle abwesend; vorausschauend vorheizen.")

    return out
