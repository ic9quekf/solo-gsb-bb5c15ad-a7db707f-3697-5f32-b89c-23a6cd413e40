"""End-to-end tests for POST /api/v1/policies/{id}/evaluate-sources.

Run with: python tests/test_evaluate_sources.py
Requires: fastapi, httpx (see requirements-dev.txt).
"""
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.sources import merge_sources  # noqa: E402
from app.store import store  # noqa: E402


client = TestClient(app)


def setup_function():
    store.clear()


POLICY = {
    "rules": [
        {
            "id": "approve",
            "priority": 10,
            "conclusion": "approve",
            "expr": {
                "all": [
                    {"field": "income", "op": "ge", "value": 50000},
                    {"field": "debt_ratio", "op": "lt", "value": 0.4},
                ]
            },
        },
        {
            "id": "reject",
            "priority": 10,
            "conclusion": "reject",
            "expr": {"field": "debt_ratio", "op": "ge", "value": 0.6},
        },
    ]
}


def create_policy():
    r = client.post("/api/v1/policies", json=POLICY)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def evaluate(pid, sources):
    return client.post(
        f"/api/v1/policies/{pid}/evaluate-sources", json={"sources": sources}
    )


def test_agreement_adopts_and_decides():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "crm", "priority": 100,
         "facts": {"income": 80000, "debt_ratio": 0.2}},
        {"name": "application", "priority": 100,
         "facts": {"income": 80000, "debt_ratio": 0.2}},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy_id"] == pid
    assert body["source_count"] == 2
    assert body["merged_facts"] == {"income": 80000, "debt_ratio": 0.2}
    assert body["unresolved_fields"] == []

    fields = {f["field"]: f for f in body["fields"]}
    income = fields["income"]
    assert income["status"] == "adopted"
    assert income["value"] == 80000
    assert income["priority"] == 100
    assert income["adopted_from"] == ["application", "crm"]
    assert income["discarded"] == []

    verdict = body["verdict"]
    assert verdict["status"] == "decided"
    assert verdict["conclusion"] == "approve"
    assert verdict["decided_priority"] == 10
    # Full engine verdict, not a summary.
    assert verdict["true_rule_ids"] == ["approve"]
    assert verdict["level_trace"]["state"] == "fired"
    assert isinstance(verdict["levels_trace"], list)


def test_conflict_unresolved_and_no_lower_priority_backfill():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "crm", "priority": 100,
         "facts": {"income": 80000, "debt_ratio": 0.5}},
        {"name": "application", "priority": 100,
         "facts": {"income": 80000, "debt_ratio": 0.3}},
        {"name": "legacy", "priority": 10, "facts": {"debt_ratio": 0.2}},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    # The lower-priority source must not backfill the contested field.
    assert body["merged_facts"] == {"income": 80000}
    assert body["unresolved_fields"] == ["debt_ratio"]

    fields = {f["field"]: f for f in body["fields"]}
    debt = fields["debt_ratio"]
    assert debt["status"] == "unresolved"
    assert debt["reason"] == "conflicting_values"
    assert debt["priority"] == 100
    assert debt["conflicts"] == [
        {"source": "application", "value": 0.3},
        {"source": "crm", "value": 0.5},
    ]
    assert debt["discarded"] == [
        {"source": "legacy", "priority": 10, "value": 0.2,
         "reason": "lower_priority"}
    ]

    verdict = body["verdict"]
    assert verdict["status"] == "unknown"
    assert verdict["missing_fields"] == ["debt_ratio"]
    assert verdict["minimal_missing_field_sets"] == [["debt_ratio"]]


def test_null_value_unresolved_even_when_unanimous():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "crm", "priority": 100,
         "facts": {"income": 80000, "debt_ratio": None}},
        {"name": "application", "priority": 100,
         "facts": {"income": 80000, "debt_ratio": None}},
        {"name": "legacy", "priority": 10, "facts": {"debt_ratio": 0.2}},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged_facts"] == {"income": 80000}

    fields = {f["field"]: f for f in body["fields"]}
    debt = fields["debt_ratio"]
    assert debt["status"] == "unresolved"
    assert debt["reason"] == "null_value"
    assert debt["conflicts"] == [
        {"source": "application", "value": None},
        {"source": "crm", "value": None},
    ]
    assert body["verdict"]["status"] == "unknown"


def test_single_null_in_top_tier_unresolved():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "crm", "priority": 100, "facts": {"debt_ratio": None}},
        {"name": "application", "priority": 100, "facts": {"debt_ratio": 0.2}},
    ])
    body = r.json()
    fields = {f["field"]: f for f in body["fields"]}
    # null vs a concrete value is a conflict, not a unanimous null.
    assert fields["debt_ratio"]["status"] == "unresolved"
    assert fields["debt_ratio"]["reason"] == "conflicting_values"
    assert body["merged_facts"] == {}


def test_lower_priority_fields_adopted_when_uncontested():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "crm", "priority": 100, "facts": {"income": 80000}},
        {"name": "application", "priority": 50, "facts": {"debt_ratio": 0.2}},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    # Each field's top tier is computed per field, not globally.
    assert body["merged_facts"] == {"income": 80000, "debt_ratio": 0.2}
    fields = {f["field"]: f for f in body["fields"]}
    assert fields["debt_ratio"]["adopted_from"] == ["application"]
    assert fields["debt_ratio"]["priority"] == 50
    assert body["verdict"]["status"] == "decided"
    assert body["verdict"]["conclusion"] == "approve"


