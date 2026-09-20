"""Batch data plans tests: combination semantics, minimality, caps, validation.

Run with: python tests/test_batch_data_plans.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.batch as batch  # noqa: E402
from app.main import app  # noqa: E402
from app.store import store  # noqa: E402

client = TestClient(app)


def setup_function():
    store.clear()


POLICY = {
    "rules": [
        {
            "id": "adult-allow",
            "priority": 100,
            "conclusion": "allow",
            "expr": {
                "all": [
                    {"field": "age", "op": "ge", "value": 18},
                    {"field": "country", "op": "eq", "value": "NL"},
                ]
            },
        },
        {
            "id": "minor-deny",
            "priority": 100,
            "conclusion": "deny",
            "expr": {"field": "age", "op": "lt", "value": 18},
        },
        {
            "id": "vip-review",
            "priority": 50,
            "conclusion": "review",
            "expr": {"any": [
                {"field": "vip", "op": "eq", "value": True},
                {"field": "flag", "op": "eq", "value": True},
            ]},
        },
    ]
}


def create_policy(policy=None):
    r = client.post("/api/v1/policies", json=policy or POLICY)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def post_plans(pid, body):
    return client.post(f"/api/v1/policies/{pid}/batch-data-plans", json=body)


# --------------------------------------------------------------------------- #
# Pure combination logic
# --------------------------------------------------------------------------- #

def test_combine_empty_families_yields_single_empty_plan():
    assert batch.combine_field_set_families([]) == [frozenset()]


def test_combine_unions_and_sorts_results():
    families = [[["b"], ["a", "c"]], [["d"]]]
    result = batch.combine_field_set_families(families)
    assert result == [frozenset({"b", "d"}), frozenset({"a", "c", "d"})]


def test_combine_dedupes_and_prunes_strict_supersets():
    # {a} subsumes {a,b} inside the first family; {a,b} from the second fold
    # is pruned by nothing, but duplicate unions collapse.
    families = [[["a", "b"], ["a"]], [["a", "b"], ["c"]]]
    result = batch.combine_field_set_families(families)
    assert result == [frozenset({"a", "b"}), frozenset({"a", "c"})]


def test_combine_with_uncoverable_family_returns_no_plans():
    assert batch.combine_field_set_families([[["a"]], []]) == []


def test_combine_cap_raises_instead_of_truncating():
    families = [[["a"], ["b"]], [["c"], ["d"]]]
    try:
        batch.combine_field_set_families(families, max_combinations=3)
        raised = False
    except batch.BatchPlanSearchError as exc:
        raised = True
        assert "exceeds 3 combinations" in str(exc)
    assert raised
    # One more slot of headroom completes the same fold exactly.
    result = batch.combine_field_set_families(families, max_combinations=6)
    assert result == [
        frozenset({"a", "c"}),
        frozenset({"a", "d"}),
        frozenset({"b", "c"}),
        frozenset({"b", "d"}),
    ]


def test_env_variable_configures_default_cap():
    key = "MAX_BATCH_PLAN_COMBINATIONS"
    original = os.environ.get(key)
    try:
        os.environ[key] = "7"
        assert batch._configured_max_combinations() == 7
        os.environ[key] = "not-a-number"
        assert batch._configured_max_combinations() == 100_000
        os.environ[key] = "-5"
        assert batch._configured_max_combinations() == 100_000
    finally:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


# --------------------------------------------------------------------------- #
# Endpoint behaviour
# --------------------------------------------------------------------------- #

def test_plans_combine_unknown_cases_only():
    pid = create_policy()
    r = post_plans(pid, {
        "cases": [
            {"case_id": "c-vip", "facts": {"age": 30, "country": "US"}},
            {"case_id": "c-decided", "facts": {"age": 25, "country": "NL"}},
            {"case_id": "c-top", "facts": {}},
            {"case_id": "c-none", "facts": {
                "age": 30, "country": "US", "vip": False, "flag": False}},
        ]
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy_id"] == pid
    assert body["case_count"] == 4
    assert body["unknown_count"] == 2

    # Case summaries are sorted by case_id.
    assert [c["case_id"] for c in body["cases"]] == [
        "c-decided", "c-none", "c-top", "c-vip"]
    statuses = {c["case_id"]: c["status"] for c in body["cases"]}
    assert statuses == {
        "c-decided": "decided",
        "c-none": "no_match",
        "c-top": "unknown",
        "c-vip": "unknown",
    }
    by_id = {c["case_id"]: c for c in body["cases"]}
    assert by_id["c-decided"]["conclusion"] == "allow"
    assert by_id["c-top"]["minimal_missing_field_sets"] == [["age"]]
    assert by_id["c-top"]["missing_fields"] == ["age"]
    assert by_id["c-vip"]["minimal_missing_field_sets"] == [["flag"], ["vip"]]

    # Plans are sorted by field count, then field names; covers only lists
    # unknown cases, keyed by case_id.
    assert body["count"] == 2
    assert body["plans"] == [
        {"fields": ["age", "flag"],
         "covers": {"c-top": [["age"]], "c-vip": [["flag"]]}},
        {"fields": ["age", "vip"],
         "covers": {"c-top": [["age"]], "c-vip": [["vip"]]}},
    ]


def test_no_unknown_cases_returns_single_empty_plan():
    pid = create_policy()
    r = post_plans(pid, {
        "cases": [
            {"case_id": "d2", "facts": {"age": 10, "country": "US"}},
            {"case_id": "d1", "facts": {"age": 25, "country": "NL"}},
        ]
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unknown_count"] == 0
    assert body["count"] == 1
    assert body["plans"] == [{"fields": [], "covers": {}}]
    assert [c["case_id"] for c in body["cases"]] == ["d1", "d2"]
    assert [c["conclusion"] for c in body["cases"]] == ["allow", "deny"]


def test_superset_plans_are_pruned():
    pid = create_policy({"rules": [
        {"id": "r", "priority": 1, "conclusion": "ok",
         "expr": {"any": [{"field": "x", "op": "eq", "value": 1},
                          {"field": "y", "op": "eq", "value": 1}]}},
    ]})
    r = post_plans(pid, {
        "cases": [
            {"case_id": "k1", "facts": {"x": 0}},  # sets: [["y"]]
            {"case_id": "k2", "facts": {}},        # sets: [["x"], ["y"]]
        ]
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # {y} covers both cases; {x, y} is a strict superset and must be pruned.
    assert body["plans"] == [
        {"fields": ["y"], "covers": {"k1": [["y"]], "k2": [["y"]]}}]


def test_facts_default_to_empty_object():
    pid = create_policy()
    r = post_plans(pid, {"cases": [{"case_id": "k"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cases"][0]["status"] == "unknown"
    assert body["plans"] == [
        {"fields": ["age"], "covers": {"k": [["age"]]}}]


def test_combination_cap_returns_422_without_truncating():
    pid = create_policy({"rules": [
        {"id": "r", "priority": 1, "conclusion": "ok",
         "expr": {"any": [
             {"field": "a", "op": "eq", "value": 1},
             {"field": "b", "op": "eq", "value": 1},
             {"field": "c", "op": "eq", "value": 1},
             {"field": "d", "op": "eq", "value": 1}]}},
    ]})
    body = {"cases": [{"case_id": "k1", "facts": {}},
                      {"case_id": "k2", "facts": {}}]}

    original = batch.MAX_BATCH_PLAN_COMBINATIONS
    batch.MAX_BATCH_PLAN_COMBINATIONS = 10
    try:
        r = post_plans(pid, body)
        assert r.status_code == 422, r.text
        out = r.json()
        assert out["detail"] == "Batch data plan search failed"
        assert isinstance(out["errors"], list) and out["errors"]
    finally:
        batch.MAX_BATCH_PLAN_COMBINATIONS = original

    # With the default cap the same request completes: each case is decided
    # by any single field, so the minimal global plans are the singletons.
    r = post_plans(pid, body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["count"] == 4
    assert [p["fields"] for p in out["plans"]] == [["a"], ["b"], ["c"], ["d"]]
    assert out["plans"][0]["covers"] == {"k1": [["a"]], "k2": [["a"]]}


def test_validation_errors_return_422():
    pid = create_policy()
    bad_bodies = [
        {},
        [1, 2],
        {"cases": []},
        {"cases": "nope"},
        {"cases": [1]},
        {"cases": [{"facts": {}}]},
        {"cases": [{"case_id": "", "facts": {}}]},
        {"cases": [{"case_id": "   ", "facts": {}}]},
        {"cases": [{"case_id": 7, "facts": {}}]},
        {"cases": [{"case_id": "a", "facts": []}]},
        {"cases": [{"case_id": "a", "facts": "x"}]},
        {"cases": [{"case_id": "a"}, {"case_id": "a"}]},
        {"cases": [{"case_id": "a"}, {"case_id": "b"}, {"case_id": "a"}]},
    ]
    for body in bad_bodies:
        r = post_plans(pid, body)
        assert r.status_code == 422, body
        out = r.json()
        assert out["detail"] == "Policy structure validation failed"
        assert isinstance(out["errors"], list) and out["errors"]


def test_duplicate_case_id_reports_every_duplicate():
    pid = create_policy()
    r = post_plans(pid, {"cases": [{"case_id": "a"}, {"case_id": "a"}]})
    assert r.status_code == 422
    assert any("duplicate case id" in e for e in r.json()["errors"])


def test_malformed_json_is_400():
    pid = create_policy()
    r = client.post(
        f"/api/v1/policies/{pid}/batch-data-plans",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_unknown_policy_returns_404():
    r = post_plans("nope", {"cases": [{"case_id": "a", "facts": {}}]})
    assert r.status_code == 404


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        setup_function()
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failures += 1
            import traceback
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
