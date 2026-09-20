"""Replacement preview tests: signatures, transitions, validation, no writes.

Run with: python tests/test_replacement_preview.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.counterfactual import canonical_json  # noqa: E402
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
            "expr": {"field": "vip", "op": "eq", "value": True},
        },
    ]
}

# Candidate: adult threshold raised to 21, review driven by `flag` not `vip`.
CANDIDATE_RULES = [
    {
        "id": "adult-allow",
        "priority": 100,
        "conclusion": "allow",
        "expr": {
            "all": [
                {"field": "age", "op": "ge", "value": 21},
                {"field": "country", "op": "eq", "value": "NL"},
            ]
        },
    },
    {
        "id": "minor-deny",
        "priority": 100,
        "conclusion": "deny",
        "expr": {"field": "age", "op": "lt", "value": 21},
    },
    {
        "id": "flag-review",
        "priority": 50,
        "conclusion": "review",
        "expr": {"field": "flag", "op": "eq", "value": True},
    },
]

CASES = [
    # allow -> allow (unchanged)
    {"case_id": "case-allow", "facts": {"age": 30, "country": "NL"}},
    # allow -> deny (changed: decided -> decided)
    {"case_id": "case-flip", "facts": {"age": 20, "country": "NL"}},
    # unknown -> unknown (unchanged)
    {"case_id": "case-unknown", "facts": {}},
    # decided review -> unknown flag (changed: decided -> unknown)
    {"case_id": "case-vip",
     "facts": {"age": 30, "country": "US", "vip": True}},
    # no_match -> no_match (unchanged)
    {"case_id": "case-nomatch",
     "facts": {"age": 30, "country": "US", "vip": False, "flag": False}},
]

SIG_ALLOW = {"status": "decided", "conclusion": "allow",
             "decided_priority": 100}
SIG_DENY = {"status": "decided", "conclusion": "deny",
            "decided_priority": 100}
SIG_REVIEW = {"status": "decided", "conclusion": "review",
              "decided_priority": 50}
SIG_UNKNOWN_AGE = {"status": "unknown", "conclusion": None,
                   "decided_priority": 100,
                   "minimal_missing_field_sets": [["age"]],
                   "pending_conclusions": ["allow", "deny"]}
SIG_UNKNOWN_FLAG = {"status": "unknown", "conclusion": None,
                    "decided_priority": 50,
                    "minimal_missing_field_sets": [["flag"]],
                    "pending_conclusions": ["review"]}
SIG_NOMATCH = {"status": "no_match", "conclusion": None,
               "decided_priority": None}


def create_policy(policy=None):
    r = client.post("/api/v1/policies", json=policy or POLICY)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def post_preview(pid, body):
    return client.post(f"/api/v1/policies/{pid}/replacement-preview", json=body)


def preview_body(rules=None, cases=None):
    return {
        "rules": rules if rules is not None else CANDIDATE_RULES,
        "cases": cases if cases is not None else CASES,
    }


# --------------------------------------------------------------------------- #
# Signature and change semantics
# --------------------------------------------------------------------------- #

def test_changes_list_only_signature_differing_cases_sorted():
    pid = create_policy()
    r = post_preview(pid, preview_body())
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["policy_id"] == pid
    assert body["case_count"] == 5
    assert body["change_count"] == 2

    # Only the two changed cases appear, sorted by case_id.
    assert [c["case_id"] for c in body["changes"]] == ["case-flip", "case-vip"]
    assert body["changes"][0]["before"] == SIG_ALLOW
    assert body["changes"][0]["after"] == SIG_DENY
    assert body["changes"][1]["before"] == SIG_REVIEW
    assert body["changes"][1]["after"] == SIG_UNKNOWN_FLAG


def test_transition_counts_cover_all_cases_and_sort_deterministically():
    pid = create_policy()
    r = post_preview(pid, preview_body())
    assert r.status_code == 200, r.text
    body = r.json()

    expected = [
        {"from": SIG_ALLOW, "to": SIG_ALLOW, "count": 1},
        {"from": SIG_ALLOW, "to": SIG_DENY, "count": 1},
        {"from": SIG_REVIEW, "to": SIG_UNKNOWN_FLAG, "count": 1},
        {"from": SIG_UNKNOWN_AGE, "to": SIG_UNKNOWN_AGE, "count": 1},
        {"from": SIG_NOMATCH, "to": SIG_NOMATCH, "count": 1},
    ]
    expected.sort(
        key=lambda item: (canonical_json(item["from"]),
                          canonical_json(item["to"]))
    )
    assert body["transition_counts"] == expected
    # Every case is accounted for exactly once across the transitions.
    assert sum(t["count"] for t in body["transition_counts"]) == 5


def test_repeated_requests_are_byte_identical():
    pid = create_policy()
    first = post_preview(pid, preview_body())
    second = post_preview(pid, preview_body())
    assert first.status_code == 200 and second.status_code == 200
    assert first.content == second.content


def test_cases_submitted_in_any_order_yield_same_response():
    pid = create_policy()
    forward = post_preview(pid, preview_body())
    backward = post_preview(pid, preview_body(cases=list(reversed(CASES))))
    assert forward.status_code == 200 and backward.status_code == 200
    assert forward.json() == backward.json()


def test_identical_candidate_rules_produce_no_changes():
    pid = create_policy()
    r = post_preview(pid, preview_body(rules=POLICY["rules"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["change_count"] == 0
    assert body["changes"] == []
    # All transitions are unchanged (from == to), one per distinct signature.
    assert all(t["from"] == t["to"] for t in body["transition_counts"])
    assert sum(t["count"] for t in body["transition_counts"]) == 5


def test_facts_default_to_empty_object():
    pid = create_policy()
    r = post_preview(pid, preview_body(cases=[{"case_id": "k"}]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case_count"] == 1
    assert body["changes"] == []
    assert body["transition_counts"] == [
        {"from": SIG_UNKNOWN_AGE, "to": SIG_UNKNOWN_AGE, "count": 1}
    ]


def test_conflict_signature_uses_conflicting_conclusions():
    pid = create_policy({"rules": [
        {"id": "r-yes", "priority": 10, "conclusion": "yes",
         "expr": {"field": "x", "op": "eq", "value": 1}},
        {"id": "r-no", "priority": 10, "conclusion": "no",
         "expr": {"field": "x", "op": "eq", "value": 1}},
    ]})
    candidate = [
        {"id": "r-yes", "priority": 10, "conclusion": "yes",
         "expr": {"field": "x", "op": "eq", "value": 1}},
    ]
    r = post_preview(pid, preview_body(
        rules=candidate, cases=[{"case_id": "k", "facts": {"x": 1}}]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["change_count"] == 1
    change = body["changes"][0]
    assert change["before"] == {
        "status": "conflict",
        "conclusion": None,
        "decided_priority": 10,
        "conflicting_conclusions": ["no", "yes"],
    }
    assert change["after"] == {
        "status": "decided", "conclusion": "yes", "decided_priority": 10}
    assert body["transition_counts"] == [
        {"from": change["before"], "to": change["after"], "count": 1}
    ]


def test_unknown_signature_tracks_missing_field_sets():
    # Candidate drops the country requirement: a case missing only `country`
    # is unknown under the stored policy but decided under the candidate;
    # the before-signature carries the minimal missing field sets.
    pid = create_policy()
    candidate = [
        {"id": "adult-allow", "priority": 100, "conclusion": "allow",
         "expr": {"field": "age", "op": "ge", "value": 18}},
        {"id": "minor-deny", "priority": 100, "conclusion": "deny",
         "expr": {"field": "age", "op": "lt", "value": 18}},
    ]
    r = post_preview(pid, preview_body(
        rules=candidate,
        cases=[{"case_id": "k", "facts": {"age": 20}}]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["change_count"] == 1
    change = body["changes"][0]
    assert change["before"] == {
        "status": "unknown",
        "conclusion": None,
        "decided_priority": 100,
        "minimal_missing_field_sets": [["country"]],
        "pending_conclusions": ["allow"],
    }
    assert change["after"] == {
        "status": "decided", "conclusion": "allow", "decided_priority": 100}
    assert body["transition_counts"] == [
        {"from": change["before"], "to": change["after"], "count": 1}
    ]


# --------------------------------------------------------------------------- #
# Validation and storage guarantees
# --------------------------------------------------------------------------- #

def test_invalid_candidate_rules_return_422():
    pid = create_policy()
    bad_rule_sets = [
        [],
        "nope",
        [{"priority": 1, "conclusion": "x"}],                      # no expr
        [{"priority": 1, "conclusion": "x",
          "expr": {"field": "a", "op": "bad", "value": 1}}],       # bad op
        [{"id": "d", "priority": 1, "conclusion": "x",
          "expr": {"field": "a", "op": "eq", "value": 1}},
         {"id": "d", "priority": 2, "conclusion": "y",
          "expr": {"field": "b", "op": "eq", "value": 1}}],        # dup id
    ]
    for rules in bad_rule_sets:
        r = post_preview(pid, preview_body(rules=rules))
        assert r.status_code == 422, rules
        out = r.json()
        assert out["detail"] == "Policy structure validation failed"
        assert isinstance(out["errors"], list) and out["errors"]


def test_invalid_cases_return_422():
    pid = create_policy()
    bad_bodies = [
        preview_body(cases=[]),
        preview_body(cases="nope"),
        preview_body(cases=[1]),
        preview_body(cases=[{"facts": {}}]),
        preview_body(cases=[{"case_id": "", "facts": {}}]),
        preview_body(cases=[{"case_id": "a", "facts": []}]),
        preview_body(cases=[{"case_id": "a"}, {"case_id": "a"}]),
        {"rules": CANDIDATE_RULES},                                # no cases
    ]
    for body in bad_bodies:
        r = post_preview(pid, body)
        assert r.status_code == 422, body
        out = r.json()
        assert out["detail"] == "Policy structure validation failed"
        assert isinstance(out["errors"], list) and out["errors"]


def test_duplicate_case_id_reports_every_duplicate():
    pid = create_policy()
    r = post_preview(pid, preview_body(
        cases=[{"case_id": "a"}, {"case_id": "b"}, {"case_id": "a"}]))
    assert r.status_code == 422
    assert any("duplicate case id" in e for e in r.json()["errors"])


def test_rule_and_case_errors_are_reported_together():
    pid = create_policy()
    r = post_preview(pid, {"rules": [], "cases": [{"case_id": "a"},
                                                  {"case_id": "a"}]})
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any(e.startswith("rules:") for e in errors)
    assert any("duplicate case id" in e for e in errors)


def test_preview_never_writes_to_store():
    pid = create_policy()
    before = client.get(f"/api/v1/policies/{pid}").json()

    r = post_preview(pid, preview_body())
    assert r.status_code == 200
    assert r.json()["change_count"] > 0  # the preview really used candidates

    after = client.get(f"/api/v1/policies/{pid}").json()
    assert after == before
    assert store.count() == 1

    # Adjudication still uses the stored rules, not the candidate ones.
    r = client.post(f"/api/v1/policies/{pid}/evaluate",
                    json={"facts": {"age": 20, "country": "NL"}})
    assert r.json()["conclusion"] == "allow"

    # A rejected (422) preview does not create or modify policies either.
    r = post_preview(pid, {"rules": [], "cases": [{"case_id": "a"}]})
    assert r.status_code == 422
    assert store.count() == 1
    assert client.get(f"/api/v1/policies/{pid}").json() == before


def test_unknown_policy_returns_404():
    r = post_preview("nope", preview_body())
    assert r.status_code == 404


def test_malformed_json_is_400():
    pid = create_policy()
    r = client.post(
        f"/api/v1/policies/{pid}/replacement-preview",
        content="{not json",
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
