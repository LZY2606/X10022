"""Directed tests for LazyConfigValue / default / validate evaluation order
and caching boundaries.

These tests record the exact callback order across the three entry paths
(class-hierarchy config inheritance, multiple Config merges, first read vs
explicit assignment) and pin the isolation boundaries: a lazy config value
must never leak state between two instances, two subclasses, or two Config
objects.
"""

import pytest

from traitlets import Dict, Int, List, TraitError, default, observe, validate
from traitlets.config import Config, Configurable
from traitlets.config.loader import LazyConfigValue


class OrderRecorder(Configurable):
    xs = List(default_value=[1]).tag(config=True)
    n = Int(0).tag(config=True)

    def __init__(self, **kwargs):
        self.events = []
        super().__init__(**kwargs)

    @default("xs")
    def _xs_default(self):
        self.events.append("default:xs")
        return [1]

    @validate("xs")
    def _xs_validate(self, proposal):
        self.events.append(f"validate:xs:{proposal['value']}")
        return proposal["value"]

    @observe("xs")
    def _xs_observe(self, change):
        self.events.append(f"observe:xs:{change['type']}:{change['old']}->{change['new']}")


def test_config_load_callback_order():
    """Config load: @default (merge base) -> @validate once on the merged
    value -> observer with old=default, new=merged."""
    cfg = Config()
    cfg.OrderRecorder.xs.append(2)
    obj = OrderRecorder(config=cfg)
    assert obj.xs == [1, 2]
    assert obj.events == [
        "default:xs",
        "validate:xs:[1, 2]",
        "observe:xs:change:[1]->[1, 2]",
    ]


def test_first_read_callback_order():
    """First read of an unconfigured trait: @default runs once, the result is
    cached, and neither @validate nor change observers fire for the default."""
    obj = OrderRecorder()
    assert obj.events == []
    assert obj.xs == [1]
    assert obj.events == ["default:xs"]
    # second read is served from the instance cache: nothing re-run
    assert obj.xs == [1]
    assert obj.events == ["default:xs"]


def test_explicit_assignment_callback_order():
    """Explicit assignment: @validate runs on the assigned value, then the
    observer fires. @default never runs because no read needed the default,
    so the change event's old value is Undefined (container defaults are
    dynamic and only materialize on first read)."""
    obj = OrderRecorder()
    obj.xs = [9]
    assert obj.events == [
        "validate:xs:[9]",
        "observe:xs:change:traitlets.Undefined->[9]",
    ]
    # once the default has been materialized by a read, later assignments
    # report it as the old value
    obj2 = OrderRecorder()
    assert obj2.xs == [1]
    obj2.events.clear()
    obj2.xs = [9]
    assert obj2.events == [
        "validate:xs:[9]",
        "observe:xs:change:[1]->[9]",
    ]


def test_validate_error_during_config_load_rolls_back():
    """A failing @validate during config load propagates a TraitError that
    names the trait, and a fresh instance is unaffected."""

    class Picky(Configurable):
        xs = List([1]).tag(config=True)

        @validate("xs")
        def _xs_validate(self, proposal):
            if len(proposal["value"]) > 2:
                raise TraitError("too many items")
            return proposal["value"]

    cfg = Config()
    cfg.Picky.xs.extend([2, 3, 4])
    with pytest.raises(TraitError, match="too many items"):
        Picky(config=cfg)
    # without the offending config the same class loads fine
    ok = Picky(config=Config())
    assert ok.xs == [1]


def test_lazy_value_not_shared_between_instances():
    """Two instances sharing one Config get independent containers: the lazy
    value is reified per instance and never served from a shared cache."""
    cfg = Config()
    cfg.OrderRecorder.xs.append(2)
    first = OrderRecorder(config=cfg)
    second = OrderRecorder(config=cfg)
    assert first.xs == [1, 2]
    assert second.xs == [1, 2]
    assert first.xs is not second.xs
    first.xs.append(99)
    assert second.xs == [1, 2]
    # a third instance created after the mutation is unaffected
    third = OrderRecorder(config=cfg)
    assert third.xs == [1, 2]


