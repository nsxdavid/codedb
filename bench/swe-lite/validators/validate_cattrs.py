"""cattrs-partial-structuring-recovery validator.

Tests the new `partial_structure` feature on BaseConverter / top-level cattrs,
returning a PartialResult with value/is_complete/structured_fields/failed_fields/
errors/error_map, plus PartialResult.refine, nested recursion, collection
atomicity, init=False exclusion, forbid_extra_keys, dataclass + TypedDict support.

Designed purely from the SPEC text. All checks are functional/observable.
"""
import sys, json, traceback

TASK_ID = "cattrs-partial-structuring-recovery"


def main():
    results = {"task": TASK_ID, "checks": {}}
    c = results["checks"]

    # --- import_cattrs (gate): module + new public symbol PartialResult ---
    try:
        import cattrs
        from cattrs import Converter, BaseConverter
        from cattrs import PartialResult  # spec: Export PartialResult
        c["import_cattrs"] = True
    except Exception as e:
        c["import_cattrs"] = False
        c["import_cattrs_err"] = f"{type(e).__name__}: {e}"
        return results

    from attrs import define, field
    from dataclasses import dataclass
    from typing import List, Dict, Optional, TypedDict

    # Build a fresh converter (top-level partial_structure also tested separately).
    def conv():
        return Converter()

    # ------------------------------------------------------------------
    # Check 1: a fully-valid attrs class -> complete result, value built
    # ------------------------------------------------------------------
    try:
        @define
        class Simple:
            a: int
            b: str

        r = conv().partial_structure({"a": 1, "b": "x"}, Simple)
        ok = (
            r.is_complete is True
            and r.value == Simple(a=1, b="x")
            and r.structured_fields == frozenset({"a", "b"})
            and r.failed_fields == frozenset()
            and r.errors is None
            and r.error_map == {}
        )
        c["complete_attrs"] = bool(ok)
    except Exception as e:
        c["complete_attrs"] = False
        c["complete_attrs_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 2: a field absent from input is FAILED, not structured.
    # Required field without default -> value is None.
    # ------------------------------------------------------------------
    try:
        @define
        class TwoReq:
            a: int
            b: int

        r = conv().partial_structure({"a": 1}, TwoReq)
        ok = (
            r.is_complete is False
            and "a" in r.structured_fields
            and "b" in r.failed_fields
            and "b" not in r.structured_fields
            and r.value is None  # required field b has no default
            # (error_map membership for an *absent* field is left unspecified
            #  by the spec, so it is intentionally not asserted here)
        )
        c["absent_field_failed_value_none"] = bool(ok)
    except Exception as e:
        c["absent_field_failed_value_none"] = False
        c["absent_field_failed_value_none_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 3: failed field WITH a default -> default used as fallback,
    # value is produced (not None).
    # ------------------------------------------------------------------
    try:
        @define
        class WithDefault:
            a: int
            b: int = 99

        r = conv().partial_structure({"a": 5}, WithDefault)
        ok = (
            r.value is not None
            and r.value.a == 5
            and r.value.b == 99  # default fallback
            and "a" in r.structured_fields
            and "b" in r.failed_fields  # absent -> failed even though defaulted
            and r.is_complete is False
        )
        c["default_fallback_produces_value"] = bool(ok)
    except Exception as e:
        c["default_fallback_produces_value"] = False
        c["default_fallback_produces_value_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 4: a field present but with a bad value -> failed, recorded in
    # error_map; if it has a default, value still built with default.
    # ------------------------------------------------------------------
    try:
        @define
        class BadValue:
            a: int
            b: int = 7

        r = conv().partial_structure({"a": "not-an-int", "b": 3}, BadValue)
        ok = (
            "a" in r.failed_fields
            and "a" in r.error_map
            and isinstance(r.error_map["a"], Exception)
            and "b" in r.structured_fields
            and r.is_complete is False
            # a required & failed -> value None
            and r.value is None
        )
        c["bad_value_failed_in_error_map"] = bool(ok)
    except Exception as e:
        c["bad_value_failed_in_error_map"] = False
        c["bad_value_failed_in_error_map_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 5: PartialResult.refine() fixes failed fields, preserving
    # already-structured fields, and can complete the result.
    # ------------------------------------------------------------------
    try:
        @define
        class Refinable:
            a: int
            b: int

        r1 = conv().partial_structure({"a": 1}, Refinable)
        r2 = r1.refine({"b": 2})
        ok = (
            r2 is not r1
            and isinstance(r2, PartialResult)
            and r2.is_complete is True
            and r2.value == Refinable(a=1, b=2)
            and "a" in r2.structured_fields  # preserved
            and "b" in r2.structured_fields  # newly fixed
            and r2.failed_fields == frozenset()
        )
        c["refine_fixes_failed_fields"] = bool(ok)
    except Exception as e:
        c["refine_fixes_failed_fields"] = False
        c["refine_fixes_failed_fields_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 6: nested attrs field partially structured recursively.
    # If nested obj is only partially complete, parent field is marked
    # failed but the partial nested value is used.
    # ------------------------------------------------------------------
    try:
        @define
        class Nested:
            x: int
            y: int = 0  # default so partial nested can still build a value

        @define
        class Parent:
            name: str
            child: Nested

        # child has x but not y... y has default so child partial value exists.
        r = conv().partial_structure(
            {"name": "p", "child": {"x": 10}}, Parent
        )
        ok = (
            "name" in r.structured_fields
            and "child" in r.failed_fields  # nested only partial -> parent failed
            and r.is_complete is False
            and r.value is not None
            and r.value.child is not None
            and r.value.child.x == 10  # partial nested value used
        )
        c["nested_partial_recursive"] = bool(ok)
    except Exception as e:
        c["nested_partial_recursive"] = False
        c["nested_partial_recursive_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 7: fully-valid nested attrs field -> structured, complete.
    # ------------------------------------------------------------------
    try:
        @define
        class Nested2:
            x: int
            y: int

        @define
        class Parent2:
            name: str
            child: Nested2

        r = conv().partial_structure(
            {"name": "p", "child": {"x": 10, "y": 20}}, Parent2
        )
        ok = (
            r.is_complete is True
            and "child" in r.structured_fields
            and r.value == Parent2(name="p", child=Nested2(x=10, y=20))
        )
        c["nested_complete"] = bool(ok)
    except Exception as e:
        c["nested_complete"] = False
        c["nested_complete_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 8: collection field (List) is atomic -- any element failure
    # fails the whole field.
    # ------------------------------------------------------------------
    try:
        @define
        class HasList:
            tags: List[int]
            name: str

        r = conv().partial_structure(
            {"tags": [1, "bad", 3], "name": "n"}, HasList
        )
        ok = (
            "tags" in r.failed_fields
            and "tags" not in r.structured_fields  # atomic failure
            and "name" in r.structured_fields
            and "tags" in r.error_map
            and r.is_complete is False
        )
        c["collection_atomic_failure"] = bool(ok)
    except Exception as e:
        c["collection_atomic_failure"] = False
        c["collection_atomic_failure_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 9: init=False fields excluded from structured/failed sets.
    # ------------------------------------------------------------------
    try:
        @define
        class HasInitFalse:
            a: int
            computed: int = field(init=False, default=0)

        r = conv().partial_structure({"a": 1}, HasInitFalse)
        ok = (
            "computed" not in r.structured_fields
            and "computed" not in r.failed_fields
            and "a" in r.structured_fields
            and r.is_complete is True  # only init field present & valid
        )
        c["init_false_excluded"] = bool(ok)
    except Exception as e:
        c["init_false_excluded"] = False
        c["init_false_excluded_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 10: forbid_extra_keys -> extra keys make is_complete False
    # but still produce a value.
    # ------------------------------------------------------------------
    try:
        @define
        class Strict:
            a: int
            b: int

        cf = Converter(forbid_extra_keys=True)
        r = cf.partial_structure({"a": 1, "b": 2, "extra": 9}, Strict)
        ok = (
            r.is_complete is False  # extra key
            and r.value is not None  # but value still produced
            and r.value.a == 1
            and r.value.b == 2
        )
        c["forbid_extra_keys_incomplete_but_value"] = bool(ok)
    except Exception as e:
        c["forbid_extra_keys_incomplete_but_value"] = False
        c["forbid_extra_keys_incomplete_but_value_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 11: dataclass support.
    # ------------------------------------------------------------------
    try:
        @dataclass
        class DC:
            a: int
            b: int = 42

        r = conv().partial_structure({"a": 3}, DC)
        ok = (
            "a" in r.structured_fields
            and "b" in r.failed_fields  # absent
            and r.value is not None
            and r.value.a == 3
            and r.value.b == 42  # default fallback
            and r.is_complete is False
        )
        c["dataclass_support"] = bool(ok)
    except Exception as e:
        c["dataclass_support"] = False
        c["dataclass_support_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 12: TypedDict support.
    # ------------------------------------------------------------------
    try:
        class TD(TypedDict):
            a: int
            b: int

        r = conv().partial_structure({"a": 1}, TD)
        ok = (
            "a" in r.structured_fields
            and "b" in r.failed_fields  # absent required
            and r.is_complete is False
        )
        c["typeddict_support"] = bool(ok)
    except Exception as e:
        c["typeddict_support"] = False
        c["typeddict_support_err"] = f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # Check 13: top-level cattrs.partial_structure exists & works like
    # the converter method.
    # ------------------------------------------------------------------
    try:
        @define
        class TopLevel:
            a: int
            b: str

        r = cattrs.partial_structure({"a": 1, "b": "x"}, TopLevel)
        ok = (
            isinstance(r, PartialResult)
            and r.is_complete is True
            and r.value == TopLevel(a=1, b="x")
        )
        c["top_level_partial_structure"] = bool(ok)
    except Exception as e:
        c["top_level_partial_structure"] = False
        c["top_level_partial_structure_err"] = f"{type(e).__name__}: {e}"

    return results


if __name__ == "__main__":
    try:
        r = main()
    except Exception as e:
        r = {"task": TASK_ID, "fatal": str(e), "traceback": traceback.format_exc()}
    print(json.dumps(r, indent=2))
