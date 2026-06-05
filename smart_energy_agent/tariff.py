"""Universal tariff/price model for load shifting (pure, unit-testable).

The engine must react to *whatever* price information is registered — there is
no single canonical source. ``cheap_now`` therefore degrades gracefully:

  * **dynamic** price entity exposing upcoming prices (a ``forecast`` /
    ``raw_today`` / ``today`` … attribute) → rank the current slot among the
    known slots; "cheap" if within the cheapest fraction.
  * **dynamic**, current value only → "cheap" if at/below an absolute threshold
    (``cheap_max_ct``), if the user set one.
  * **ht_nt** (static high/low tariff) → "cheap" during the NT (off-peak) window.
  * plain **static** tariff → no shifting benefit, never "cheap".
"""

from __future__ import annotations

from datetime import datetime, time as dtime
from typing import Any, Optional

# Attribute keys that common dynamic-tariff integrations use for upcoming prices
# (Tibber, Nord Pool, aWATTar, EPEX, generic). Checked in this order.
_TODAY_KEYS = ("raw_today", "today")
_TOMORROW_KEYS = ("raw_tomorrow", "tomorrow")
_FORECAST_KEYS = ("forecast", "prices", "upcoming", "data")
# Within a per-slot dict, the price may sit under any of these.
_PRICE_SUBKEYS = ("price", "value", "total", "amount", "cost", "marketprice")


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slot_price(item: Any) -> Optional[float]:
    """Price of one forecast slot — a bare number or a dict with a price key."""
    if isinstance(item, dict):
        for sk in _PRICE_SUBKEYS:
            f = _to_float(item.get(sk))
            if f is not None:
                return f
        return None
    return _to_float(item)


def parse_price_forecast(attrs: dict[str, Any]) -> list[float]:
    """Flat list of upcoming prices from a price entity's attributes.

    Handles lists of numbers and lists of dicts; concatenates today+tomorrow
    when both are present. Returns ``[]`` when nothing usable is found.
    """
    if not isinstance(attrs, dict):
        return []
    keys: list[str] = [k for k in _TODAY_KEYS if isinstance(attrs.get(k), list)][:1]
    keys += [k for k in _TOMORROW_KEYS if isinstance(attrs.get(k), list)][:1]
    if not keys:
        keys = [k for k in _FORECAST_KEYS if isinstance(attrs.get(k), list)][:1]
    out: list[float] = []
    for k in keys:
        for item in attrs.get(k) or []:
            f = _slot_price(item)
            if f is not None:
                out.append(f)
    return out


def _parse_hhmm(value: Any) -> Optional[dtime]:
    try:
        h, m = str(value).split(":")[:2]
        return dtime(int(h) % 24, int(m) % 60)
    except (ValueError, AttributeError):
        return None


def _in_window(now: dtime, start: dtime, end: dtime) -> bool:
    """Is ``now`` within [start, end), correctly handling a midnight wrap."""
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def cheap_now(
    tariff: dict[str, Any],
    price_state: dict[str, Any],
    now: datetime,
    cheap_fraction: float = 0.33,
) -> dict[str, Any]:
    """Decide whether *now* is a cheap period to run shiftable loads.

    Returns ``{cheap, reason, price_ct, source}``. ``source`` is one of
    ``forecast`` / ``threshold`` / ``ht_nt`` / ``static`` / ``none``.
    """
    tariff = tariff or {}
    mode = tariff.get("mode", "static")
    attrs = (price_state or {}).get("attributes", {}) or {}
    cur = _to_float((price_state or {}).get("state"))

    if mode == "dynamic":
        forecast = parse_price_forecast(attrs)
        if cur is not None and len(forecast) >= 3:
            ranked = sorted(forecast)
            idx = max(0, min(len(ranked) - 1, int(len(ranked) * cheap_fraction) - 1))
            threshold = ranked[idx]
            cheap = cur <= threshold
            pct = int(round(cheap_fraction * 100))
            return {
                "cheap": cheap, "price_ct": cur, "source": "forecast",
                "reason": (f"Preis {cur:.1f} – günstigste {pct} %" if cheap
                           else f"Preis {cur:.1f} über Günstig-Schwelle ({threshold:.1f})"),
            }
        thr = _to_float(tariff.get("cheap_max_ct"))
        if cur is not None and thr is not None:
            cheap = cur <= thr
            return {
                "cheap": cheap, "price_ct": cur, "source": "threshold",
                "reason": f"Preis {cur:.1f} {'≤' if cheap else '>'} Schwelle {thr:.1f}",
            }
        return {"cheap": False, "price_ct": cur, "source": "none",
                "reason": "keine Preisvorschau oder -schwelle vorhanden"}

    if mode == "ht_nt":
        start = _parse_hhmm(tariff.get("nt_start"))
        end = _parse_hhmm(tariff.get("nt_end"))
        if start and end and _in_window(now.time(), start, end):
            return {"cheap": True, "price_ct": _to_float(tariff.get("nt_price_ct")),
                    "source": "ht_nt", "reason": "Niedertarif-Fenster"}
        return {"cheap": False, "price_ct": _to_float(tariff.get("ht_price_ct")),
                "source": "ht_nt", "reason": "Hochtarif"}

    return {"cheap": False, "price_ct": _to_float(tariff.get("price_ct")),
            "source": "static", "reason": "statischer Tarif – keine Verschiebung"}


def has_price_source(tariff: dict[str, Any]) -> bool:
    """Whether a usable price signal for shifting is configured at all."""
    tariff = tariff or {}
    mode = tariff.get("mode", "static")
    if mode == "ht_nt":
        return bool(_parse_hhmm(tariff.get("nt_start")) and _parse_hhmm(tariff.get("nt_end")))
    if mode == "dynamic":
        return bool(tariff.get("price_entity"))
    return False