def test_lazy_value_respects_subclass_default():
    """Most dangerous counterexample: one Config, two classes in a hierarchy
    with different defaults.  The lazy value must merge into *each* class's
    own default, not reuse the first instance's merged result."""
    cfg = Config()
    cfg.OrderRecorder.xs.append(2)

    class Child(OrderRecorder):
        @default("xs")
        def _xs_default(self):
            return [10]

    base = OrderRecorder(config=cfg)
    child = Child(config=cfg)
    assert base.xs == [1, 2]
    assert child.xs == [10, 2]
    # and in the opposite instantiation order
    child2 = Child(config=cfg)
    base2 = OrderRecorder(config=cfg)
    assert child2.xs == [10, 2]
    assert base2.xs == [1, 2]


def test_lazy_dict_update_respects_subclass_default():
    """Dict counterpart of the subclass-default counterexample."""

    class Holder(Configurable):
        d = Dict({"a": 1}).tag(config=True)

    class ChildHolder(Holder):
        @default("d")
        def _d_default(self):
            return {"z": 0}

    cfg = Config()
    cfg.Holder.d.update({"b": 2})
    base = Holder(config=cfg)
    child = ChildHolder(config=cfg)
    assert base.d == {"a": 1, "b": 2}
    assert child.d == {"z": 0, "b": 2}
    assert child.d is not base.d


def test_class_hierarchy_config_sections_compound():
    """Class hierarchy path: parent and child sections both apply to a child
    instance; child section wins on clashes, lazy ops from both compound in
    precedence order."""

    class Parent(Configurable):
        n = Int(0).tag(config=True)
        xs = List([1]).tag(config=True)

    class Child(Parent):
        pass

    cfg = Config()
    cfg.Parent.n = 1
    cfg.Child.n = 2
    cfg.Parent.xs.append(10)
    cfg.Child.xs.append(20)
    child = Child(config=cfg)
    assert child.n == 2
    assert child.xs == [1, 10, 20]
    # the parent itself only sees its own section
    parent = Parent(config=cfg)
    assert parent.n == 1
    assert parent.xs == [1, 10]


def test_multiple_config_merge_does_not_mutate_source():
    """Multiple-Config path: merging must not mutate or alias the source
    Config's lazy value.  In ``target.merge(source)`` the source has the
    higher precedence, so its appends land after the target's."""
    target = Config()
    target.OrderRecorder.xs.append(2)
    source = Config()
    source.OrderRecorder.xs.append(3)
    target.merge(source)
    # source is untouched by the merge
    assert source.OrderRecorder.xs.to_dict() == {"extend": [3]}
    assert target.OrderRecorder.xs.to_dict() == {"extend": [2, 3]}
    # no aliasing: mutating the source afterwards must not leak
    source.OrderRecorder.xs.append(99)
    assert target.OrderRecorder.xs.to_dict() == {"extend": [2, 3]}
    # and the merged config drives instances correctly
    obj = OrderRecorder(config=target)
    assert obj.xs == [1, 2, 3]


def test_merge_update_not_dropped_when_later_lazy_has_no_update():
    """Regression: an earlier config's dict update must survive a merge even
    when the later config's lazy value only recorded list operations."""
    earlier = Config()
    earlier.OrderRecorder.xs.update({"a": 1})
    later = Config()
    later.OrderRecorder.xs.append(5)
    later.merge(earlier)
    merged = later.OrderRecorder.xs
    assert isinstance(merged, LazyConfigValue)
    assert merged.to_dict()["update"] == {"a": 1}


def test_merge_into_leaves_earlier_lazy_intact():
    """LazyConfigValue.merge_into treats its argument as read-only."""
    low = LazyConfigValue()
    low.append(1)
    low.update({"a": 1})
    high = LazyConfigValue()
    high.append(2)
    high.update({"b": 2})
    merged = high.merge_into(low)
    assert merged is high
    assert low.to_dict() == {"extend": [1], "update": {"a": 1}}
    assert merged.get_value([0]) == [0, 1, 2]
    # earlier update keys are kept, later keys win on clashes
    high2 = LazyConfigValue()
    high2.update({"a": 0})
    assert high2.merge_into(low).get_value({}) == {"a": 0}
    assert low.to_dict() == {"extend": [1], "update": {"a": 1}}


def test_get_value_reifies_fresh_container_per_call():
    """get_value never serves a cached result: each call derives from the
    initial value it is given."""
    lazy = LazyConfigValue()
    lazy.append(1)
    first = lazy.get_value([0])
    second = lazy.get_value([10])
    assert first == [0, 1]
    assert second == [10, 1]
    assert first is not second
    first.append(99)
    assert lazy.get_value([0]) == [0, 1]
