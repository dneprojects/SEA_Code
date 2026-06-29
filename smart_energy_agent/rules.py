"""A small, safe declarative rule engine (a JSON-logic subset).

Lets simple strategies be expressed as **data** instead of code — the analog of
OpenEMS' ``generic.jsonlogic`` controller. A rule is::

    {"id": "boiler", "when": {">": [{"var": "surplus"}, 1500]},
     "then": [{"switch": {"entity": "switch.boiler", "on": true}}],
     "priority": 0, "enabled": true}

``when`` is a JSON-logic expression over **variables** (resolved by a callable):
scalars like ``grid_w``/``surplus``/``hour``/``price_ct`` and any entity id
(dotted name → its numeric live state; on/home/heat → 1, else 0). Truthy ``when``
applies the ``then`` actions, which emit commands/constraints into the shared
command set — so rules compose with the built-in controllers via the same
priority/constraint arbitration (block 1) and device limits (block 2).

The evaluator is pure data interpretation (no ``eval``), so rules are safe.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Callable

from .control_core import Command, CommandSet, ProcessImage

_LOGGER = logging.getLogger(__name__)

Resolver = Callable[[str], Any]


def _num(v: Any) -> float:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def truthy(v: Any) -> bool:
    """JSON-logic truthiness (0, '', [], None, false are falsy)."""
    if isinstance(v, (list, str)):
        return len(v) > 0
    return bool(v)


def _eq(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return str(a) == str(b)


def _chain_cmp(vals: list[Any], op: Callable[[float, float], bool]) -> bool:
    """Compare pairwise (JSON-logic allows ``{"<": [a, b, c]}`` = a<b<c)."""
    nums = [_num(v) for v in vals]
    return all(op(nums[i], nums[i + 1]) for i in range(len(nums) - 1))


def _do_if(args: list[Any], resolve: Resolver) -> Any:
    # if/elseif chain: [cond, then, cond2, then2, ..., else]
    i = 0
    while i + 1 < len(args):
        if truthy(evaluate(args[i], resolve)):
            return evaluate(args[i + 1], resolve)
        i += 2
    return evaluate(args[i], resolve) if i < len(args) else None


def evaluate(rule: Any, resolve: Resolver) -> Any:
    """Evaluate a JSON-logic(-subset) expression against ``resolve(varname)``."""
    if not isinstance(rule, dict):
        return rule                                   # literal
    if len(rule) != 1:
        raise ValueError("rule object must have exactly one operator")
    op, raw = next(iter(rule.items()))

    if op == "var":
        name = raw[0] if isinstance(raw, list) else raw
        return resolve(str(evaluate(name, resolve)))
    if op == "if":
        return _do_if(raw if isinstance(raw, list) else [raw], resolve)

    args = raw if isinstance(raw, list) else [raw]
    vals = [evaluate(a, resolve) for a in args]

    if op == "and":
        return all(truthy(v) for v in vals)
    if op == "or":
        return any(truthy(v) for v in vals)
    if op == "!":
        return not truthy(vals[0])
    if op == "==":
        return _eq(vals[0], vals[1])
    if op == "!=":
        return not _eq(vals[0], vals[1])
    if op == ">":
        return _chain_cmp(vals, lambda a, b: a > b)
    if op == ">=":
        return _chain_cmp(vals, lambda a, b: a >= b)
    if op == "<":
        return _chain_cmp(vals, lambda a, b: a < b)
    if op == "<=":
        return _chain_cmp(vals, lambda a, b: a <= b)
    if op == "+":
        return sum(_num(v) for v in vals)
    if op == "*":
        out = 1.0
        for v in vals:
            out *= _num(v)
        return out
    if op == "-":
        return -_num(vals[0]) if len(vals) == 1 else _num(vals[0]) - _num(vals[1])
    if op == "/":
        return _num(vals[0]) / _num(vals[1]) if _num(vals[1]) != 0 else 0.0
    if op == "%":
        return _num(vals[0]) % _num(vals[1]) if _num(vals[1]) != 0 else 0.0
    if op == "min":
        return min(_num(v) for v in vals)
    if op == "max":
        return max(_num(v) for v in vals)
    if op == "in":
        return vals[0] in vals[1] if isinstance(vals[1], (list, str)) else False
    raise ValueError(f"unsupported operator: {op}")


def make_resolver(image: ProcessImage, store: Any) -> Resolver:
    """Build the variable resolver for one cycle (scalars + entity states)."""
    lt = time.localtime(image.now)
    try:
        price = float(store.current_price_ct() or 0.0)
    except Exception:  # noqa: BLE001
        price = 0.0
    scalars: dict[str, float] = {
        "grid_w": image.grid_w,
        "surplus": image.surplus_signed,
        "surplus_w": image.surplus_signed,
        "hour": float(lt.tm_hour),
        "minute": float(lt.tm_min),
        "weekday": float(lt.tm_wday),
        "month": float(lt.tm_mon),
        "price_ct": price,
    }
    truthy_states = ("on", "true", "heat", "home", "open", "charging")

    def resolve(name: str) -> Any:
        if name in scalars:
            return scalars[name]
        if "." in name:                               # entity id -> numeric state
            st = store.live_state(name).get("state")
            try:
                return float(st)
            except (TypeError, ValueError):
                return 1.0 if str(st).lower() in truthy_states else 0.0
        return 0.0

    return resolve


def apply_actions(actions: list[Any], resolve: Resolver, cmds: CommandSet) -> None:
    """Apply a rule's ``then`` actions as commands/constraints."""
    def val(x: Any) -> Any:
        return evaluate(x, resolve) if isinstance(x, dict) else x

    for a in actions or []:
        if not isinstance(a, dict) or len(a) != 1:
            continue
        kind, spec = next(iter(a.items()))
        if not isinstance(spec, dict):
            continue
        entity = spec.get("entity")
        if not entity:
            continue
        reason = str(spec.get("reason", "Regel"))
        if kind == "set":
            cmds.add(Command(str(entity), "set", round(_num(val(spec.get("value"))), 2), reason))
        elif kind == "switch":
            on = truthy(val(spec.get("on", True)))
            cmds.add(Command(str(entity), "on" if on else "off", reason=reason))
        elif kind == "constrain":
            lo = _num(val(spec["lo"])) if "lo" in spec else -math.inf
            hi = _num(val(spec["hi"])) if "hi" in spec else math.inf
            target = round(_num(val(spec["target"])), 2) if "target" in spec else None
            cmds.constrain(str(entity), lo=lo, hi=hi, target=target, reason=reason)


class RuleController:
    """Runs user-defined rules; emits commands/constraints into the chain.

    Rules default to the controller's (low) priority, so the built-in strategies
    win on shared entities; a rule may set its own ``priority`` to override. Off
    (no rules configured) it adds nothing — fully behaviour-equivalent.
    """

    name = "rules"

    def process(self, image: ProcessImage, cmds: CommandSet) -> None:
        rules = image.extra.get("rules")
        resolve = image.extra.get("rule_resolve")
        if not rules or resolve is None:
            return
        base = cmds.current_priority
        for r in rules:
            if not isinstance(r, dict) or r.get("enabled") is False:
                continue
            try:
                if not truthy(evaluate(r.get("when", True), resolve)):
                    continue
                cmds.current_priority = float(r.get("priority", base))
                apply_actions(r.get("then", []), resolve, cmds)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("rule %s failed: %s", r.get("id"), err)
            finally:
                cmds.current_priority = base
