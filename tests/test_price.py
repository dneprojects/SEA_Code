"""Current dynamic price: snapshot fallback + unit normalisation to ct/kWh."""

from __future__ import annotations

from smart_energy_agent.store import Store


def _store_with_price(state, unit=None, monkeypatch=None):
    s = Store()
    s._settings["tariff"] = {"mode": "dynamic", "price_entity": "sensor.price"}
    attrs = {"unit_of_measurement": unit} if unit else {}
    # only a snapshot is present (no state_changed event has arrived yet)
    s._ha_snapshot = {"state_by_id": {"sensor.price": {"state": state, "attributes": attrs}}}
    return s


def test_price_reads_snapshot_before_any_event(tmp_path, monkeypatch):
    monkeypatch.setenv("SEA_HISTORY_DB", str(tmp_path / "h.db"))
    s = _store_with_price("28", "ct/kWh")
    assert s.current_price_ct() == 28.0


def test_price_unit_normalisation(tmp_path, monkeypatch):
    monkeypatch.setenv("SEA_HISTORY_DB", str(tmp_path / "h.db"))
    assert _store_with_price("0.2832", "EUR/kWh").current_price_ct() == 28.32
    assert _store_with_price("283.2", "EUR/MWh").current_price_ct() == 28.32
    assert _store_with_price("30").current_price_ct() == 30.0           # unit-less
    assert _store_with_price("unavailable", "ct/kWh").current_price_ct() is None


def test_price_entity_is_watched(tmp_path, monkeypatch):
    monkeypatch.setenv("SEA_HISTORY_DB", str(tmp_path / "h.db"))
    s = _store_with_price("28", "ct/kWh")
    assert "sensor.price" in s.watched_entity_ids()
