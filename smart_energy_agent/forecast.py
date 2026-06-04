"""History-based consumption forecast (concept ch. 5a.2).

Builds an hour-of-day load profile from the recorded energy-state history and
projects household consumption for the next hours. Deliberately simple and
explainable, per the concept's "leichtgewichtig, erklaerbar, ohne Trainingsdaten
startfaehig" guideline:

  * Per (day class, hour-of-day) bucket we keep an exponentially recency-weighted
    average of ``house_load_w`` (recent days count more, half-life configurable).
  * Day class splits weekday vs. weekend, the cheapest split that still captures
    the dominant weekly pattern. Hours without their own bucket fall back to the
    overall weighted mean, so the forecast produces useful output after ~1-2 days.

All functions are pure over the input rows (timestamp, watt) and take an
injectable ``now``, so they are unit-testable without a database or wall clock.
The history-based consumption forecast is the demand side of the PV-surplus
forecast (concept ch. 5a.3); the PV/weather side is added in a later slice.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

SECONDS_PER_DAY = 86400
DEFAULT_HALF_LIFE_DAYS = 14.0
DEFAULT_HISTORY_DAYS = 28


def _day_class(tm: time.struct_time) -> str:
    """Weekend (Sat/Sun) vs. weekday — the dominant weekly load split."""
    return "weekend" if tm.tm_wday >= 5 else "weekday"


@dataclass
class LoadProfile:
    """Recency-weighted hour-of-day load profile.

    ``buckets`` maps (day_class, hour) -> mean watts; ``overall_w`` is the
    fallback for hours that have no own bucket yet.
    """

    buckets: dict[tuple[str, int], float]
    overall_w: Optional[float]
    samples: int
    span_days: float

    def predict(self, ts: float) -> tuple[Optional[float], str]:
        """Predicted load (W) at ``ts`` plus the source ("profile"/"overall"/"none")."""
        tm = time.localtime(ts)
        key = (_day_class(tm), tm.tm_hour)
        if key in self.buckets:
            return self.buckets[key], "profile"
        if self.overall_w is not None:
            return self.overall_w, "overall"
        return None, "none"


def build_load_profile(
    rows: Iterable[tuple[int, Optional[float]]],
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[float] = None,
) -> LoadProfile:
    """Build a recency-weighted load profile from (ts, house_load_w) rows."""
    now = time.time() if now is None else now
    decay = (
        math.log(2.0) / (half_life_days * SECONDS_PER_DAY)
        if half_life_days and half_life_days > 0
        else 0.0
    )
    weight_sum: dict[tuple[str, int], float] = {}
    value_sum: dict[tuple[str, int], float] = {}
    total_weight = 0.0
    total_value = 0.0
    n = 0
    min_ts: Optional[int] = None
    max_ts: Optional[int] = None
    for ts, watt in rows:
        if watt is None:
            continue
        try:
            w = float(watt)
        except (TypeError, ValueError):
            continue
        age = max(0.0, now - ts)
        weight = math.exp(-decay * age) if decay > 0 else 1.0
        tm = time.localtime(ts)
        key = (_day_class(tm), tm.tm_hour)
        weight_sum[key] = weight_sum.get(key, 0.0) + weight
        value_sum[key] = value_sum.get(key, 0.0) + weight * w
        total_weight += weight
        total_value += weight * w
        n += 1
        min_ts = ts if min_ts is None else min(min_ts, ts)
        max_ts = ts if max_ts is None else max(max_ts, ts)

    buckets = {
        k: value_sum[k] / weight_sum[k] for k in weight_sum if weight_sum[k] > 0
    }
    overall = total_value / total_weight if total_weight > 0 else None
    span = (
        (max_ts - min_ts) / SECONDS_PER_DAY
        if min_ts is not None and max_ts is not None
        else 0.0
    )
    return LoadProfile(
        buckets=buckets, overall_w=overall, samples=n, span_days=round(span, 2)
    )


def forecast_consumption(
    rows: Iterable[tuple[int, Optional[float]]],
    *,
    hours: int = 24,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Forecast household consumption for the next ``hours`` whole hours.

    Returns hourly points (each carrying its prediction source), the summed
    energy in kWh, how much of the horizon is covered by data, and the size of
    the history the profile was built from.
    """
    now = time.time() if now is None else now
    rows = list(rows)
    profile = build_load_profile(rows, half_life_days=half_life_days, now=now)
    hours = max(0, hours)
    start = (int(now) // 3600 + 1) * 3600  # next full hour
    points: list[dict[str, Any]] = []
    total_wh = 0.0
    covered = 0
    for i in range(hours):
        ts = start + i * 3600
        watt, source = profile.predict(ts)
        if watt is not None:
            total_wh += watt  # one-hour slots -> Wh
            covered += 1
        points.append(
            {
                "ts": ts,
                "watt": round(watt, 1) if watt is not None else None,
                "source": source,
            }
        )
    return {
        "horizon_h": hours,
        "start_ts": start,
        "points": points,
        "kwh": round(total_wh / 1000.0, 2),
        "samples": profile.samples,
        "span_days": profile.span_days,
        "coverage": round(covered / hours, 2) if hours else 0.0,
    }


# --- PV / weather forecast (concept ch. 5a.1) --------------------------------

def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(s: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp (HA forecast keys) to epoch seconds."""
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str):
        return None
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def parse_solar_forecast(data: Optional[dict[str, Any]]) -> list[tuple[float, float]]:
    """Parse the HA ``energy/solar_forecast`` result into [(ts, watt)].

    This is the supported source for Forecast.Solar (and any Energy-dashboard
    solar-forecast integration): HA returns its cached forecast, so polling it
    does not hit the upstream API. Shape::

        {config_entry_id: {"wh_hours": {iso_ts: wh}}}

    Multiple config entries (e.g. several roof planes) are summed per timestamp.
    Each value is watt-hours over a one-hour period, i.e. numerically the
    average power in watts for that hour.
    """
    if not isinstance(data, dict):
        return []
    by_ts: dict[float, float] = {}
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        wh_hours = entry.get("wh_hours")
        if not isinstance(wh_hours, dict):
            continue
        for k, v in wh_hours.items():
            ts, w = _parse_iso(k), _to_float(v)
            if ts is not None and w is not None:
                by_ts[ts] = by_ts.get(ts, 0.0) + w  # Wh over 1 h == W
    return sorted(by_ts.items())


def parse_pv_forecast(state: Optional[dict[str, Any]]) -> list[tuple[float, float]]:
    """Extract a PV power forecast [(ts, watt)] from an HA entity's state dict.

    Source-agnostic: auto-detects the common shapes so the user only has to
    point at one entity (concept ch. 12 leaves the exact source open):

      * Forecast.Solar  – attribute ``watts`` = {iso_ts: watts}
      * Solcast         – attribute ``detailedForecast``/``detailedHourly`` =
        [{period_start, pv_estimate (kW)}]
      * generic         – attribute ``forecast`` = [{datetime, power/pv_estimate}]

    Returns points sorted by timestamp; empty list if nothing is parseable.
    """
    if not state:
        return []
    attrs = state.get("attributes") or {}
    points: list[tuple[float, float]] = []

    # Forecast.Solar: instantaneous power forecast keyed by ISO timestamp.
    watts = attrs.get("watts")
    if isinstance(watts, dict):
        for k, v in watts.items():
            ts, w = _parse_iso(k), _to_float(v)
            if ts is not None and w is not None:
                points.append((ts, w))
        if points:
            return sorted(points)

    # Solcast: per-period PV estimate in kW.
    for key in ("detailedForecast", "detailedHourly"):
        lst = attrs.get(key)
        if isinstance(lst, list):
            for it in lst:
                if not isinstance(it, dict):
                    continue
                ts = _parse_iso(it.get("period_start") or it.get("datetime"))
                w = _to_float(it.get("pv_estimate"))
                if ts is not None and w is not None:
                    points.append((ts, w * 1000.0))  # kW -> W
            if points:
                return sorted(points)

    # Generic forecast list with some power-ish field.
    lst = attrs.get("forecast")
    if isinstance(lst, list):
        for it in lst:
            if not isinstance(it, dict):
                continue
            ts = _parse_iso(it.get("datetime") or it.get("period_start"))
            val: Optional[float] = None
            for k in ("pv_estimate", "pv_power", "power", "watts"):
                if k in it:
                    val = _to_float(it[k])
                    if k == "pv_estimate" and val is not None:
                        val *= 1000.0
                    break
            if ts is not None and val is not None:
                points.append((ts, val))
        if points:
            return sorted(points)

    return []


def _pv_in_hour(pv_points: list[tuple[float, float]], ts: int) -> Optional[float]:
    """Average PV forecast (W) over the hour starting at ``ts``."""
    vals = [w for (t, w) in pv_points if ts <= t < ts + 3600]
    return sum(vals) / len(vals) if vals else None


def build_surplus_forecast(
    consumption: dict[str, Any],
    pv_points: list[tuple[float, float]],
) -> dict[str, Any]:
    """Combine the consumption forecast with a PV forecast into a PV-surplus
    forecast (concept ch. 5a.3): surplus = PV - load, hour by hour."""
    pts = consumption.get("points") or []
    pv_points = sorted(pv_points or [])
    out: list[dict[str, Any]] = []
    pv_wh = load_wh = surplus_wh = 0.0
    pv_covered = 0
    for p in pts:
        ts = p["ts"]
        load = p.get("watt")
        pv = _pv_in_hour(pv_points, ts)
        surplus = None
        if pv is not None:
            pv_wh += pv
            pv_covered += 1
            if load is not None:
                surplus = max(0.0, pv - load)  # PV surplus only positive
                surplus_wh += surplus
        if load is not None:
            load_wh += load
        out.append(
            {
                "ts": ts,
                "load_w": load,
                "pv_w": round(pv, 1) if pv is not None else None,
                "surplus_w": round(surplus, 1) if surplus is not None else None,
            }
        )
    n = len(pts)
    return {
        "points": out,
        "pv_kwh": round(pv_wh / 1000.0, 2),
        "load_kwh": round(load_wh / 1000.0, 2),
        "surplus_kwh": round(surplus_wh / 1000.0, 2),
        "pv_available": pv_covered > 0,
        "pv_coverage": round(pv_covered / n, 2) if n else 0.0,
    }


def profile_backtest(
    rows: Iterable[tuple[int, Optional[float]]],
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    test_window_days: float = 1.0,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Forecast quality (concept ch. 5a.3): hold out the most recent window,
    build the profile from the older data, predict the held-out tail and report
    mean absolute error (W) and mean absolute percentage error (%)."""
    now = time.time() if now is None else now
    clean = [(int(ts), w) for ts, w in rows if w is not None]
    empty = {"samples": 0, "mae_w": None, "mape_pct": None}
    if not clean:
        return empty
    split = now - test_window_days * SECONDS_PER_DAY
    train = [(ts, w) for ts, w in clean if ts < split]
    test = [(ts, w) for ts, w in clean if ts >= split]
    if not train or not test:
        return empty
    profile = build_load_profile(train, half_life_days=half_life_days, now=split)

    abs_err = 0.0
    ape = 0.0
    ape_n = 0
    n = 0
    for ts, w in test:
        pred, _src = profile.predict(ts)
        if pred is None:
            continue
        try:
            actual = float(w)
        except (TypeError, ValueError):
            continue
        abs_err += abs(pred - actual)
        n += 1
        if abs(actual) > 1e-6:
            ape += abs(pred - actual) / abs(actual)
            ape_n += 1
    if n == 0:
        return empty
    return {
        "samples": n,
        "mae_w": round(abs_err / n, 1),
        "mape_pct": round(100.0 * ape / ape_n, 1) if ape_n else None,
    }
