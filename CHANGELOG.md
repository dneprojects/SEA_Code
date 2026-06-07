# Changelog

## 0.2.1

- Menu reordered: Dashboard, Verlauf, Strategien, Einsparung, Geräte,
  Einstellungen. Device gear buttons now open just that device's settings block
  (all others collapsed).
- Verlauf reworked: the **top plot now also shows every device's power**
  (consumers, wallbox, battery …) as toggleable series next to the balance
  (Haus/Netz/Überschuss). For the devices selected (enabled) in the top legend, a
  **set of detail plots** appears below — **one plot per sensor class** (all
  temperatures together, all SoC together, …) from HA's recorder, each auto-scaled
  to its own range. All plots share the **same time window** as the top plot.
  **Cumulative energies (kWh) and the measured device power are not repeated**
  below (the power is already in the top plot) — only additional quantities like
  the forced charge/discharge setpoint appear in the detail plots. New HA history
  fetch (`get_history`) + `/api/history-devices` and `/api/entity-history`.
- **Battery arbitrage – forced discharge at expensive prices**: a new battery
  actuator "Entladeleistungs-Sollwert (erzwungen)" lets the battery discharge on
  the dynamic tariff. New ceiling "Speicher entladen bei ≥ (ct/kWh)" (0 = off):
  when the price is at/above it and the SoC is above the reserve floor, the
  battery is driven to full discharge; charge/discharge/surplus are mutually
  exclusive (one controller). The battery's tariff toggle now governs both
  grid-charge and forced discharge, sharing the reserve-/target-SoC band.
- **Battery grid-charging on the dynamic tariff**: the battery can now charge
  from the grid in cheap/negative price windows. A global price ceiling
  "Speicher netzladen bei ≤ (ct/kWh)" (default **0** = only free/negative) plus a
  per-battery **target SoC band** (min reserve … max). Below the reserve floor it
  tops up at any price; otherwise it charges to the max target only when the
  price is at/under the ceiling (positive-price grid charging loses money to
  round-trip losses, hence the conservative default). The ControlEngine owns the
  battery in both surplus and grid-charge modes (no conflict with the tariff
  engine). Thermal storage already shifts via tariff load-shifting.
- Strategy list tidy-up: the separate "Wallbox PV-Überschussladen" and
  "Batterie-Optimierung" strategies are **removed** — both are already covered by
  the unified surplus list. Renamed: **"PV-Überschuss: Eigenverbrauch und
  Speicherung"** and **"Dynamischer Tarif: Lastverschiebung"**. The per-device
  settings button is now a small **gear icon (⚙)**.
- Tariff load-shifting follow-ups:
  - the **"spätester Start" deadline now also applies to tariff shifting** — a
    deferrable load (e.g. washing machine) is force-started by its deadline even
    if the tariff never got cheap. The deadline shows in the list (⏱ spätestens …).
  - the tariff list gained the **"Einstellungen →"** buttons (right-aligned, as in
    the surplus list).
  - **battery stop is the SoC** (already known) — the device block now asks only
    for the threshold ("Stopp bei SoC ≥ … %"), no entity picker. A threshold of 0
    disables the stop. For other devices the set-stop button is small/icon-only.
- Strategy UI reworked per feedback:
  - **One unified PV-surplus list**: all controllable devices (incl. wallbox AND
    battery) now live in the "PV-Überschuss-Eigenverbrauch" box, **sorted by
    priority** (high → low). The separate Wallbox/Batterie boxes no longer carry
    their own device lists. On the strategy page you only pick devices and set
    priority.
  - **Device-specific settings moved to the device block** under
    "Erzeuger/Verbraucher" (a new "PV-Überschuss-Steuerung" panel per device):
    PV threshold, min runtime/off, max starts, max/min power, W-per-unit, the
    wallbox "vehicle connected" entity and the stop condition.
  - **New "spätester Start" deadline** for deferrable loads (e.g. washing
    machine): if no surplus came by then, the device is **force-started** (from
    the grid if needed) within a 2 h window after the deadline.
