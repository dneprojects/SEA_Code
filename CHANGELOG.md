# Changelog

## 0.2.0

- UI cleanup: removed the "Auswahl der Entitäten" view; **Einrichtung** moved
  under **Einstellungen** (Einstellungen → Grundeinstellungen / Einrichtung).
  Tariffs merged into Grundeinstellungen and **pre-filled from the Energy
  dashboard** (price / feed-in / dynamic price entity) when not yet entered.
  Removed the global sign-invert checkboxes (per-instance invert is in the wizard).
- Wizard redesigned to a uniform **instance pattern**: setup starts empty with
  "… hinzufügen" buttons; every genus (PV, battery, heat pump, heating rod,
  wallbox, consumer) is an addable, renameable, removable instance — only the
  grid stays a fixed section. Entity pickers collapse to a summary after
  selection. Heat pump (and any genus) supports **multiple named powers/energies**
  (two pumps + auxiliary heater) and **heating circuits** (temperature + setpoint).
  Controllable instances get a **control radio** (switch via strategy / setpoint)
  that then asks for the actuator entity. Driven by a declarative
  `setup_catalog.INSTANCE_KINDS`; the whole config is posted and **sanitised**
  server-side; the old flat beta config is migrated automatically.
- Power-flow diagram: one **summary circle per device** with a **"+"** that
  expands its individual sub-powers as circles to the right (`balance` now emits
  `loads` with `parts`).
- Energy-dashboard pre-fill now **derives the power (and battery SoC) entity from
  the same HA device** as the configured energy entity — for PV, grid and battery
  — so power slots get pre-selected even when only energy entities are set up.
- Entity selection moved into a **popup dialog** (search + ranked list) opened by
  an "Ändern/Wählen" button, keeping the setup page clean.
- Suggestions now include **diagnostic/config entities** (heat-pump powers are
  often diagnostic) — previously hidden, so search couldn't find them; they rank
  slightly below primary entities. Same for device-based power derivation.
- Wizard: **free, user-named consumers** ("Weitere Verbraucher", e.g. "Allgemein",
  "Wohnungen") — each with its own power/energy entity (multi). They appear as
  extra nodes in the power-flow diagram and as groups in the device view.
- Dashboard: native animated **power-flow diagram** (SVG) — house in the centre
  with PV, grid (import/export direction), battery (charge/discharge + SoC) and
  the configured loads (heat pump, wallbox) as nodes; flow lines animate in the
  direction of energy flow with width scaled by power. Fed by `/api/flow`
  (balance now also reports wallbox power `ev_w`).
- Wizard fixes: multi-select (PV) no longer accumulates silently — already
  selected entities that the current search hides are now pinned (and can be
  unchecked), and the list re-renders after each change. The invert flag
  (grid / battery) is now also visible in the device view, which shows the
  sign-adjusted live power.
- Wizard: added the **wallbox / EV charger** category (charging power + energy +
  control actuators: charge-current setpoint and on/off), prefilled from the
  Energy dashboard's named device consumption.
- Heat pump now supports **multiple entities per quantity**: several power and
  energy meters (e.g. two pumps, summed), plus multi temperature sensors
  (heating circuits, DHW/buffer tanks — shown in the device view) and multiple
  control actuators (climate entities / setpoints per circuit). Energy-dashboard
  prefill accumulates several matching device-consumption entries into a list.

- Guided setup wizard ("Einrichtung"): instead of unreliable auto-classification,
  the user assigns the entities the agent really needs per logical category
  (v1: PV, heat pump, grid), choosing from HA-ranked suggestions — independent of
  which HA device an entity belongs to (solves scattered heat-pump entities and
  power measured by a separate Shelly PM3). New `setup_catalog.py` (slot catalog)
  and `suggest.py` (ranking + energy-prefs prefill); endpoints under `/api/setup/*`.
- Pre-fill from the HA Energy dashboard (`energy/get_prefs`): solar/grid power
  (`stat_rate`), energy meters, price entity and named device consumption
  (heat pump) are proposed top-of-list; one-click "Aus Energy-Dashboard übernehmen".
- The live energy balance now uses this explicit configuration once PV or grid
  power is set (`aggregator.balance_from_config`), falling back to the legacy
  role-based aggregation otherwise.
- Wizard now also covers the **battery** (charge/discharge power + SoC, sign
  flag; included in the balance: house = pv + grid − battery) and **control
  actuators** — for the heat pump a climate/thermostat entity or a number
  setpoint for the temperature setback/raise (marked "Stellgröße").
- Device view reworked: entities grouped by logical category (PV / battery /
  heat pump / grid) with live values, regardless of their HA device
  (`GET /api/categories`).
- App version shown in the header.
- Tests for the suggestion ranking, energy-prefs prefill (incl. battery) and
  config-based balance (incl. battery).

- Phase 2 (forecast): history-based household consumption forecast
  (recency-weighted hour-of-day load profile, weekday/weekend split) with a
  backtest of forecast accuracy (MAE/MAPE).
- Phase 2 (forecast): PV forecast from Home Assistant. Primary source is the
  Energy-dashboard `energy/solar_forecast` (Forecast.Solar; HA-cached, no
  upstream API hit, no entity config needed); a configurable PV-forecast entity
  (Solcast `detailedForecast`, generic list) is the fallback. The solar forecast
  is pulled on connect and refreshed every 15 min. Plus the resulting PV-surplus
  forecast (surplus = PV − load) — all via `GET /api/forecast`.
- New "Prognose" dashboard view: 24 h consumption/PV/surplus chart, kWh summary
  and forecast-error figure.
- Add unit-test setup (`pytest`, `requirements-dev.txt`) covering the forecast.

## 0.1.0

- First version: detection and curation of energy-related entities, live energy
  flow, history, consumer configuration, tariffs (purchase price/feed-in
  compensation), savings calculation with selectable baseline, and
  (off-by-default) PV-surplus control.
