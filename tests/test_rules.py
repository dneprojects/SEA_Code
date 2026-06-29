"""Tests for the declarative rule engine (JSON-logic subset)."""

from __future__ import annotations

from smart_energy_agent.control_core import Command, CommandSet, ProcessImage
from smart_energy_agent.rules import RuleController, evaluate, make_resolver


def _res(d):
    return lambda name: d.get(name, 0.0)


def test_evaluate_operators():
    r = _res({"grid_w": 3200, "surplus": 1500, "hour": 14})
    assert evaluate({">": [{"var": "grid_w"}, 3000]}, r) is True
    assert evaluate({"and": [{">": [{"var": "surplus"}, 1000]},
                             {"<": [{"var": "hour"}, 18]}]}, r) is True
    assert evaluate({"or": [{"<": [{"var": "grid_w"}, 0]}, False]}, r) is False
    assert evaluate({"!": [{"<": [{"var": "surplus"}, 0]}]}, r) is True
    assert evaluate({"+": [1, 2, 3]}, r) == 6
    assert evaluate({"if": [{">": [{"var": "surplus"}, 1000]}, 100, 0]}, r) == 100
    assert evaluate({"<": [1, 2, 3]}, r) is True            # chained comparison
    assert evaluate({"in": [2, [1, 2, 3]]}, r) is True


def test_make_resolver_scalars_and_entities():
    class S:
        _s = {"sensor.p": {"state": "812"}, "binary_sensor.plug": {"state": "on"}}
        def live_state(self, e):
            return self._s.get(e, {})
        def current_price_ct(self):
            return 23.4

    img = ProcessImage(now=0.0, grid_w=500.0, surplus_signed=1200.0)
    r = make_resolver(img, S())
    assert r("grid_w") == 500.0 and r("surplus") == 1200.0 and r("price_ct") == 23.4
    assert r("sensor.p") == 812.0
    assert r("binary_sensor.plug") == 1.0                   # on -> 1
    assert r("sensor.unknown") == 0.0


def test_rule_controller_fires_and_respects_enabled():
    img = ProcessImage(now=0.0, surplus_signed=2000.0)
    img.extra["rules"] = [
        {"id": "boiler", "when": {">": [{"var": "surplus"}, 1500]},
         "then": [{"switch": {"entity": "switch.boiler", "on": True}}]},
        {"id": "off", "enabled": False, "when": True,
         "then": [{"switch": {"entity": "switch.x", "on": True}}]},
    ]
    img.extra["rule_resolve"] = _res({"surplus": 2000.0})
    cmds = CommandSet()
    RuleController().process(img, cmds)
    out = {c.entity: c for c in cmds.commands()}
    assert out["switch.boiler"].kind == "on"
    assert "switch.x" not in out                            # disabled rule skipped


def test_rule_set_action_evaluates_value():
    img = ProcessImage(now=0.0)
    img.extra["rules"] = [{"id": "hz", "when": True,
                           "then": [{"set": {"entity": "number.hz", "value": {"var": "surplus"}}}]}]
    img.extra["rule_resolve"] = _res({"surplus": 1234.5})
    cmds = CommandSet()
    RuleController().process(img, cmds)
    assert {c.entity: c for c in cmds.commands()}["number.hz"].value == 1234.5


def test_rule_constrain_with_priority_overrides_builtin_target():
    img = ProcessImage(now=0.0)
    img.extra["rules"] = [{"id": "cap", "when": True, "priority": 99,
                           "then": [{"constrain": {"entity": "number.bd", "hi": 0}}]}]
    img.extra["rule_resolve"] = _res({})
    cmds = CommandSet()
    cmds.current_priority = 1
    cmds.add(Command("number.bd", "set", 800.0, "peak"))    # lower-priority target
    RuleController().process(img, cmds)
    assert {c.entity: c for c in cmds.commands()}["number.bd"].value == 0.0   # rule bound wins


def test_bad_rule_is_skipped_others_still_run():
    img = ProcessImage(now=0.0)
    img.extra["rules"] = [
        {"id": "bad", "when": {"nope": [1]}, "then": [{"switch": {"entity": "switch.a", "on": True}}]},
        {"id": "ok", "when": True, "then": [{"switch": {"entity": "switch.b", "on": True}}]},
    ]
    img.extra["rule_resolve"] = _res({})
    cmds = CommandSet()
    RuleController().process(img, cmds)
    out = {c.entity for c in cmds.commands()}
    assert "switch.b" in out and "switch.a" not in out      # bad rule isolated