- Fix "Aktueller Bezugspreis – (Entität liefert noch keinen Wert)" for dynamic
  tariffs: the current price now reads the entity's live/snapshot value
  immediately (no longer waits for the next hourly state change), the price
  entity is added to the watched set so its value stays fresh, and the value is
  **normalised to ct/kWh** from its unit (EUR/kWh, EUR/MWh, … are converted).
- **Critical persistence fix**: storage was briefly pointed at `/addon_config`,
  which is NOT a mount point — writes went to the container's ephemeral overlay
  and were lost on every restart/update (battery charge setpoint, tariff and
  other settings appeared "not saved"). The `addon_config:rw` share is mounted at
  **`/config`**; storage now uses `/config`. Migration copies any files left in
  `/data` or `/addon_config` over once.
- Fix persistence/refresh of the tariff & strategy settings:
  - the **tariff mode** dropdown now saves immediately (previously the choice was
    lost unless "Speichern" was clicked), and saving the tariff refreshes the
    strategy availability (a dynamic price source enables `tariff_shift` live).
  - the **price-entity picker** is now universal — it matches common dynamic
    tariff sensors (monetary device class, ct/kWh·EUR/kWh·öre/kWh·EUR/MWh… units,
    and number/input_number helpers), so the price entity is actually selectable.
  - added a round-trip persistence test (settings, strategy-loads, wizard config).
- **New strategy – Wallbox PV-Überschussladen (`ev_surplus`)**: the wallbox now
  follows the PV surplus via the unified modulation, with a **minimum charge
  power** (below it the wallbox switches off instead of pulling from the grid)
  and an optional **"vehicle connected"** guard entity. Excluded from the
  generic PV-surplus box (own box), kept selectable for tariff shifting.
- **New strategy – Dynamischer Tarif / Lastverschiebung (`tariff_shift`)**: a new
  `TariffEngine` runs deferrable loads during cheap periods. **Universal price
  model** (`tariff.py`) that adapts to whatever is registered: a dynamic price
  entity's upcoming-price attribute (greedy cheapest fraction), else an absolute
  threshold, else the static HT/NT window. The Strategien page shows the live
  "cheap/expensive" status. Both engines are gated by the master control switch.
- Entity picker: once an entity is selected, suggestions are narrowed to others
  with the **same name prefix** (same leading object-id tokens), so changing a
  pick stays within the same device/integration. Typing a search query lifts the
  restriction to find other families.
- Fix: the battery **charge-power setpoint** picker showed no candidates — its
  field used an undefined `number` unit group, so nothing ever matched. Added
  the `number` unit group (domains `number`/`input_number`); writable number
  entities can now be selected.
- Banner simplified: now shows only the sun (moved slightly right) — the PV
  modules, car and lightning bolt are gone. Gradient unchanged.
- **Persistent settings**: config + history now default to `/config` (the
  host-mapped `addon_config` share) instead of the add-on's private `/data`
  volume, so settings survive an uninstall/reinstall (HA wipes `/data` on
  uninstall). On update, existing legacy files are copied over once
  (non-destructive).
- Strategy device rows: the stop condition is now a small black ■ button on the
  device row; once set it appears on an indented second line. The line no longer
  has a checkbox — to remove it, click "ändern" and pick "— nicht zugeordnet —".
  Tooltips added across the pages (incl. "W/Einheit").
- Savings: the explanation text now adapts to the selected baseline.
- Dashboard: the power-flow frame height fits the diagram and grows for expanded
  sub-power circles (no more cut-off when expanding consumers).

- **Battery control**: the battery can take a charge-power setpoint (new wizard
  field) and then participates in PV-surplus as a **modulating load** (priority
  vs other loads configurable) — the Batterie-Optimierung strategy becomes
  available/active accordingly.
