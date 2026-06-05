"""Tests for the suggestion engine and instance-based energy-prefs prefill."""

from __future__ import annotations

from smart_energy_agent import setup_catalog
from smart_energy_agent.suggest import (
    derive_on_device,
    prefill_from_prefs,
    prefs_entity_set,
    rank_for_slot,
)

POWER = {"unit_group": "power"}


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
    cands = rank_for_slot(states, ent_reg, dev_reg, area_reg, slot=POWER)
    ids = {c["entity_id"] for c in cands}
    assert "sensor.wohnzimmer_temp" not in ids   # temperature excluded
    assert "sensor.wp_leistung" in ids
    assert "sensor.shelly_pm3_ch2" in ids        # foreign device still a candidate


def test_heatpump_name_hint_ranks_first() -> None:
    states, ent_reg, dev_reg, area_reg = _build()
    cands = rank_for_slot(states, ent_reg, dev_reg, area_reg, slot=POWER,
                          category_hints=setup_catalog.kind_hints("heat_pump"))
    assert cands[0]["entity_id"] == "sensor.wp_leistung"


def test_prefs_entity_boosted_to_top() -> None:
    states, ent_reg, dev_reg, area_reg = _build()
    cands = rank_for_slot(states, ent_reg, dev_reg, area_reg, slot=POWER,
                          category_hints=setup_catalog.kind_hints("heat_pump"),
                          prefs_entities={"sensor.shelly_pm3_ch2"})
    assert cands[0]["entity_id"] == "sensor.shelly_pm3_ch2"
    assert "Energy-Dashboard" in cands[0]["reason"]


def test_diagnostic_entities_are_included() -> None:
    # Heat-pump powers are often diagnostic entities — they must still appear.
    state = {"entity_id": "sensor.wp1_power", "state": "500",
             "attributes": {"friendly_name": "WP1 Leistung", "device_class": "power",
                            "unit_of_measurement": "W"}}
    meta = {"entity_id": "sensor.wp1_power", "device_id": "d", "entity_category": "diagnostic"}
    cands = rank_for_slot([state], [meta], [], [], slot=POWER, query="wp1")
    assert [c["entity_id"] for c in cands] == ["sensor.wp1_power"]
    assert "Diagnose" in cands[0]["reason"]


def test_query_filters_by_name() -> None:
    states, ent_reg, dev_reg, area_reg = _build()
    cands = rank_for_slot(states, ent_reg, dev_reg, area_reg, slot=POWER, query="shelly")
    assert [c["entity_id"] for c in cands] == ["sensor.shelly_pm3_ch2"]


def _prefix_fixture():
    pairs = [
        _state("sensor.pv_ac_power", "PV AC", dc="power", unit="W"),
        _state("sensor.pv_dc_power", "PV DC", dc="power", unit="W"),
        _state("sensor.wp_leistung", "WP", dc="power", unit="W"),
    ]
    return [s for s, _m in pairs], [m for _s, m in pairs]


def test_current_restricts_to_same_name_prefix() -> None:
    states, ent_reg = _prefix_fixture()
    cands = rank_for_slot(states, ent_reg, [], [], slot=POWER,
                          current="sensor.pv_ac_power")
    # only entities sharing the "pv" leading token survive
    assert {c["entity_id"] for c in cands} == {"sensor.pv_ac_power", "sensor.pv_dc_power"}
    assert all("gleicher Namensprefix" in c["reason"] for c in cands)


def test_current_prefix_lifted_by_search_query() -> None:
    states, ent_reg = _prefix_fixture()
    # an explicit search overrides the prefix restriction so other families show
    cands = rank_for_slot(states, ent_reg, [], [], slot=POWER,
                          current="sensor.pv_ac_power", query="wp")
    assert [c["entity_id"] for c in cands] == ["sensor.wp_leistung"]


