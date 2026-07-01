"""Tests for the IPO control core (process image, command set, cycle, apply)."""

from __future__ import annotations

import asyncio

from smart_energy_agent.control_core import (
    Command, CommandSet, Cycle, ProcessImage, apply_commands,
)


def test_command_set_highest_priority_target_wins_and_skips_empty():
    cs = CommandSet()
    assert cs.add(Command("number.a", "set", 1.0, "first")) is True
    assert cs.add(Command("number.a", "set", 2.0, "second")) is True   # accepted (constraint)
    assert cs.add(Command("", "set", 3.0, "empty")) is False           # ignored
    assert cs.add(Command("switch.b", "on", reason="x")) is True
    cmds = cs.commands()
    assert [c.entity for c in cmds] == ["number.a", "switch.b"]   # first-appearance order
    # same priority -> the first target still wins (back-compat with first-writer)
    assert cmds[0].value == 1.0 and cmds[0].reason == "first"
    assert cs.has("number.a") and not cs.has("number.z")


def test_constraint_bound_clamps_lower_priority_target():
    """The keystone: a higher-priority bound narrows a lower-priority target."""
    cs = CommandSet()
    cs.current_source, cs.current_priority = "peak_shaving", 1
    cs.add(Command("number.bd", "set", 800.0, "Peak-Shaving"))      # low-prio target
    cs.current_source, cs.current_priority = "reserve", 10
    cs.constrain("number.bd", hi=0.0, reason="SoC-Reserve")         # high-prio bound: no discharge
    bd = {c.entity: c for c in cs.commands()}["number.bd"]
    assert bd.value == 0.0          # reserve clamps the discharge target to 0
    # a target alone (no competing bound) passes through unchanged
    cs2 = CommandSet()
    cs2.add(Command("number.bc", "set", 1500.0, "Laden"))
    assert cs2.commands()[0].value == 1500.0


def test_resolve_clamps_to_device_bounds():
    cs = CommandSet()
    cs.bounds = {"number.hz": (0.0, 3000.0)}
    cs.add(Command("number.hz", "set", 5000.0, "too much"))
    assert cs.commands()[0].value == 3000.0      # clamped to the device max
    cs2 = CommandSet()
    cs2.bounds = {"number.hz": (0.0, 3000.0)}
    cs2.add(Command("number.hz", "set", -10.0, "neg"))
    assert cs2.commands()[0].value == 0.0        # clamped to the device min


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


def test_cycle_stamps_command_source_for_tracing():
    class Heater:
        name = "pv_surplus_modulation"
        def process(self, image, cmds): cmds.add(Command("number.hz", "set", 0.0, "regelbar"))
    class Peak:
        name = "peak_shaving"
        def process(self, image, cmds): cmds.add(Command("number.bd", "set", 500.0, "Peak"))

    cmds = Cycle([Heater(), Peak()]).run(ProcessImage(now=0.0))
    by = {t["entity"]: t for t in cmds.trace()}
    assert by["number.hz"]["source"] == "pv_surplus_modulation"
    assert by["number.bd"]["source"] == "peak_shaving" and by["number.bd"]["reason"] == "Peak"


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
