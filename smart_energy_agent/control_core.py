"""Control core — an OpenEMS-inspired Input → Process → Output (IPO) cycle.

The control logic is expressed as an ordered chain of small **controllers** that
run over one consistent **process image** (the snapshot of all inputs taken once
per cycle) and contribute **commands** into a shared **command set**. A later
(lower-priority) controller may *add* commands but never overrides an entity that
an earlier (higher-priority) controller already commanded — the OpenEMS
"controllers may only further constrain" principle. Finally all commands are
written to Home Assistant in one place (``apply_commands``), which is also where
cross-cutting policies live (keepalive re-writes, switch bookkeeping).

This module is intentionally free of business logic: the concrete controllers and
the process-image construction live in ``control.py``.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

from . import const

_LOGGER = logging.getLogger(__name__)


@dataclass
class Command:
    """One planned write to Home Assistant.

    ``kind`` is ``"on"``/``"off"`` for a switch, or ``"set"`` for a numeric
    setpoint (``value`` in the entity's own unit). ``reason`` is a short,
    human-readable explanation used for logging/tracing.
    """

    entity: str
    kind: str
    value: Optional[float] = None
    reason: str = ""
    source: str = ""     # which controller produced it (stamped by the CommandSet)


@dataclass
class Constraint:
    """A narrowing of the allowed value of a numeric resource (one entity).

    This is the OpenEMS "controllers may only further constrain" model: instead
    of a single owner writing a final setpoint, every controller contributes a
    constraint — an allowed interval ``[lo, hi]`` and/or a desired ``target``.
    The resolver intersects all constraints for an entity in priority order
    (highest first) and clamps the highest-priority ``target`` into the surviving
    interval. A plain numeric command is just a constraint that only sets a
    target; a reserve/limit controller adds a ``hi``/``lo`` bound that the target
    must respect — so strategies compose on shared resources (e.g. the battery)
    instead of one silently winning.
    """

    entity: str
    lo: float = -math.inf
    hi: float = math.inf
    target: Optional[float] = None
    priority: float = 0.0
    source: str = ""
    reason: str = ""


@dataclass
class CommandSet:
    """Collects controller output and resolves it into final writes.

    Switches (``on``/``off``) keep first-writer-per-entity-wins (a switch has no
    range to narrow). Numeric setpoints are collected as :class:`Constraint`\\ s
    and resolved by priority: the highest-priority ``target`` wins and is clamped
    into the intersection of all bounds. ``current_source``/``current_priority``
    are stamped by the runner before each controller so output is traceable and
    ordered by controller priority.
    """

    _switches: dict[str, Command] = field(default_factory=dict)
    _constraints: list[Constraint] = field(default_factory=list)
    _order: list[str] = field(default_factory=list)   # entities by first appearance
    current_source: str = ""
    current_priority: float = 0.0
    bounds: dict[str, tuple[float, float]] = field(default_factory=dict)  # device [min,max]

    def _touch(self, entity: str) -> None:
        if entity not in self._order:
            self._order.append(entity)

    def add(self, cmd: Command) -> bool:
        """Add a switch command (first writer wins) or a numeric target."""
        if not cmd.entity:
            return False
        if not cmd.source:
            cmd.source = self.current_source     # trace: who decided this
        if cmd.kind in ("on", "off"):
            if cmd.entity in self._switches:
                return False                     # switch already owned this cycle
            self._switches[cmd.entity] = cmd
            self._touch(cmd.entity)
            return True
        self._constraints.append(Constraint(
            entity=cmd.entity, target=cmd.value, priority=self.current_priority,
            source=cmd.source, reason=cmd.reason))
        self._touch(cmd.entity)
        return True

    def constrain(self, entity: str, *, lo: float = -math.inf, hi: float = math.inf,
                  target: Optional[float] = None, reason: str = "") -> None:
        """Add a bound and/or target for a numeric resource (the constraint API)."""
        if not entity:
            return
        self._constraints.append(Constraint(
            entity=entity, lo=lo, hi=hi, target=target,
            priority=self.current_priority, source=self.current_source, reason=reason))
        self._touch(entity)

    def has(self, entity: str) -> bool:
        return entity in self._switches or any(c.entity == entity for c in self._constraints)

    def _resolve(self, bounds: Optional[dict[str, tuple[float, float]]] = None) -> list[Command]:
        """Resolve switches + constraints into the final per-entity commands.

        ``bounds`` optionally gives a device's hard ``[min, max]`` per entity as
        the starting interval; controller constraints narrow it from there.
        """
        bounds = bounds or {}
        out: dict[str, Command] = dict(self._switches)
        by_entity: dict[str, list[Constraint]] = {}
        for c in self._constraints:
            by_entity.setdefault(c.entity, []).append(c)
        for entity, cs in by_entity.items():
            lo, hi = bounds.get(entity, (-math.inf, math.inf))
            order = sorted(range(len(cs)), key=lambda i: (-cs[i].priority, i))  # high prio first
            target: Optional[float] = None
            src, reason = self.current_source, ""
            for i in order:
                c = cs[i]
                nlo, nhi = max(lo, c.lo), min(hi, c.hi)
                if nlo <= nhi:
                    lo, hi = nlo, nhi
                # else: this lower-priority bound can't be honoured -> dropped
                if target is None and c.target is not None:
                    target, src, reason = c.target, c.source, c.reason
            if target is None:
                continue                         # a pure bound with no target -> no write
            val = min(max(target, lo), hi)
            out[entity] = Command(entity, "set", round(val, 2), reason, src)
        return [out[e] for e in self._order if e in out]

    def commands(self) -> list[Command]:
        return self._resolve(self.bounds)

    def trace(self) -> list[dict[str, Any]]:
        """Serializable record of what each controller decided (for debugging)."""
        return [{"entity": c.entity, "kind": c.kind, "value": c.value,
                 "source": c.source, "reason": c.reason} for c in self.commands()]


@dataclass
class ProcessImage:
    """Consistent snapshot of all inputs for one cycle (the **I** in IPO).

    Built once at the start of a cycle so every controller sees the same data.
    Business fields (consumers, modulating loads, …) are attached by the engine;
    ``extra`` is a free-form bag for additional controller inputs.
    """

    now: float
    surplus_signed: float = 0.0
    mods_hold: bool = False         # True = freeze modulating setpoints (stale/unreliable data)
    grid_w: float = 0.0          # raw grid power: + = import, − = export
    pv_w: float = 0.0            # PV production (W)
    consumers: list[Any] = field(default_factory=list)
    mods: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class Controller(Protocol):
    """A unit of control logic. ``process`` reads the image and adds commands."""

    name: str

    def process(self, image: ProcessImage, cmds: CommandSet) -> None: ...


class Cycle:
    """Runs an ordered list of controllers over one process image (the **P**)."""

    def __init__(self, controllers: list[Controller]) -> None:
        self._controllers = controllers

    def run(self, image: ProcessImage) -> CommandSet:
        cmds = CommandSet()
        n = len(self._controllers)
        for i, c in enumerate(self._controllers):
            cmds.current_source = getattr(c, "name", "")
            cmds.current_priority = n - i        # earlier controller -> higher priority
            try:
                c.process(image, cmds)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Controller %s failed: %s", getattr(c, "name", c), err)
        return cmds


async def apply_commands(
    call_service: Callable[..., Awaitable[Any]], store: Any, cmds: CommandSet,
    now: Optional[float] = None,
) -> None:
    """Write all planned commands to Home Assistant (the **O** in IPO).

    The single place for service calls, switch bookkeeping and the keepalive
    policy. Unchanged commands are skipped and only re-sent every
    ``APPLY_KEEPALIVE_S`` (keepalive), so a fast control cadence doesn't spam HA.
    """
    cache = getattr(store, "_apply_cache", None)
    if cache is None:
        cache = {}
        try:
            store._apply_cache = cache
        except Exception:  # noqa: BLE001 - store may be a stub in tests
            pass
    mono = now if now is not None else time.time()
    for cmd in cmds.commands():
        domain = cmd.entity.split(".", 1)[0]
        key = cmd.kind if cmd.kind in ("on", "off") else cmd.value
        prev = cache.get(cmd.entity)
        # Switches: send on change, else a periodic keepalive (don't spam HA).
        # Setpoints ("set"): re-send EVERY cycle. Externally-controlled actuators
        # (my-PV AC ELWA 2, inverters, wallboxes …) run a control watchdog and
        # fall back to 0 / their own control if the target isn't refreshed within
        # seconds — a 55 s keepalive is far too slow, so never skip a setpoint.
        if cmd.kind in ("on", "off") and prev is not None and prev[0] == key \
                and (mono - prev[1]) < const.APPLY_KEEPALIVE_S:
            continue
        try:
            if cmd.kind in ("on", "off"):
                await call_service(
                    domain, "turn_on" if cmd.kind == "on" else "turn_off", cmd.entity)
                store.note_switch(cmd.entity, cmd.kind == "on", cmd.reason)
                _LOGGER.info("Control[%s]: %s %s (%s)", cmd.source, cmd.kind, cmd.entity, cmd.reason)
            elif cmd.kind == "set" and domain in ("number", "input_number"):
                await call_service(domain, "set_value", cmd.entity, {"value": cmd.value})
                _LOGGER.info("Control[%s]: %s -> %s (%s)", cmd.source, cmd.entity, cmd.value, cmd.reason)
            else:
                continue
            cache[cmd.entity] = (key, mono)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Apply failed for %s: %s", cmd.entity, err)
