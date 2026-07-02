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

    se = settings.get("strategy_enabled", {}) or {}

    def _en(key: str, configured: bool) -> bool:
        # per-strategy enable flag; if never set, default to "configured" (so a
        # strategy that already had a threshold stays on — backward compatible).
        return bool(se[key]) if key in se else configured

    out: list[dict[str, Any]] = []

    def add(key, name, missing, enabled, desc, configured=True):
        available = not missing
        out.append({"key": key, "name": name, "available": available,
                    "missing": missing, "engine_ready": True,
                    "enabled": bool(enabled),
                    # active = enabled AND prerequisites met AND actually configured
                    "active": bool(enabled and available and configured), "desc": desc})

    # PV surplus: one strategy covering ALL controllable devices — switchable and
    # modulating loads, the wallbox AND the battery (storage) — by priority.
    miss = []
    if not surplus:
        miss.append("PV + Netz konfigurieren")
    if not ctrl:
        miss.append("mind. einen steuerbaren Verbraucher (Schalt-/Sollwert-Entität)")
    add("self_consumption", "PV-Überschuss: Eigenverbrauch und Speicherung", miss,
        control_on,
        "Alle steuerbaren Geräte – inkl. Wallbox und Batterie – folgen dem PV-Überschuss (nach Priorität).")

    miss = []
    if not price_source:
        miss.append("Preisquelle (dynamischer Tarif oder HT/NT-Fenster)")
    if not ctrl:
        miss.append("steuerbare/verschiebbare Last")
    has_shift = any(isinstance(v, dict) and v.get("tariff_shift") for v in sl.values())
    add("tariff_shift", "Dynamischer Tarif: Lastverschiebung", miss, tariff_on,
        "Verschiebbare Lasten und Speicher (Batterie/thermisch) in günstige Zeiten legen – "
        "besonders bei sehr niedrigen oder negativen Preisen.", configured=has_shift)

    miss = [] if has_thermo else ["Thermostat-Gruppe mit Personen + Räumen anlegen"]
    add("setback", "Temperaturabsenkung bei Abwesenheit", miss,
        (settings.get("setback") or {}).get("enabled"),
        "Heizen absenken, wenn alle abwesend; vorausschauend vorheizen.")

    has_batt = bool(config.get("battery"))
    opt_on = bool(settings.get("optimizer_enabled"))
    miss = [] if has_batt else ["Batterie mit Lade-Sollwert konfigurieren"]
    add("optimizer", "Prognose-Optimierer (Batterie)", miss, opt_on,
        "Lädt/entlädt die Batterie vorausschauend nach PV-Prognose und Preis: günstig "
        "laden, teuer entladen, und netzoptimiert (kein Netzladen, wenn die PV den "
        "Speicher noch füllt). Reserve/Peak bleiben übergeordnet.")

    # Netz-/Speicherschutz-Dienste — Schwellwerte in der Strategiekarte, plus ein
    # separates Enable (Aktiv-Checkbox), das die Schwellwerte nicht verändert.
    def _on(v: Any) -> bool:
        try:
            return float(v or 0) > 0
        except (TypeError, ValueError):
            return False
    peak_cfg = _on(settings.get("peak_limit_w")) or bool(settings.get("peak_slots"))
    add("peak_shaving", "Peak-Shaving (Netzbezug deckeln)", [], _en("peak_shaving", peak_cfg),
        "Begrenzt den Netzbezug durch gezieltes Entladen der Batterie.", configured=peak_cfg)
    fi_cfg = _on(settings.get("feed_in_limit_w"))
    add("feed_in_limit", "Einspeise-Limit", [], _en("feed_in_limit", fi_cfg),
        "Begrenzt die Einspeisung durch Zwangsladen (und optional PV-Abregelung).", configured=fi_cfg)
    res_cfg = _on(settings.get("emergency_reserve_soc"))
    add("emergency_reserve", "Notstrom-Reserve", [], _en("emergency_reserve", res_cfg),
        "Hält die Batterie auf einer Backup-Reserve für Stromausfälle.", configured=res_cfg)
    care_cfg = _on(settings.get("soh_cycle_days"))
    add("battery_care", "Batteriepflege", [], _en("battery_care", care_cfg),
        "Periodische Vollladung der Batterie zur SoC-Kalibrierung.", configured=care_cfg)

    return out
