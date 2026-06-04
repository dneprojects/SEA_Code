# Changelog

## Unreleased

- Phase 2 (forecast): history-based household consumption forecast
  (recency-weighted hour-of-day load profile, weekday/weekend split) with a
  backtest of forecast accuracy (MAE/MAPE).
- Phase 2 (forecast): source-agnostic PV forecast parsed from a configurable HA
  entity (Forecast.Solar `watts`, Solcast `detailedForecast`, generic forecast
  list), and the resulting PV-surplus forecast (surplus = PV − load) — all via
  `GET /api/forecast`; PV entity configurable under Settings → basics.
- New "Prognose" dashboard view: 24 h consumption/PV/surplus chart, kWh summary
  and forecast-error figure.
- Add unit-test setup (`pytest`, `requirements-dev.txt`) covering the forecast.

## 0.1.0

- First version: detection and curation of energy-related entities, live energy
  flow, history, consumer configuration, tariffs (purchase price/feed-in
  compensation), savings calculation with selectable baseline, and
  (off-by-default) PV-surplus control.
