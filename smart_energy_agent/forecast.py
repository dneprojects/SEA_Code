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