def test_no_current_returns_all() -> None:
    states, ent_reg = _prefix_fixture()
    cands = rank_for_slot(states, ent_reg, [], [], slot=POWER)
    assert len(cands) == 3


# --- prefill (energy dashboard -> instances) --------------------------------

PREFS = {
    "energy_sources": [
        {"type": "solar", "stat_energy_from": "sensor.pv_today", "stat_rate": "sensor.pv_power"},
        {"type": "grid", "stat_rate": "sensor.grid_power", "entity_energy_price": "sensor.price"},
        {"type": "battery", "stat_rate": "sensor.batt_power", "stat_soc": "sensor.batt_soc"},
    ],
    "device_consumption": [
        {"stat_consumption": "sensor.hp_energy", "stat_rate": "sensor.hp_power", "name": "Wärmepumpe EG"},
        {"stat_consumption": "sensor.wb_energy", "stat_rate": "sensor.wb_power", "name": "Wallbox Garage"},
    ],
}


def test_prefill_creates_instances() -> None:
    out = prefill_from_prefs(PREFS)
    assert out["pv"][0]["powers"][0]["entity"] == "sensor.pv_power"
    assert out["pv"][0]["energy"][0]["entity"] == "sensor.pv_today"
    assert out["battery"][0]["power"] == "sensor.batt_power"
    assert out["battery"][0]["soc"] == "sensor.batt_soc"
    assert out["grid"]["power"] == "sensor.grid_power"
    assert out["tariff"]["price_entity"] == "sensor.price"
    assert out["heat_pump"][0]["powers"][0]["entity"] == "sensor.hp_power"
    assert out["heat_pump"][0]["energies"][0]["entity"] == "sensor.hp_energy"
    assert out["ev_charger"][0]["powers"][0]["entity"] == "sensor.wb_power"
    assert out["ev_charger"][0]["energy"][0]["entity"] == "sensor.wb_energy"


def test_prefill_multiple_heatpumps_create_separate_instances() -> None:
    prefs = {"device_consumption": [
        {"name": "Wärmepumpe 1", "stat_rate": "sensor.hp1_p"},
        {"name": "Wärmepumpe 2", "stat_rate": "sensor.hp2_p"},
    ]}
    out = prefill_from_prefs(prefs)
    assert len(out["heat_pump"]) == 2
    assert {i["powers"][0]["entity"] for i in out["heat_pump"]} == {"sensor.hp1_p", "sensor.hp2_p"}


def test_prefill_empty() -> None:
    assert prefill_from_prefs(None) == {
        "grid": {}, "pv": [], "battery": [], "heat_pump": [],
        "water_heater": [], "ev_charger": [], "consumers": [], "tariff": {}}


def test_derive_power_from_energy_on_same_device() -> None:
    # Energy dashboard knows the battery *energy* entity; derive its power entity
    # from the same HA device (preferring battery-named candidates).
    pairs = [
        _state("sensor.batt_energy_in", "Batterie Energie", dc="energy", unit="kWh", device="dev_b"),
        _state("sensor.batt_inout", "Batterie Leistung", dc="power", unit="W", device="dev_b"),
        _state("sensor.other_power", "Anderer Sensor", dc="power", unit="W", device="dev_x"),
    ]
    states = [s for s, _m in pairs]
    ent_reg = [m for _s, m in pairs]
    got = derive_on_device(states, ent_reg, "sensor.batt_energy_in", "power",
                           setup_catalog.kind_hints("battery"))
    assert got == "sensor.batt_inout"


def test_derive_returns_none_without_device() -> None:
    assert derive_on_device([], [], "sensor.x", "power") is None


def test_prefs_entity_set_collects_all() -> None:
    s = prefs_entity_set(PREFS)
    assert {"sensor.pv_power", "sensor.grid_power", "sensor.price",
            "sensor.batt_power", "sensor.batt_soc",
            "sensor.hp_power", "sensor.wb_power"} <= s