def test_lower_priority_discarded_when_top_tier_agrees():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "crm", "priority": 100, "facts": {"income": 80000}},
        {"name": "application", "priority": 100, "facts": {"income": 80000}},
        {"name": "legacy", "priority": 1, "facts": {"income": 70000}},
    ])
    body = r.json()
    fields = {f["field"]: f for f in body["fields"]}
    income = fields["income"]
    assert income["status"] == "adopted"
    assert income["value"] == 80000
    assert income["discarded"] == [
        {"source": "legacy", "priority": 1, "value": 70000,
         "reason": "lower_priority"}
    ]


def test_strict_equality_typing():
    pid = create_policy()
    # true and 1 are strictly different: the field stays unresolved.
    r = evaluate(pid, [
        {"name": "a", "priority": 5, "facts": {"income": 80000}},
        {"name": "b", "priority": 5, "facts": {"income": True}},
    ])
    fields = {f["field"]: f for f in r.json()["fields"]}
    assert fields["income"]["status"] == "unresolved"
    assert fields["income"]["reason"] == "conflicting_values"

    # 1 and 1.0 are the same JSON number: adopted.
    r = evaluate(pid, [
        {"name": "a", "priority": 5, "facts": {"income": 80000}},
        {"name": "b", "priority": 5, "facts": {"income": 80000.0}},
    ])
    fields = {f["field"]: f for f in r.json()["fields"]}
    assert fields["income"]["status"] == "adopted"
    assert fields["income"]["value"] == 80000


def test_source_order_does_not_change_result():
    pid = create_policy()
    sources = [
        {"name": "crm", "priority": 100,
         "facts": {"income": 80000, "debt_ratio": 0.5}},
        {"name": "application", "priority": 100,
         "facts": {"income": 80000, "debt_ratio": 0.3}},
        {"name": "legacy", "priority": 10,
         "facts": {"debt_ratio": 0.2, "country": "NL"}},
        {"name": "webform", "priority": 100, "facts": {"country": "NL"}},
    ]
    baseline = evaluate(pid, sources).json()
    for permuted in itertools.permutations(sources):
        body = evaluate(pid, list(permuted)).json()
        assert body == baseline


def test_merge_sources_is_order_independent_unit():
    sources = [
        {"name": "a", "priority": 3, "facts": {"x": 1, "y": None}},
        {"name": "b", "priority": 7, "facts": {"x": 2, "z": "hi"}},
        {"name": "c", "priority": 7, "facts": {"x": 2}},
    ]
    merged, reports = merge_sources(sources)
    for permuted in itertools.permutations(sources):
        assert merge_sources(list(permuted)) == (merged, reports)
    assert merged == {"x": 2, "z": "hi"}
    assert [r["field"] for r in reports] == ["x", "y", "z"]


def test_no_match_verdict_still_complete():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "crm", "priority": 100,
         "facts": {"income": 30000, "debt_ratio": 0.5}},
    ])
    body = r.json()
    verdict = body["verdict"]
    assert verdict["status"] == "no_match"
    assert verdict["conclusion"] is None
    assert "levels_trace" in verdict and "level_trace" in verdict


def test_facts_default_to_empty_object():
    pid = create_policy()
    r = evaluate(pid, [{"name": "crm", "priority": 100}])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merged_facts"] == {}
    assert body["fields"] == []
    assert body["verdict"]["status"] == "unknown"


def test_unknown_policy_404():
    r = client.post(
        "/api/v1/policies/nope/evaluate-sources",
        json={"sources": [{"name": "a", "priority": 1, "facts": {}}]},
    )
    assert r.status_code == 404


def test_empty_or_missing_sources_422():
    pid = create_policy()
    for payload in ({}, {"sources": []}, {"sources": "nope"},
                    {"sources": [1]}, {"sources": None}):
        r = client.post(
            f"/api/v1/policies/{pid}/evaluate-sources", json=payload
        )
        assert r.status_code == 422, (payload, r.text)
        body = r.json()
        assert body["detail"] == "Policy structure validation failed"
        assert isinstance(body["errors"], list) and body["errors"]


def test_duplicate_source_name_422():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "crm", "priority": 100, "facts": {}},
        {"name": "crm", "priority": 50, "facts": {}},
    ])
    assert r.status_code == 422
    assert any("duplicate source name" in e for e in r.json()["errors"])


def test_malformed_sources_422():
    pid = create_policy()
    r = evaluate(pid, [
        {"name": "", "priority": 100, "facts": {}},
        {"name": "b", "priority": True, "facts": {}},
        {"name": "c", "priority": 1.5, "facts": {}},
        {"name": "d", "priority": 10**10, "facts": {}},
        {"name": "e", "priority": 1, "facts": [1, 2]},
        {"name": "f", "priority": 1, "facts": {}, "extra": 1},
        {"priority": 1, "facts": {}},
    ])
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any("sources[0].name" in e for e in errors)
    assert any("sources[1].priority" in e for e in errors)
    assert any("sources[2].priority" in e for e in errors)
    assert any("sources[3].priority" in e for e in errors)
    assert any("sources[4].facts" in e for e in errors)
    assert any("sources[5]: unknown keys" in e for e in errors)
    assert any("sources[6].name" in e for e in errors)


def test_invalid_json_body_400():
    pid = create_policy()
    r = client.post(
        f"/api/v1/policies/{pid}/evaluate-sources",
        content=b"{not json",
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
