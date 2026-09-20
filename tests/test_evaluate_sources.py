"""Priority fact-source merging tests: merge semantics, validation, API.

Run with: python tests/test_evaluate_sources.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.sources import merge_fact_sources  # noqa: E402
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


# --------------------------------------------------------------------------- #
# merge_fact_sources unit tests
# --------------------------------------------------------------------------- #

def test_merge_highest_priority_wins_per_field():
    result = merge_fact_sources([
        {"name": "low", "priority": 1,
         "facts": {"age": 10, "country": "US"}},
        {"name": "high", "priority": 10,
         "facts": {"age": 25}},
    ])
    assert result["facts"] == {"age": 25, "country": "US"}
    assert result["unresolved_fields"] == []
    age = result["fields"]["age"]
    assert age["status"] == "resolved"
    assert age["adopted_sources"] == ["high"]
    assert age["discarded_sources"] == [
        {"source": "low", "priority": 1, "value": 10,
         "reason": "lower_priority"}
    ]
    assert result["fields"]["country"]["adopted_sources"] == ["low"]


def test_merge_equal_highest_priority_values_agree():
    result = merge_fact_sources([
        {"name": "a", "priority": 5, "facts": {"age": 25}},
        {"name": "b", "priority": 5, "facts": {"age": 25}},
        {"name": "c", "priority": 1, "facts": {"age": 99}},
    ])
    assert result["facts"] == {"age": 25}
    report = result["fields"]["age"]
    assert report["adopted_sources"] == ["a", "b"]
    assert report["discarded_sources"] == [
        {"source": "c", "priority": 1, "value": 99,
         "reason": "lower_priority"}
    ]


def test_merge_conflict_at_top_priority_unresolved_no_backfill():
    result = merge_fact_sources([
        {"name": "low", "priority": 1, "facts": {"age": 25}},
        {"name": "x", "priority": 9, "facts": {"age": 30}},
        {"name": "y", "priority": 9, "facts": {"age": 40}},
    ])
    assert result["facts"] == {}
    assert result["unresolved_fields"] == ["age"]
    report = result["fields"]["age"]
    assert report["status"] == "unresolved"
    assert report["reason"] == "conflicting_values"
    assert report["conflicting_values"] == [30, 40]
    assert report["adopted_sources"] == []
    discarded = {(d["source"], d["reason"]) for d in report["discarded_sources"]}
    assert discarded == {
        ("x", "conflicting_values"),
        ("y", "conflicting_values"),
        ("low", "lower_priority"),
    }


def test_merge_null_at_top_priority_unresolved_no_backfill():
    result = merge_fact_sources([
        {"name": "low", "priority": 1, "facts": {"age": 25}},
        {"name": "flaky", "priority": 9, "facts": {"age": None}},
    ])
    assert result["facts"] == {}
    assert result["unresolved_fields"] == ["age"]
    report = result["fields"]["age"]
    assert report["reason"] == "null_value"
    assert report["conflicting_values"] == [None]
    reasons = {d["source"]: d["reason"] for d in report["discarded_sources"]}
    assert reasons == {"flaky": "null_value", "low": "lower_priority"}


def test_merge_null_beside_value_at_top_priority_unresolved():
    result = merge_fact_sources([
        {"name": "a", "priority": 5, "facts": {"age": None}},
        {"name": "b", "priority": 5, "facts": {"age": 25}},
    ])
    assert result["unresolved_fields"] == ["age"]
    assert result["fields"]["age"]["reason"] == "conflicting_values"
    reasons = {d["source"]: d["reason"] for d
               in result["fields"]["age"]["discarded_sources"]}
    assert reasons == {"a": "null_value", "b": "conflicting_values"}


def test_merge_strict_equality_true_is_not_one():
    result = merge_fact_sources([
        {"name": "a", "priority": 5, "facts": {"flag": True}},
        {"name": "b", "priority": 5, "facts": {"flag": 1}},
    ])
    assert result["unresolved_fields"] == ["flag"]
    assert result["fields"]["flag"]["reason"] == "conflicting_values"


def test_merge_equal_values_are_strictly_compared():
    result = merge_fact_sources([
        {"name": "a", "priority": 5,
         "facts": {"tags": [1, 2], "meta": {"k": 1}}},
        {"name": "b", "priority": 5,
         "facts": {"tags": [1, 2], "meta": {"k": 1}}},
    ])
    assert result["facts"] == {"tags": [1, 2], "meta": {"k": 1}}
    assert result["unresolved_fields"] == []


def test_merge_result_is_order_independent():
    sources = [
        {"name": "low", "priority": 1, "facts": {"a": 1, "b": 10}},
        {"name": "mid", "priority": 5, "facts": {"a": 2}},
        {"name": "hi", "priority": 9, "facts": {"a": 3, "c": None}},
    ]
    reversed_order = [
        {"name": "hi", "priority": 9, "facts": {"c": None, "a": 3}},
        {"name": "mid", "priority": 5, "facts": {"a": 2}},
        {"name": "low", "priority": 1, "facts": {"b": 10, "a": 1}},
    ]
    assert merge_fact_sources(sources) == merge_fact_sources(reversed_order)

    # Conflict case must also be stable under reordering.
    conflict_a = [
        {"name": "p", "priority": 7, "facts": {"x": 1}},
        {"name": "q", "priority": 7, "facts": {"x": 2}},
        {"name": "r", "priority": 3, "facts": {"x": 9}},
    ]
    conflict_b = [
        {"name": "r", "priority": 3, "facts": {"x": 9}},
        {"name": "q", "priority": 7, "facts": {"x": 2}},
        {"name": "p", "priority": 7, "facts": {"x": 1}},
    ]
    assert merge_fact_sources(conflict_a) == merge_fact_sources(conflict_b)


def test_merge_empty_facts_objects_resolve_nothing():
    result = merge_fact_sources([
        {"name": "a", "priority": 1, "facts": {}},
        {"name": "b", "priority": 2, "facts": {}},
    ])
    assert result["facts"] == {}
    assert result["fields"] == {}
    assert result["unresolved_fields"] == []


# --------------------------------------------------------------------------- #
# API tests
# --------------------------------------------------------------------------- #

def test_evaluate_sources_resolved_verdict():
    pid = create_policy()
    body = {
        "sources": [
            {"name": "census", "priority": 10,
             "facts": {"age": 25, "country": "NL"}},
            {"name": "form", "priority": 1,
             "facts": {"age": 10, "country": "US", "vip": True}},
        ]
    }
    r = client.post(f"/api/v1/policies/{pid}/evaluate-sources", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["merged_facts"] == {"age": 25, "country": "NL", "vip": True}
    assert data["unresolved_fields"] == []
    field = data["fields"]["age"]
    assert field["status"] == "resolved"
    assert field["adopted_sources"] == ["census"]
    assert field["discarded_sources"][0]["source"] == "form"
    assert field["discarded_sources"][0]["reason"] == "lower_priority"
    verdict = data["verdict"]
    assert verdict["status"] == "decided"
    assert verdict["conclusion"] == "allow"
    assert verdict["decided_priority"] == 100


def test_evaluate_sources_unresolved_field_makes_verdict_unknown():
    pid = create_policy()
    body = {
        "sources": [
            {"name": "a", "priority": 10, "facts": {"age": 25}},
            {"name": "b", "priority": 10, "facts": {"age": 30}},
            # Low-priority country must not resolve anything either.
            {"name": "c", "priority": 1,
             "facts": {"age": 25, "country": "NL"}},
        ]
    }
    r = client.post(f"/api/v1/policies/{pid}/evaluate-sources", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    # age stays unresolved (no backfill from c); c's country has no competing
    # provider, so it is still adopted.
    assert data["merged_facts"] == {"country": "NL"}
    assert data["unresolved_fields"] == ["age"]
    assert data["fields"]["age"]["reason"] == "conflicting_values"
    assert data["verdict"]["status"] == "unknown"
    assert "age" in data["verdict"]["missing_fields"]


def test_evaluate_sources_null_top_value_blocks_lower_backfill():
    pid = create_policy()
    body = {
        "sources": [
            {"name": "trusted", "priority": 9, "facts": {"country": None}},
            {"name": "fallback", "priority": 1,
             "facts": {"country": "NL", "age": 25}},
        ]
    }
    r = client.post(f"/api/v1/policies/{pid}/evaluate-sources", json=body)
    data = r.json()
    assert data["unresolved_fields"] == ["country"]
    assert "country" not in data["merged_facts"]
    assert data["merged_facts"] == {"age": 25}
    assert data["verdict"]["status"] == "unknown"


def test_evaluate_sources_order_does_not_change_response():
    pid = create_policy()
    body_a = {
        "sources": [
            {"name": "z", "priority": 1, "facts": {"age": 10}},
            {"name": "m", "priority": 5, "facts": {"age": 25}},
            {"name": "a", "priority": 5, "facts": {"age": 25}},
            {"name": "k", "priority": 9, "facts": {"country": "NL"}},
        ]
    }
    body_b = {"sources": list(reversed(body_a["sources"]))}
    r_a = client.post(
        f"/api/v1/policies/{pid}/evaluate-sources", json=body_a
    ).json()
    r_b = client.post(
        f"/api/v1/policies/{pid}/evaluate-sources", json=body_b
    ).json()
    assert r_a == r_b
    assert r_a["merged_facts"] == {"age": 25, "country": "NL"}


def test_evaluate_sources_404_for_unknown_policy():
    r = client.post("/api/v1/policies/nope/evaluate-sources",
                    json={"sources": [{"name": "s", "priority": 1,
                                       "facts": {}}]})
    assert r.status_code == 404


def test_evaluate_sources_duplicate_names_422():
    pid = create_policy()
    r = client.post(f"/api/v1/policies/{pid}/evaluate-sources", json={
        "sources": [
            {"name": "dup", "priority": 1, "facts": {}},
            {"name": "dup", "priority": 2, "facts": {}},
        ]
    })
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "Fact source validation failed"
    assert any("duplicate source name" in e for e in body["errors"])


def test_evaluate_sources_empty_list_422():
    pid = create_policy()
    r = client.post(
        f"/api/v1/policies/{pid}/evaluate-sources", json={"sources": []}
    )
    assert r.status_code == 422
    assert "non-empty array" in r.json()["errors"][0]


def test_evaluate_sources_missing_sources_422():
    pid = create_policy()
    r = client.post(
        f"/api/v1/policies/{pid}/evaluate-sources", json={}
    )
    assert r.status_code == 422


def test_evaluate_sources_priority_must_be_integer_422():
    pid = create_policy()
    r = client.post(f"/api/v1/policies/{pid}/evaluate-sources", json={
        "sources": [{"name": "s", "priority": "5", "facts": {}}]
    })
    assert r.status_code == 422
    assert any("priority" in e for e in r.json()["errors"])

    # Booleans are not integers.
    r = client.post(f"/api/v1/policies/{pid}/evaluate-sources", json={
        "sources": [{"name": "s", "priority": True, "facts": {}}]
    })
    assert r.status_code == 422


def test_evaluate_sources_bad_name_and_facts_422():
    pid = create_policy()
    r = client.post(f"/api/v1/policies/{pid}/evaluate-sources", json={
        "sources": [
            {"name": "", "priority": 1, "facts": {}},
            {"name": "ok", "priority": 1, "facts": ["not", "object"]},
            "not-an-object",
        ]
    })
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any("name" in e for e in errors)
    assert any("facts" in e for e in errors)
    assert any("must be an object" in e for e in errors)


def test_evaluate_sources_unknown_keys_422():
    pid = create_policy()
    r = client.post(f"/api/v1/policies/{pid}/evaluate-sources", json={
        "sources": [{"name": "s", "priority": 1, "facts": {},
                     "extra": 1}]
    })
    assert r.status_code == 422
    assert any("unknown keys" in e for e in r.json()["errors"])


def test_evaluate_sources_invalid_json_400():
    pid = create_policy()
    r = client.post(
        f"/api/v1/policies/{pid}/evaluate-sources",
        data="not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


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
