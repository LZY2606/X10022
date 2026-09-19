"""Targeted tests for LazyConfigValue / default / validate evaluation order
and cache boundaries.

These tests pin down, for every entry point that turns a Config object into
trait values on an instance:

- the order in which class defaults, ``@default`` generators, config
  increment operations, ``@validate`` cross-validators and ``@observe``
  callbacks run;
- the cache boundaries: a LazyConfigValue stored in a shared Config must
  never leak reified state between two instances, and merging configs must
  never mutate or alias the sources.

Several of these tests are executable counterexamples: they fail against
the pre-fix implementation where ``LazyConfigValue.get_value`` memoized its
result on the shared lazy object and ``merge_into`` mutated its operands.
"""

from __future__ import annotations

import pytest

from traitlets import Dict, Int, List, Set, TraitError, default, observe, validate
from traitlets.config import Config, Configurable
from traitlets.config.loader import LazyConfigValue


class Recorder:
    """Append-only event log shared by the fixtures below."""

    def __init__(self):
        self.events = []

    def record(self, *event):
        self.events.append(event)

    def kinds(self):
        return [e[0] for e in self.events]


def make_traced_class(events):
    """A Configurable recording default/validate/observe callback order."""

    class Traced(Configurable):
        foo = List(config=True)

        @default("foo")
        def _foo_default(self):
            events.record("default")
            return [0]

        @validate("foo")
        def _foo_validate(self, proposal):
            events.record("validate", list(proposal.value))
            return proposal.value

        @observe("foo")
        def _foo_observe(self, change):
            events.record(f"observe:{change.type}", list(change.new))

        @observe("foo", type="default")
        def _foo_default_observe(self, change):
            events.record("observe:default", list(change.value))

    return Traced


# ---------------------------------------------------------------------------
# Callback order on the three entry paths
# ---------------------------------------------------------------------------


def test_callback_order_config_load():
    """Config path: default -> (lazy increments) -> validate -> observe."""
    events = Recorder()
    Traced = make_traced_class(events)
    c = Config()
    c.Traced.foo.append(1)

    obj = Traced(config=c)

    assert obj.foo == [0, 1]
    # @default runs first (to provide the increments' base). The "default"
    # notification fires synchronously from TraitType.get: it goes through
    # _notify_observers directly and is NOT held by hold_trait_notifications.
    # @validate then sees the fully merged value exactly once (deferred to
    # the end of the notification hold), and finally the change observer.
    assert events.kinds() == ["default", "observe:default", "validate", "observe:change"]
    assert ("validate", [0, 1]) in events.events
    # the change notification spans default -> configured value
    assert ("observe:change", [0, 1]) in events.events


def test_callback_order_first_read():
    """First-read path: default is computed once, cached, and not re-validated
    by the @validate cross-validator (cross-validation lock is held)."""
    events = Recorder()
    Traced = make_traced_class(events)
    obj = Traced()

    assert obj.foo == [0]
    assert events.kinds() == ["default", "observe:default"]
    # second read is served from the instance cache: no further events
    assert obj.foo == [0]
    assert events.kinds() == ["default", "observe:default"]


def test_callback_order_explicit_assignment():
    """Explicit assignment: validate runs synchronously before the observer."""
    events = Recorder()
    Traced = make_traced_class(events)
    obj = Traced()
    events.events.clear()

    obj.foo = [5]

    assert events.kinds() == ["validate", "observe:change"]
    assert ("validate", [5]) in events.events
    assert ("observe:change", [5]) in events.events


def test_validate_sees_merged_value_not_default():
    """The @validate cross-validator must run on default+increments, once."""
    seen = []

    class C(Configurable):
        foo = List([1], config=True)

        @validate("foo")
        def _v(self, proposal):
            seen.append(list(proposal.value))
            return proposal.value

    c = Config()
    c.C.foo.extend([2, 3])
    obj = C(config=c)
    assert obj.foo == [1, 2, 3]
    assert seen == [[1, 2, 3]]


# ---------------------------------------------------------------------------
# Cache boundary: no pollution between two instances
# ---------------------------------------------------------------------------


