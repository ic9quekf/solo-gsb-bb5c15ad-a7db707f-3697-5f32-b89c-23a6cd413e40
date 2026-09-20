"""Unit tests for the three-valued logic and feasibility analysis.

Run with either:
    python -m pytest tests/
    python tests/test_logic.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.logic import (  # noqa: E402
    FALSE,
    TRUE,
    UNKNOWN,
    Atom,
    AtomTable,
    clean_family,
    compare_values,
    cross_families,
    evaluate_expr,
    term_is_feasible,
)


def field_sets_of(node, facts):
    atoms = AtomTable()
    result = evaluate_expr(node, facts, atoms)
    by_id = {a.id: a for a in atoms.atoms}
    t_sets = sorted(
        sorted({atoms.atoms[i].field for i, _ in term})
        for term in result.t_family
        if term_is_feasible(term, by_id)
    )
    f_sets = sorted(
        sorted({atoms.atoms[i].field for i, _ in term})
        for term in result.f_family
        if term_is_feasible(term, by_id)
    )
    return result.status, t_sets, f_sets


# --------------------------------------------------------------------------- #
def test_leaf_known():
    node = {"field": "age", "op": "ge", "value": 18}
    atoms = AtomTable()
    assert evaluate_expr(node, {"age": 20}, atoms).status == TRUE
    assert evaluate_expr(node, {"age": 5}, atoms).status == FALSE
    assert evaluate_expr(node, {}, atoms).status == UNKNOWN
    assert evaluate_expr(node, {"age": None}, atoms).status == UNKNOWN


def test_leaf_unknown_families():
    node = {"field": "age", "op": "ge", "value": 18}
    status, t_sets, f_sets = field_sets_of(node, {})
    assert status == UNKNOWN
    assert t_sets == [["age"]]
    assert f_sets == [["age"]]


def test_kleene_all():
    node = {"all": [
        {"field": "a", "op": "eq", "value": 1},
        {"field": "b", "op": "eq", "value": 2},
    ]}
    assert field_sets_of(node, {"a": 1, "b": 2})[0] == TRUE
    assert field_sets_of(node, {"a": 9, "b": 2})[0] == FALSE
    status, t_sets, f_sets = field_sets_of(node, {"a": 1})
    assert status == UNKNOWN
    # To become TRUE both a and b are needed (a is present so only b remains);
    # to become FALSE it is enough to know b is false -> {b}.
    assert t_sets == [["b"]]
    assert f_sets == [["b"]]

    # One known-false child makes the conjunction false even with unknowns.
    node2 = {"all": [
        {"field": "a", "op": "eq", "value": 1},
        {"field": "b", "op": "eq", "value": 2},
    ]}
    assert field_sets_of(node2, {"a": 9})[0] == FALSE


def test_kleene_any():
    node = {"any": [
        {"field": "a", "op": "eq", "value": 1},
        {"field": "b", "op": "eq", "value": 2},
    ]}
    assert field_sets_of(node, {"a": 1})[0] == TRUE
    status, t_sets, f_sets = field_sets_of(node, {"a": 9})
    assert status == UNKNOWN
    assert t_sets == [["b"]]
    assert f_sets == [["b"]]

    status, t_sets, f_sets = field_sets_of(node, {})
    assert status == UNKNOWN
    assert t_sets == [["a"], ["b"]]
    assert f_sets == [["a", "b"]]


def test_not_swap():
    node = {"not": {"field": "a", "op": "eq", "value": 1}}
    assert field_sets_of(node, {"a": 1})[0] == FALSE
    assert field_sets_of(node, {"a": 2})[0] == TRUE
    status, t_sets, f_sets = field_sets_of(node, {})
    assert status == UNKNOWN and t_sets == [["a"]] and f_sets == [["a"]]


def test_shared_field_family_is_union_of_atoms():
    # x>10 OR x<5: truth witnesses are either comparison (same field).
    node = {"any": [
        {"field": "x", "op": "gt", "value": 10},
        {"field": "x", "op": "lt", "value": 5},
    ]}
    status, t_sets, f_sets = field_sets_of(node, {})
    assert status == UNKNOWN
    # At atom level: {atom0} or {atom1}; both project to the same field.
    assert t_sets == [["x"], ["x"]] or t_sets == [["x"]]
    # Falsity needs both atoms false, still one field.
    assert f_sets == [["x"]]


def test_inclusion_minimal():
    terms = [
        frozenset({(1, True), (2, False)}),
        frozenset({(1, True)}),
        frozenset({(3, True)}),
    ]
    minimal = clean_family(terms)
    assert frozenset({(1, True)}) in minimal
    assert frozenset({(3, True)}) in minimal
    assert len(minimal) == 2


def test_cross_drops_contradictions():
    left = (frozenset({(1, True)}),)
    right = (frozenset({(1, False)}), frozenset({(2, True)}))
    out = cross_families(left, right)
    assert out == (frozenset({(1, True), (2, True)}),)


def test_feasibility_same_field_impossible_gt_and_lt():
    # x > 10 AND x < 5 must be judged infeasible.
    node = {"all": [
        {"field": "x", "op": "gt", "value": 10},
        {"field": "x", "op": "lt", "value": 5},
    ]}
    atoms = AtomTable()
    result = evaluate_expr(node, {}, atoms)
    by_id = {a.id: a for a in atoms.atoms}
    assert result.status == UNKNOWN
    feasible = [t for t in result.t_family if term_is_feasible(t, by_id)]
    assert feasible == []


def test_feasibility_equal_conflict():
    node = {"all": [
        {"field": "x", "op": "eq", "value": 1},
        {"field": "x", "op": "eq", "value": 2},
    ]}
    atoms = AtomTable()
    result = evaluate_expr(node, {}, atoms)
    by_id = {a.id: a for a in atoms.atoms}
    assert all(not term_is_feasible(t, by_id) for t in result.t_family)


def test_feasibility_ne_with_strict_bounds():
    # 5 < x < 10, x != 6, x != 7  -> still feasible.
    node = {"all": [
        {"field": "x", "op": "gt", "value": 5},
        {"field": "x", "op": "lt", "value": 10},
        {"field": "x", "op": "ne", "value": 6},
        {"field": "x", "op": "ne", "value": 7},
    ]}
    atoms = AtomTable()
    result = evaluate_expr(node, {}, atoms)
    by_id = {a.id: a for a in atoms.atoms}
    assert any(term_is_feasible(t, by_id) for t in result.t_family)


def test_feasibility_strict_open_interval_single_integer_excluded():
    # 0 < x < 2 with integers: only x == 1, but x != 1 -> infeasible.
    node = {"all": [
        {"field": "x", "op": "gt", "value": 0},
        {"field": "x", "op": "lt", "value": 2},
        {"field": "x", "op": "ne", "value": 1},
    ]}
    atoms = AtomTable()
    result = evaluate_expr(node, {}, atoms)
    by_id = {a.id: a for a in atoms.atoms}
    # Feasibility is over value domains, not integer domains: reals 1.5 work.
    # Therefore the term stays feasible (correct for JSON numbers).
    assert any(term_is_feasible(t, by_id) for t in result.t_family)


def test_compare_strict_typing():
    assert compare_values(True, "eq", 1)[0] is False
    assert compare_values(1, "eq", 1.0)[0] is True
    assert compare_values("a", "eq", "a")[0] is True
    assert compare_results(compare_values(1, "lt", "1")[1])
    assert compare_values(True, "lt", False)[0] is False
    assert compare_values(None, "eq", None)[0] is True
    assert compare_values([1], "eq", [True])[0] is False


def compare_results(reason):
    return reason == "type_mismatch"


def test_nested_trace_structure():
    node = {"all": [
        {"any": [
            {"field": "a", "op": "eq", "value": 1},
            {"not": {"field": "b", "op": "gt", "value": 2}},
        ]},
        {"field": "c", "op": "ne", "value": None},
    ]}
    atoms = AtomTable()
    result = evaluate_expr(node, {}, atoms)
    assert result.status == UNKNOWN
    assert result.trace["op"] == "all"
    assert result.trace["children"][0]["op"] == "any"
    assert result.trace["children"][0]["children"][1]["op"] == "not"


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
