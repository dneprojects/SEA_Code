# Changelog

## Unreleased

- Phase 2 (forecast) – start: history-based household consumption forecast
  (recency-weighted hour-of-day load profile, weekday/weekend split) with a
  backtest of forecast accuracy (MAE/MAPE), exposed via `GET /api/forecast`.
- Add unit-test setup (`pytest`, `requirements-dev.txt`) covering the forecast.

## 0.1.0

- First version: detection and curation of energy-related entities, live energy
  flow, history, consumer configuration, tariffs (purchase price/feed-in
  compensation), savings calculation with selectable baseline, and
  (off-by-default) PV-surplus control.