def test_two_instances_per_instance_dynamic_default():
    """Executable counterexample: two instances of one class share a single
    Config; each has a per-instance dynamic default. The lazy object must be
    reified against each instance's own default, not the first instance's."""
    events = Recorder()

    class C(Configurable):
        foo = List(config=True)
        n = Int(0)

        @default("foo")
        def _foo_default(self):
            events.record("default", self.n)
            return [self.n]

    c = Config()
    c.C.foo.append(99)

    first = C(config=c, n=7)
    second = C(config=c, n=8)

    assert first.foo == [7, 99]
    # pre-fix, the shared LazyConfigValue cached [7, 99] and the second
    # instance observed the first instance's default.
    assert second.foo == [8, 99]


def test_two_classes_sharing_one_config():
    """Two different Configurable classes with different defaults must each
    apply the increments to their own default."""

    class A(Configurable):
        foo = List([1, 2], config=True)

    class B(Configurable):
        foo = List([100], config=True)

    c = Config()
    c.A.foo.append(3)
    c.B.foo.append(3)

    assert A(config=c).foo == [1, 2, 3]
    assert B(config=c).foo == [100, 3]


def test_instance_values_not_shared_with_config_or_each_other():
    """Mutating one instance's container must not touch the other instance
    nor the lazy object still stored in the Config."""

    class C(Configurable):
        foo = List([0], config=True)

    c = Config()
    c.C.foo.append(1)
    one = C(config=c)
    two = C(config=c)

    one.foo.append("mine")

    assert two.foo == [0, 1]
    assert c.C.foo._extend == [1]
    # a third instance still gets a pristine merge
    assert C(config=c).foo == [0, 1]


def test_lazy_object_has_no_reified_state_after_load():
    """The lazy object in the Config stays a pure op-recorder after use."""

    class C(Configurable):
        foo = List([0], config=True)

    c = Config()
    c.C.foo.append(1)
    C(config=c)

    lazy = c.C.foo
    assert isinstance(lazy, LazyConfigValue)
    assert not hasattr(lazy, "_value")
    assert lazy.to_dict() == {"extend": [1]}


def test_dict_and_set_increments_isolated_between_instances():
    class C(Configurable):
        d = Dict({"a": 1}, config=True)
        s = Set({"x"}, config=True)

    c = Config()
    c.C.d.update({"b": 2})
    c.C.s.add("y")

    one = C(config=c)
    two = C(config=c)
    assert one.d == {"a": 1, "b": 2}
    assert two.d == {"a": 1, "b": 2}
    one.d["c"] = 3
    one.s.add("z")
    assert two.d == {"a": 1, "b": 2}
    assert two.s == {"x", "y"}


# ---------------------------------------------------------------------------
# Multiple stacked configs: composition without source pollution
# ---------------------------------------------------------------------------


def test_merge_does_not_mutate_or_alias_sources():
    """Executable counterexample: merging configs must leave both sources
    usable; pre-fix, merge_into mutated the lower-precedence lazy object and
    aliased its operation lists with the merged result."""
    c1 = Config()
    c1.C.foo.append(1)
    c2 = Config()
    c2.C.foo.append(2)
    lazy1 = c1.C.foo
    lazy2 = c2.C.foo

    c = Config()
    c.merge(c1)
    c.merge(c2)

    # the original lazy objects are untouched by the merges
    assert lazy1._extend == [1]
    assert lazy2._extend == [2]
    # merged result composes in precedence order
    assert c.C.foo._extend == [1, 2]

    # later edits to a source lazy object must not leak into the merged
    # config, and edits to the merged result must not leak back either
    lazy2.append(99)
    assert c.C.foo._extend == [1, 2]
    c.C.foo._extend.append(0)
    assert lazy1._extend == [1]
    assert lazy2._extend == [2, 99]

    # nor into values reified from the merged config

    class C(Configurable):
        foo = List([0], config=True)

    assert C(config=c).foo == [0, 1, 2, 0]


def test_stacked_configs_prepend_extend_and_update():
    c1 = Config()
    c1.C.lis.prepend([1])
    c1.C.d.update({"a": 1})
    c2 = Config()
    c2.C.lis.extend([2])
    c2.C.d.update({"b": 2})
    lazy_d1 = c1.C.d
    lazy_d2 = c2.C.d

    c = Config()
    c.merge(c1)
    c.merge(c2)

    class C(Configurable):
        lis = List([0], config=True)
        d = Dict(config=True)

    obj = C(config=c)
    assert obj.lis == [1, 0, 2]
    assert obj.d == {"a": 1, "b": 2}
    # the sources' recorded operations are not polluted by the merge
    assert lazy_d1.to_dict() == {"update": {"a": 1}}
    assert lazy_d2.to_dict() == {"update": {"b": 2}}


