"""Tests for the setup-wizard suggestion engine and energy-prefs prefill."""

from __future__ import annotations

from smart_energy_agent import setup_catalog
from smart_energy_agent.suggest import (
    prefill_from_prefs,
    prefs_entity_set,
    rank_for_slot,
)


def _state(eid, name, *, dc=None, unit=None, state="0", device=None):
    return {"entity_id": eid, "state": state,
            "attributes": {"friendly_name": name, "device_class": dc,
                           "unit_of_measurement": unit}}, \
           {"entity_id": eid, "device_id": device}


def _build():
    pairs = [
        _state("sensor.wp_leistung", "Wärmepumpe Leistung", dc="power", unit="W",
               state="900", device="dev_wp"),
        _state("sensor.shelly_pm3_ch2", "Shelly PM3 Kanal 2", dc="power", unit="W",
               state="850", device="dev_shelly"),
        _state("sensor.pv_ac_power", "PV AC Leistung", dc="power", unit="W",
               state="3200", device="dev_pv"),
        _state("sensor.wohnzimmer_temp", "Wohnzimmer Temperatur", dc="temperature",
               unit="°C", state="21", device="dev_th"),
    ]
    states = [s for s, _m in pairs]
    ent_reg = [m for _s, m in pairs]
    dev_reg = [
        {"id": "dev_wp", "name": "Wärmepumpe"},
        {"id": "dev_shelly", "name": "Shelly PM3"},
        {"id": "dev_pv", "name": "Wechselrichter"},
        {"id": "dev_th", "name": "Thermostat"},
    ]
    return states, ent_reg, dev_reg, []


def test_power_slot_hard_filters_non_power() -> None:
    states, ent_reg, dev_reg, area_reg = _build()
    slot = setup_catalog.find_slot("heat_pump", "power")
    cands = rank_for_slot(states, ent_reg, dev_reg, area_reg, slot=slot)
    ids = {c["entity_id"] for c in cands}
    assert "sensor.wohnzimmer_temp" not in ids  # temperature excluded
    assert "sensor.wp_leistung" in ids
    assert "sensor.shelly_pm3_ch2" in ids  # foreign device still a candidate


def test_heatpump_name_hint_ranks_first() -> None:
    states, ent_reg, dev_reg, area_reg = _build()
    cat = setup_catalog.find_category("heat_pump")
    slot = setup_catalog.find_slot("heat_pump", "power")
    cands = rank_for_slot(states, ent_reg, dev_reg, area_reg, slot=slot,
                          category_hints=cat["hints"])
    assert cands[0]["entity_id"] == "sensor.wp_leistung"


def test_prefs_entity_boosted_to_top() -> None:
    states, ent_reg, dev_reg, area_reg = _build()
    cat = setup_catalog.find_category("heat_pump")
    slot = setup_catalog.find_slot("heat_pump", "power")
    # The Shelly channel has no name hint, but is the one wired in the dashboard.
    cands = rank_for_slot(states, ent_reg, dev_reg, area_reg, slot=slot,
                          category_hints=cat["hints"],
                          prefs_entities={"sensor.shelly_pm3_ch2"})
    assert cands[0]["entity_id"] == "sensor.shelly_pm3_ch2"
    assert "Energy-Dashboard" in cands[0]["reason"]


def test_query_filters_by_name() -> None:
    states, ent_reg, dev_reg, area_reg = _build()
    slot = setup_catalog.find_slot("pv", "power")
    cands = rank_for_slot(states, ent_reg, dev_reg, area_reg, slot=slot, query="shelly")
    assert [c["entity_id"] for c in cands] == ["sensor.shelly_pm3_ch2"]


PREFS = {
    "energy_sources": [
        {"type": "solar", "stat_energy_from": "sensor.pv_today",
         "stat_rate": "sensor.pv_power", "config_entry_solar_forecast": ["x"]},
        {"type": "grid", "stat_energy_from": "sensor.imp", "stat_energy_to": "sensor.exp",
         "stat_rate": "sensor.grid_power", "entity_energy_price": "sensor.price"},
        {"type": "battery", "stat_rate": "sensor.batt_power", "stat_soc": "sensor.batt_soc",
         "stat_energy_from": "sensor.batt_out", "stat_energy_to": "sensor.batt_in"},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.hp_energy", "stat_rate": "sensor.hp_power",
         "name": "Wärmepumpe EG"},
        {"stat_consumption": "sensor.wb_energy", "stat_rate": "sensor.wb_power",
         "name": "Wallbox Garage"},
    ],
}


def test_prefill_from_prefs_maps_all_slots() -> None:
    out = prefill_from_prefs(PREFS)
    assert out["pv"]["power"] == ["sensor.pv_power"]
    assert out["pv"]["energy_today"] == "sensor.pv_today"
    assert out["grid"]["power"] == "sensor.grid_power"
    assert out["tariff"]["price_entity"] == "sensor.price"
    assert out["heat_pump"]["power"] == ["sensor.hp_power"]  # multi -> list
    assert out["heat_pump"]["energy"] == ["sensor.hp_energy"]
    assert out["battery"]["power"] == "sensor.batt_power"
    assert out["battery"]["soc"] == "sensor.batt_soc"
    assert out["ev_charger"]["power"] == "sensor.wb_power"
    assert out["ev_charger"]["energy"] == "sensor.wb_energy"


def test_prefill_heatpump_multi_accumulates() -> None:
    prefs = {"device_consumption": [
        {"name": "Wärmepumpe 1", "stat_rate": "sensor.hp1_p", "stat_consumption": "sensor.hp1_e"},
        {"name": "Wärmepumpe 2", "stat_rate": "sensor.hp2_p", "stat_consumption": "sensor.hp2_e"},
    ]}
    out = prefill_from_prefs(prefs)
    assert out["heat_pump"]["power"] == ["sensor.hp1_p", "sensor.hp2_p"]
    assert out["heat_pump"]["energy"] == ["sensor.hp1_e", "sensor.hp2_e"]


def test_prefill_legacy_flow_grid() -> None:
    prefs = {"energy_sources": [{
        "type": "grid",
        "flow_from": [{"stat_energy_from": "sensor.imp", "entity_energy_price": "sensor.p"}],
        "flow_to": [{"stat_energy_to": "sensor.exp"}],
    }]}
    out = prefill_from_prefs(prefs)
    assert out["tariff"]["price_entity"] == "sensor.p"


def test_prefs_entity_set_collects_all() -> None:
    s = prefs_entity_set(PREFS)
    assert {"sensor.pv_power", "sensor.grid_power", "sensor.price",
            "sensor.hp_power", "sensor.hp_energy"} <= s


def test_prefill_empty() -> None:
    assert prefill_from_prefs(None) == {
        "pv": {}, "battery": {}, "grid": {}, "heat_pump": {}, "ev_charger": {}, "tariff": {}}