- **Consumer stop conditions**: per device an optional limit (e.g. vehicle SoC or
  temperature ≥ value). When reached the device is "satisfied" — switchable loads
  are switched off, modulating loads driven to 0 — so the **remaining surplus is
  redistributed to the other consumers**.

- Strategies now reference the **configured devices** (from Erzeuger/Verbraucher),
  not raw entities: pick which devices take part in PV-surplus self-consumption
  (and tariff shifting) per device, with priority/threshold/runtime. Multiple
  devices supported.
- **Modulating (regelbare) loads** (e.g. heating rod, wallbox) now absorb the
  **remaining surplus**: the control engine sets their power setpoint (number)
  to drive the grid toward zero, allocated by priority across all modulating
  loads, clamped to a max power, with a W-per-unit factor (e.g. wallbox amps).
  Switchable loads keep on/off control. The engine now uses the signed grid
  signal (fixes shedding after the surplus≥0 clamp).

- Strategien page: each strategy is now an **expandable box** with its settings
  inside (PV-surplus self-consumption holds the master switch + consumer control;
  setback holds its enable + link). Toggling an activation updates the box status
  (verfügbar ↔ aktiv) live.
- Tariff: the dynamic price entity now uses the same **entity picker popup**
  (new 'price' unit group).

- Strategies are now **auto-detected from the configured entities** and listed on
  the Strategien page with status (aktiv / verfügbar / verfügbar (folgt) / nicht
  verfügbar) and what's still missing — PV-surplus self-consumption, wallbox
  surplus charging, dynamic-tariff load shifting, battery optimisation, absence
  setback. `GET /api/strategies`, `strategies.overview()`.
- Removed the obsolete Phase-1 footer.

- Temperaturabsenkung reworked into **groups** (persons + their rooms): a group
  is set back only when **all** its persons are away; comfort returns when anyone
  is home. Multiple groups supported. Optional **predictive pre-heating** to a
  per-group comfort time using a **learned reheat rate** (min/K, EWMA). Frost
  guard + opt-in unchanged. Full-object `POST /api/thermostats` with server-side
  sanitising; live presence shown per group.

- Mobile: the sidebar collapses into a slide-in drawer; a white hamburger icon
  (from SmartHub) appears on the red header to toggle it, with a backdrop and
  auto-close on navigation. Desktop layout unchanged.

- New **Thermostate** settings page (Einstellungen → Thermostate): absence
  temperature setback for room/radiator thermostats (concept 6.6). Add named
  thermostats (climate entity + comfort/eco °C), a global presence entity, frost
  guard and a master switch. A new engine sets eco on away and comfort on home
  (opt-in, respects frost, no-op while presence is unknown). HA `call_service`
  now supports service data (climate.set_temperature).
- Power-flow: wider spacing for expanded sub-power circles (readable labels).

- Merged the forecast into the **Verlauf** view (removed the separate "Prognose"
  menu item): the forecast is drawn as **dashed lines in the same colours** as
  the history; chart colours now match the dashboard. Added **pan ◀/▶** buttons
  and a **free from/to** datetime selection (alternative to 24 h / 7 d / 30 d).
  The history API accepts an explicit `from`/`to` window; a time-based x-axis
  with a "now" marker aligns history and forecast.
- PV surplus is now clamped to **≥ 0** (no negative "surplus" when importing) —
  in the live balance, the recorded history and the forecast.
- History and forecast charts: shared renderer with **nice y-axis ticks, subtle
  horizontal/vertical grid lines and multiple x-axis time labels**.

- Power-flow diagram keeps a **configured device visible even when its entities
  are temporarily unavailable** (shows "–" instead of dropping the node);
  `balance` marks loads `configured` and reports unavailable parts as None.
- Tooltips on the setup buttons (Wählen/Ändern, clear ×, remove −, add, Entfernen,
  expand +/−, close).
- Setup page made compact: each device is now a **collapsible panel** (summary
  shows name + entity count), rows are tight and single-line where possible, and
  named powers/energies/circuits render as inline chips.
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
