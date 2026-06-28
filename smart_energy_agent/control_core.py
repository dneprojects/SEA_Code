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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

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
class CommandSet:
    """Collects commands; first writer per entity wins.

    Controllers run highest-priority first, so an entity commanded by an earlier
    controller is locked for the rest of the cycle (later controllers may only
    command *other* entities). Empty-entity commands are ignored.
    """

    _by_entity: dict[str, Command] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    current_source: str = ""   # set by the runner before each controller runs

    def add(self, cmd: Command) -> bool:
        if not cmd.entity or cmd.entity in self._by_entity:
            return False
        if not cmd.source:
            cmd.source = self.current_source     # trace: who decided this
        self._by_entity[cmd.entity] = cmd
        self._order.append(cmd.entity)
        return True

    def has(self, entity: str) -> bool:
        return entity in self._by_entity

    def commands(self) -> list[Command]:
        return [self._by_entity[e] for e in self._order]

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
        for c in self._controllers:
            cmds.current_source = getattr(c, "name", "")
            try:
                c.process(image, cmds)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Controller %s failed: %s", getattr(c, "name", c), err)
        return cmds


async def apply_commands(
    call_service: Callable[..., Awaitable[Any]], store: Any, cmds: CommandSet
) -> None:
    """Write all planned commands to Home Assistant (the **O** in IPO).

    The single place for service calls, switch bookkeeping and the keepalive
    policy (setpoints are re-sent every cycle even when unchanged).
    """
    for cmd in cmds.commands():
        domain = cmd.entity.split(".", 1)[0]
        try:
            if cmd.kind in ("on", "off"):
                await call_service(
                    domain, "turn_on" if cmd.kind == "on" else "turn_off", cmd.entity)
                store.note_switch(cmd.entity, cmd.kind == "on", cmd.reason)
                _LOGGER.info("Control[%s]: %s %s (%s)", cmd.source, cmd.kind, cmd.entity, cmd.reason)
            elif cmd.kind == "set" and domain in ("number", "input_number"):
                await call_service(domain, "set_value", cmd.entity, {"value": cmd.value})
                _LOGGER.info("Control[%s]: %s -> %s (%s)", cmd.source, cmd.entity, cmd.value, cmd.reason)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Apply failed for %s: %s", cmd.entity, err)
