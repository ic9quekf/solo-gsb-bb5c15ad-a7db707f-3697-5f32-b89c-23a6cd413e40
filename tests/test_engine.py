"""Engine-level tests: priority levels, conflicts, minimal missing sets.

Run with: python tests/test_engine.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.engine import adjudicate  # noqa: E402
from app.validation import PolicyValidationError, validate_policy  # noqa: E402


def P(rules):
    return {"rules": validate_policy({"rules": rules})}


CMP = lambda f, op, v: {"field": f, "op": op, "value": v}


def test_decided_unique_conclusion():
    policy = P([
        {"id": "r1", "priority": 10, "conclusion": "allow",
         "expr": CMP("age", "ge", 18)},
        {"id": "r2", "priority": 5, "conclusion": "deny",
         "expr": CMP("age", "lt", 18)},
    ])
    verdict = adjudicate(policy, {"age": 20})
    assert verdict["status"] == "decided"
    assert verdict["conclusion"] == "allow"
    assert verdict["decided_priority"] == 10
    assert verdict["true_rule_ids"] == ["r1"]


def test_priority_skips_all_false_level():
    policy = P([
        {"id": "high", "priority": 100, "conclusion": "A",
         "expr": CMP("x", "eq", 1)},
        {"id": "low", "priority": 1, "conclusion": "B",
         "expr": CMP("y", "eq", 2)},
    ])
    verdict = adjudicate(policy, {"x": 9, "y": 2})
    assert verdict["status"] == "decided"
    assert verdict["conclusion"] == "B"
    assert 100 in verdict["skipped_priorities"]


def test_conflict_same_level():
    policy = P([
        {"id": "a", "priority": 10, "conclusion": "yes",
         "expr": CMP("v", "eq", 1)},
        {"id": "b", "priority": 10, "conclusion": "no",
         "expr": CMP("v", "ne", 2)},
    ])
    verdict = adjudicate(policy, {"v": 1})
    assert verdict["status"] == "conflict"
    assert set(verdict["conflicting_conclusions"]) == {"yes", "no"}
    assert set(verdict["true_rule_ids"]) == {"a", "b"}


def test_no_match():
    policy = P([
        {"id": "a", "priority": 1, "conclusion": "x",
         "expr": CMP("a", "eq", 1)},
    ])
    verdict = adjudicate(policy, {"a": 2})
    assert verdict["status"] == "no_match"
    assert verdict["conclusion"] is None


def test_unknown_returns_minimal_sets_single_rule():
    policy = P([
        {"id": "a", "priority": 1, "conclusion": "c",
         "expr": {"all": [CMP("x", "gt", 1), CMP("y", "gt", 2)]}},
    ])
    verdict = adjudicate(policy, {})
    assert verdict["status"] == "unknown"
    assert verdict["decided_priority"] == 1
    # To decide (true OR false): either field suffices to make it false, or
    # both make it true. Minimal sets are therefore {x} and {y}.
    sets = verdict["minimal_missing_field_sets"]
    assert sets == [["x"], ["y"]]
    assert set(verdict["missing_fields"]) == {"x", "y"}


def test_unknown_combination_of_two_rules():
    # Rule A: x > 0 -> "allow"; Rule B: y > 0 -> "deny".
    # Making the level decidable means every rule in it becomes known, so
    # both fields must be provided: minimal set is exactly {x, y}.
    policy = P([
        {"id": "a", "priority": 1, "conclusion": "allow",
         "expr": CMP("x", "gt", 0)},
        {"id": "b", "priority": 1, "conclusion": "deny",
         "expr": CMP("y", "gt", 0)},
    ])
    verdict = adjudicate(policy, {})
    assert verdict["status"] == "unknown"
    sets = [tuple(s) for s in verdict["minimal_missing_field_sets"]]
    assert sets == [("x", "y")]


def test_unknown_requires_both_when_rules_are_conjunctive():
    # Rule A fires only if x>0; rule B fires only if y>0; both only fire when
    # both fields are present AND positive... but single fields can still make
    # rules false. Use ALL-of-two inside each to force needing both fields for
    # a true witness; however a false witness for one rule needs one field.
    # Construct: A = (x>0 AND x<10)  B = (y>0 AND y<10)
    policy = P([
        {"id": "a", "priority": 1, "conclusion": "allow",
         "expr": {"all": [CMP("x", "gt", 0), CMP("x", "lt", 10)]}},
        {"id": "b", "priority": 1, "conclusion": "deny",
         "expr": {"all": [CMP("y", "gt", 0), CMP("y", "lt", 10)]}},
    ])
    verdict = adjudicate(policy, {})
    # For one rule, deciding families: T needs {x} (both atoms same field);
    # F needs {x} too. Each rule only depends on its own field.
    sets = [tuple(s) for s in verdict["minimal_missing_field_sets"]]
    assert sets == [("x", "y")]


def test_impossible_missing_set_is_excluded():
    # Rule: x>10 AND x<5 (never true) — deciding it false still requires x
    # (e.g. x=0 makes both comparisons false). The true witness must be absent.
    policy = P([
        {"id": "a", "priority": 1, "conclusion": "c",
         "expr": {"all": [CMP("x", "gt", 10), CMP("x", "lt", 5)]}},
    ])
    verdict = adjudicate(policy, {})
    assert verdict["status"] == "unknown"
    assert verdict["minimal_missing_field_sets"] == [["x"]]


def test_higher_level_unknown_blocks_lower_level():
    policy = P([
        {"id": "high", "priority": 100, "conclusion": "A",
         "expr": CMP("x", "eq", 1)},
        {"id": "low", "priority": 1, "conclusion": "B",
         "expr": CMP("y", "eq", 2)},
    ])
    verdict = adjudicate(policy, {"y": 2})
    assert verdict["status"] == "unknown"
    assert verdict["decided_priority"] == 100
    assert verdict["minimal_missing_field_sets"] == [["x"]]


def test_unknown_level_trace_contains_node_traces():
    policy = P([
        {"id": "a", "priority": 1, "conclusion": "c",
         "expr": {"not": CMP("x", "eq", 1)}},
    ])
    verdict = adjudicate(policy, {})
    rule_trace = verdict["level_trace"]["rules"][0]
    assert rule_trace["status"] == "unknown"
    assert rule_trace["expr"]["op"] == "not"
    assert rule_trace["expr"]["child"]["field"] == "x"
    assert verdict["level_trace"]["atoms"][0]["field"] == "x"


def test_known_true_rule_neutral_in_unknown_level():
    # One rule TRUE (fires) and another UNKNOWN: the level cannot be resolved
    # yet; the only deciding field is the unknown rule's field.
    policy = P([
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
        {"id": "b", "priority": 10, "conclusion": "deny",
         "expr": CMP("y", "eq", 2)},
    ])
    verdict = adjudicate(policy, {"x": 1})
    assert verdict["status"] == "unknown"
    assert verdict["minimal_missing_field_sets"] == [["y"]]
    assert verdict["level_trace"]["true_rule_ids"] == ["a"]
    assert set(verdict["pending_conclusions"]) == {"allow", "deny"}


def test_known_false_rules_do_not_appear_in_level():
    policy = P([
        {"id": "a", "priority": 10, "conclusion": "allow",
         "expr": CMP("x", "eq", 1)},
        {"id": "b", "priority": 10, "conclusion": "deny",
         "expr": CMP("y", "eq", 2)},
    ])
    verdict = adjudicate(policy, {"x": 9, "y": 2})
    assert verdict["status"] == "decided"
    assert verdict["conclusion"] == "deny"
    assert verdict["true_rule_ids"] == ["b"]


def test_null_fact_is_unknown():
    policy = P([
        {"id": "a", "priority": 1, "conclusion": "c",
         "expr": CMP("x", "eq", 1)},
    ])
    verdict = adjudicate(policy, {"x": None})
    assert verdict["status"] == "unknown"
    assert verdict["minimal_missing_field_sets"] == [["x"]]


def test_complex_conjunction_minimal_sets():
    # Rule: any[ (a AND b), (a AND c), d ]
    policy = P([
        {"id": "r", "priority": 1, "conclusion": "c",
         "expr": {"any": [
             {"all": [CMP("a", "eq", 1), CMP("b", "eq", 1)]},
             {"all": [CMP("a", "eq", 1), CMP("c", "eq", 1)]},
             CMP("d", "eq", 1),
         ]}},
    ])
    verdict = adjudicate(policy, {})
    # Decide true:  {d} or {a,b} or {a,c}; decide false: {a,b,c,d}.
    # Inclusion-minimal: [a,b],[a,c],[d].
    sets = sorted(tuple(s) for s in verdict["minimal_missing_field_sets"])
    assert sets == [("a", "b"), ("a", "c"), ("d",)]


def test_type_mismatch_is_false_not_unknown():
    policy = P([
        {"id": "a", "priority": 1, "conclusion": "c",
         "expr": CMP("x", "lt", "y")},
    ])
    verdict = adjudicate(policy, {"x": 1, "y": "s"})
    assert verdict["status"] == "no_match"


def test_validation_rejects_bad_policy():
    bad_cases = [
        {},
        {"rules": []},
        {"rules": [{"priority": 1, "conclusion": "c",
                    "expr": CMP("x", "badop", 1)}]},
        {"rules": [{"priority": "high", "conclusion": "c",
                    "expr": CMP("x", "eq", 1)}]},
        {"rules": [{"priority": 1, "conclusion": "",
                    "expr": CMP("x", "eq", 1)}]},
        {"rules": [{"id": "dup", "priority": 1, "conclusion": "c",
                    "expr": CMP("x", "eq", 1)},
                   {"id": "dup", "priority": 2, "conclusion": "d",
                    "expr": CMP("x", "eq", 2)}]},
        {"rules": [{"priority": 1, "conclusion": "c",
                    "expr": {"all": []}}]},
        {"rules": [{"priority": 1, "conclusion": "c",
                    "expr": {"all": [CMP("x", "eq", 1)], "bogus": True}}]},
    ]
    for case in bad_cases:
        try:
            validate_policy(case)
        except PolicyValidationError:
            continue
        raise AssertionError(f"should have rejected: {case}")


def test_validation_accepts_good_policy():
    rules = validate_policy({"rules": [
        {"priority": 0, "conclusion": "default",
         "expr": {"any": [
             {"all": [CMP("a", "ge", 1), {"not": CMP("b", "eq", "s")}]},
             CMP("c", "ne", None),
         ]}},
    ]})
    assert rules[0]["id"] == "rule-1"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            import traceback
            traceback.print_exc()
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
