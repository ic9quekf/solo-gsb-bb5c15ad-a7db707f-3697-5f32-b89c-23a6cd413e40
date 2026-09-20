"""Counterfactual endpoint tests: search semantics, minimality, validation.

Run with: python tests/test_counterfactual.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

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
            "id": "manual-review",
            "priority": 10,
            "conclusion": "review",
            "expr": {"field": "vip", "op": "eq", "value": True},
        },
    ]
}


def create_policy(policy=None):
    r = client.post("/api/v1/policies", json=policy or POLICY)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def post_cf(pid, **overrides):
    body = {
        "facts": {},
        "target_conclusion": "allow",
        "candidates": {},
        "max_changes": 1,
    }
    body.update(overrides)
    return client.post(f"/api/v1/policies/{pid}/counterfactuals", json=body)


def test_already_at_target_returns_empty_changes():
    pid = create_policy()
    r = post_cf(
        pid,
        facts={"age": 30, "country": "NL"},
        candidates={"age": [20]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["original_status"] == "decided"
    assert body["original_conclusion"] == "allow"
    assert body["count"] == 1
    item = body["counterfactuals"][0]
    assert item["changes"] == {}
    assert item["verdict"]["status"] == "decided"
    assert item["verdict"]["conclusion"] == "allow"
    assert item["level_trace"]["state"] == "fired"


def test_single_field_edits_unknown_to_target():
    pid = create_policy()
    # Empty facts are unknown on the top level; age alone decides it, but only
    # age>=18 still leaves country unknown -> that candidate stays unknown and
    # must be excluded. Combine with country candidates to reach "allow".
    r = post_cf(
        pid,
        facts={},
        max_changes=2,
        candidates={"age": [10, 30], "country": ["US", "NL"]},
    )
    assert r.status_code == 200, r.text
    items = r.json()["counterfactuals"]
    # The only minimal hit is age>=18 plus country NL.
    assert items == [
        {
            "changes": {"age": 30, "country": "NL"},
            "verdict": items[0]["verdict"],
            "level_trace": items[0]["level_trace"],
        }
    ]
    verdict = items[0]["verdict"]
    assert verdict["status"] == "decided"
    assert verdict["conclusion"] == "allow"
    assert verdict["decided_priority"] == 100
    assert items[0]["level_trace"] == verdict["level_trace"]
    assert items[0]["level_trace"]["state"] == "fired"


def test_no_match_outcome_is_excluded():
    pid = create_policy()
    # age=30, country=US skips level 100; vip candidates that are not true
    # cannot produce "allow" at all (nothing ever fires allow without NL).
    r = post_cf(
        pid,
        facts={"age": 30, "country": "US", "vip": False},
        target_conclusion="allow",
        max_changes=1,
        candidates={"vip": [True]},
    )
    assert r.status_code == 200
    assert r.json()["counterfactuals"] == []


def test_conflict_candidates_are_excluded():
    pid = create_policy({
        "rules": [
            {"id": "a", "priority": 1, "conclusion": "yes",
             "expr": {"field": "v", "op": "ge", "value": 10}},
            {"id": "b", "priority": 1, "conclusion": "no",
             "expr": {"field": "v", "op": "le", "value": 20}},
        ]
    })
    # v=15 fires both rules -> conflict; v=25 uniquely fires "yes";
    # v=5 uniquely fires "no".
    r = post_cf(
        pid,
        facts={"v": 15},
        target_conclusion="yes",
        max_changes=1,
        candidates={"v": [25, 15, 5]},
    )
    assert r.status_code == 200
    assert r.json()["original_status"] == "conflict"
    changes = [i["changes"] for i in r.json()["counterfactuals"]]
    # v=15 stays a conflict, v=5 yields "no" -> both excluded.
    assert changes == [{"v": 25}]

    r = post_cf(
        pid,
        facts={"v": 15},
        target_conclusion="no",
        max_changes=1,
        candidates={"v": [25, 15, 5]},
    )
    changes = [i["changes"] for i in r.json()["counterfactuals"]]
    assert changes == [{"v": 5}]


def test_unknown_candidates_are_excluded():
    pid = create_policy()
    # From unknown facts, changing age to a concrete value still leaves
    # country unknown at level 100 -> verdict remains unknown.
    r = post_cf(
        pid,
        facts={},
        max_changes=1,
        candidates={"age": [30]},
    )
    assert r.status_code == 200
    assert r.json()["counterfactuals"] == []


def test_max_changes_limits_enumeration():
    pid = create_policy()
    r = post_cf(
        pid,
        facts={},
        max_changes=1,
        candidates={"age": [30], "country": ["NL"]},
    )
    assert r.status_code == 200
    assert r.json()["counterfactuals"] == []

    r = post_cf(
        pid,
        facts={},
        max_changes=2,
        candidates={"age": [30], "country": ["NL"]},
    )
    assert r.status_code == 200
    assert [i["changes"] for i in r.json()["counterfactuals"]] == [
        {"age": 30, "country": "NL"}
    ]


def test_minimality_supersets_are_pruned():
    pid = create_policy()
    # With country already NL and level 10 decided allow by age, changing vip
    # as well would be a non-minimal superset and must not appear.
    r = post_cf(
        pid,
        facts={"country": "NL"},
        max_changes=2,
        candidates={"age": [30], "vip": [True, False]},
    )
    assert r.status_code == 200, r.text
    changes = [i["changes"] for i in r.json()["counterfactuals"]]
    assert changes == [{"age": 30}]


def test_all_value_options_on_minimal_field_set_are_reported():
    pid = create_policy({
        "rules": [
            {"id": "r", "priority": 1, "conclusion": "ok",
             "expr": {"field": "v", "op": "gt", "value": 0}},
        ]
    })
    r = post_cf(
        pid,
        facts={"v": -5},
        target_conclusion="ok",
        max_changes=1,
        candidates={"v": [2, 1, 10]},
    )
    assert r.status_code == 200
    changes = [i["changes"] for i in r.json()["counterfactuals"]]
    # Canonical JSON ordering of numeric values: 1, 10, 2.
    assert changes == [{"v": 1}, {"v": 10}, {"v": 2}]


def test_results_stable_sorted_by_size_fields_values():
    pid = create_policy({
        "rules": [
            {"id": "r", "priority": 1, "conclusion": "ok",
             "expr": {"any": [
                 {"field": "a", "op": "eq", "value": 1},
                 {"field": "b", "op": "eq", "value": 1},
             ]}},
        ]
    })
    r = post_cf(
        pid,
        facts={"a": 0, "b": 0},
        target_conclusion="ok",
        max_changes=2,
        candidates={"a": [1], "b": [1, 10]},
    )
    assert r.status_code == 200, r.text
    changes = [i["changes"] for i in r.json()["counterfactuals"]]
    # Size 1 sets first (field name order a before b); for b, 10 makes
    # b==1 false and is excluded. The size-2 set is pruned as a superset.
    assert changes == [{"a": 1}, {"b": 1}]


def test_duplicate_candidate_values_are_deduped():
    pid = create_policy({
        "rules": [
            {"id": "r", "priority": 1, "conclusion": "ok",
             "expr": {"field": "v", "op": "eq", "value": 1}},
        ]
    })
    # Identical encodings collapse; JSON-typed 1 (integer) vs 1.0 (number)
    # stay distinct entries under canonical ordering.
    r = post_cf(
        pid,
        facts={"v": 0},
        target_conclusion="ok",
        max_changes=1,
        candidates={"v": [1, 1, 1.0]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    changes = [i["changes"] for i in body["counterfactuals"]]
    assert changes == [{"v": 1}, {"v": 1.0}]


def test_validation_errors_return_422():
    pid = create_policy()
    bad_bodies = [
        {},
        {"facts": {}, "target_conclusion": "allow", "max_changes": 1},
        {"facts": {}, "target_conclusion": "allow",
         "candidates": {}, "max_changes": 1},
        {"facts": {}, "target_conclusion": "allow",
         "candidates": {"a": []}, "max_changes": 1},
        {"facts": {}, "target_conclusion": "allow",
         "candidates": {"a": [1]}, "max_changes": 0},
        {"facts": {}, "target_conclusion": "allow",
         "candidates": {"a": [1]}, "max_changes": -1},
        {"facts": {}, "target_conclusion": "allow",
         "candidates": {"a": [1]}, "max_changes": "2"},
        {"facts": {}, "target_conclusion": "allow",
         "candidates": {"a": [1]}, "max_changes": True},
        {"facts": {}, "target_conclusion": "",
         "candidates": {"a": [1]}, "max_changes": 1},
        {"facts": [], "target_conclusion": "allow",
         "candidates": {"a": [1]}, "max_changes": 1},
    ]
    for body in bad_bodies:
        r = client.post(f"/api/v1/policies/{pid}/counterfactuals", json=body)
        assert r.status_code == 422, body
        out = r.json()
        assert out["detail"] == "Policy structure validation failed"
        assert isinstance(out["errors"], list) and out["errors"]


def test_malformed_json_is_400():
    pid = create_policy()
    r = client.post(
        f"/api/v1/policies/{pid}/counterfactuals",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_unknown_policy_returns_404():
    r = post_cf("nope", candidates={"a": [1]})
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
