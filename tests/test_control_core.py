"""Tests for the IPO control core (process image, command set, cycle, apply)."""

from __future__ import annotations

import asyncio

from smart_energy_agent.control_core import (
    Command, CommandSet, Cycle, ProcessImage, apply_commands,
)


def test_command_set_first_writer_wins_and_skips_empty():
    cs = CommandSet()
    assert cs.add(Command("number.a", "set", 1.0, "first")) is True
    assert cs.add(Command("number.a", "set", 2.0, "second")) is False  # locked
    assert cs.add(Command("", "set", 3.0, "empty")) is False           # ignored
    assert cs.add(Command("switch.b", "on", reason="x")) is True
    cmds = cs.commands()
    assert [c.entity for c in cmds] == ["number.a", "switch.b"]   # insertion order
    assert cmds[0].value == 1.0 and cmds[0].reason == "first"     # earlier wins
    assert cs.has("number.a") and not cs.has("number.z")


def test_cycle_runs_controllers_in_order():
    log = []

    class A:
        name = "a"
        def process(self, image, cmds): log.append("a"); cmds.add(Command("number.x", "set", 1.0))
    class Boom:
        name = "boom"
        def process(self, image, cmds): raise RuntimeError("nope")   # must be swallowed
    class B:
        name = "b"
        def process(self, image, cmds): log.append("b"); cmds.add(Command("number.x", "set", 9.0))

    cmds = Cycle([A(), Boom(), B()]).run(ProcessImage(now=0.0))
    assert log == ["a", "b"]                       # all controllers ran, in order
    assert cmds.commands()[0].value == 1.0          # first (A) wins, B blocked


def test_apply_commands_maps_kinds_and_records_switch():
    calls, switches = [], []

    async def cs(domain, service, entity, data=None):
        calls.append((domain, service, entity, data))

    class Store:
        def note_switch(self, entity, on, reason): switches.append((entity, on, reason))

    s = CommandSet()
    s.add(Command("switch.boiler", "on", reason="cheap"))
    s.add(Command("number.hz", "set", 1500.0, "regelbar"))
    s.add(Command("sensor.x", "set", 5.0, "ignored"))   # non-actuator domain -> skipped
    asyncio.run(apply_commands(cs, Store(), s))

    assert ("switch", "turn_on", "switch.boiler", None) in calls
    assert ("number", "set_value", "number.hz", {"value": 1500.0}) in calls
    assert not any(e == "sensor.x" for _, _, e, _ in calls)   # set on a sensor is dropped
    assert switches == [("switch.boiler", True, "cheap")]