def test_merge_into_is_pure_function():
    low = LazyConfigValue()
    low.append(1)
    high = LazyConfigValue()
    high.append(2)

    merged = high.merge_into(low)

    assert merged is not low
    assert merged is not high
    assert merged._extend == [1, 2]
    assert low._extend == [1]
    assert high._extend == [2]
    # no aliasing between operands and result
    merged._extend.append(3)
    assert low._extend == [1]
    assert high._extend == [2]


# ---------------------------------------------------------------------------
# Class hierarchy inheritance
# ---------------------------------------------------------------------------


def test_class_hierarchy_sections_compose_in_mro_order():
    class Base(Configurable):
        foo = List([0], config=True)

    class Derived(Base):
        pass

    c = Config()
    c.Base.foo.append(1)
    c.Derived.foo.append(2)

    assert Derived(config=c).foo == [0, 1, 2]
    # the base class only sees its own section
    assert Base(config=c).foo == [0, 1]


def test_subclass_section_overrides_plain_value():
    class Base(Configurable):
        n = Int(1, config=True)

    class Derived(Base):
        pass

    c = Config()
    c.Base.n = 2
    c.Derived.n = 3
    assert Derived(config=c).n == 3
    assert Base(config=c).n == 2


# ---------------------------------------------------------------------------
# Repeat loads: idempotency is an instance-level cache, not a global one
# ---------------------------------------------------------------------------


def test_repeated_update_config_does_not_double_apply():
    class C(Configurable):
        foo = List([0], config=True)

    c = Config()
    c.C.foo.append(1)
    obj = C(config=c)
    assert obj.foo == [0, 1]

    obj.update_config(c)
    assert obj.foo == [0, 1]
    obj.update_config(c)
    assert obj.foo == [0, 1]


def test_update_config_with_new_increments_applies_on_top():
    class C(Configurable):
        foo = List([0], config=True)

    c1 = Config()
    c1.C.foo.append(1)
    obj = C(config=c1)

    c2 = Config()
    c2.C.foo.append(2)
    obj.update_config(c2)
    assert obj.foo == [0, 1, 2]


# ---------------------------------------------------------------------------
# Error propagation keeps diagnosable context
# ---------------------------------------------------------------------------


def test_invalid_config_value_raises_with_trait_context():
    class C(Configurable):
        n = Int(0, config=True)

    c = Config()
    c.C.n = "not-an-int"
    with pytest.raises(TraitError) as excinfo:
        C(config=c)
    msg = str(excinfo.value)
    assert "n" in msg
    assert "C" in msg or "int" in msg


def test_validate_error_during_config_load_propagates():
    class C(Configurable):
        foo = List([0], config=True)

        @validate("foo")
        def _v(self, proposal):
            if len(proposal.value) > 2:
                raise TraitError(f"foo too long: {proposal.value!r}")
            return proposal.value

    c = Config()
    c.C.foo.extend([1, 2])
    with pytest.raises(TraitError, match="foo too long"):
        C(config=c)


def test_get_value_type_mismatch_leaves_initial_unchanged():
    """Increments recorded for the wrong container type are ignored by
    get_value (documented boundary), they must not corrupt the default."""
    lazy = LazyConfigValue()
    lazy.append(1)
    assert lazy.get_value({"a": 1}) == {"a": 1}
    assert lazy.get_value([0]) == [0, 1]


def test_explicit_assignment_after_config_uses_config_value_as_old():
    """Observers see the configured value as `old` after config load."""
    changes = []

    class C(Configurable):
        foo = List([0], config=True)

    c = Config()
    c.C.foo.append(1)
    obj = C(config=c)
    obj.observe(lambda change: changes.append((list(change.old), list(change.new))), "foo")
    obj.foo = [9]
    assert changes == [([0, 1], [9])]


def test_kwargs_beat_config_for_same_trait():
    """Explicit constructor kwargs are re-applied after config loading."""

    class C(Configurable):
        foo = List([0], config=True)

    c = Config()
    c.C.foo.append(1)
    obj = C(config=c, foo=[5])
    assert obj.foo == [5]
