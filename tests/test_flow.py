"""End-to-end tests for POST /api/v1/adjudication-flows/evaluate.

Run with: python tests/test_flow.py
Requires: fastapi, httpx (see requirements-dev.txt).
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


POLICY_INCOME = {
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

POLICY_VIP = {
    "rules": [
        {
            "id": "vip",
            "priority": 5,
            "conclusion": "fast-track",
            "expr": {"field": "vip", "op": "eq", "value": True},
        },
        {
            "id": "fallback",
            "priority": 1,
            "conclusion": "standard",
            "expr": {"field": "country", "op": "eq", "value": "NL"},
        },
    ]
}

POLICY_CONFLICT = {
    "rules": [
        {
            "id": "a",
            "priority": 1,
            "conclusion": "yes",
            "expr": {"field": "v", "op": "ge", "value": 10},
        },
        {
            "id": "b",
            "priority": 1,
            "conclusion": "no",
            "expr": {"field": "v", "op": "ne", "value": 5},
        },
    ]
}


def create_policy(body):
    r = client.post("/api/v1/policies", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def income_and_vip_policies():
    return create_policy(POLICY_INCOME), create_policy(POLICY_VIP)


def evaluate_flow(payload):
    return client.post("/api/v1/adjudication-flows/evaluate", json=payload)


def test_flow_decided_conclusion_routing_two_hops():
    p_income, p_vip = income_and_vip_policies()
    r = evaluate_flow({
        "start": "income-check",
        "nodes": [
            {"id": "income-check", "policy_id": p_income,
             "routes": [
                 {"on": "decided", "conclusion": "approve", "next": "vip-lane"},
             ]},
            {"id": "vip-lane", "policy_id": p_vip, "routes": []},
        ],
        "facts": {"income": 80000, "debt_ratio": 0.2, "vip": True},
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["termination_reason"] == "no_matching_route"
    assert body["final_node_id"] == "vip-lane"
    assert body["final_signature"] == {
        "status": "decided",
        "conclusion": "fast-track",
        "decided_priority": 5,
    }
    assert body["step_count"] == 2
    steps = body["steps"]
    assert [s["node_id"] for s in steps] == ["income-check", "vip-lane"]
    assert steps[0]["policy_id"] == p_income
    assert steps[0]["status"] == "decided"
    assert steps[0]["conclusion"] == "approve"
    assert steps[0]["decided_priority"] == 10
    assert steps[0]["route"] == {
        "on": "decided", "conclusion": "approve", "next": "vip-lane"}
    assert steps[1]["policy_id"] == p_vip
    assert steps[1]["route"] is None


def test_flow_conclusion_selects_branch():
    p_income, p_vip = income_and_vip_policies()
    p_conflict = create_policy(POLICY_CONFLICT)
    payload = {
        "start": "income-check",
        "nodes": [
            {"id": "income-check", "policy_id": p_income,
             "routes": [
                 {"on": "decided", "conclusion": "approve", "next": "vip-lane"},
                 {"on": "decided", "conclusion": "reject", "next": "audit"},
             ]},
            {"id": "vip-lane", "policy_id": p_vip, "routes": []},
            {"id": "audit", "policy_id": p_conflict, "routes": []},
        ],
        "facts": {"income": 80000, "debt_ratio": 0.7, "v": 10},
    }
    body = evaluate_flow(payload).json()
    # debt_ratio 0.7 rejects at the income node, so the audit branch runs.
    assert [s["node_id"] for s in body["steps"]] == ["income-check", "audit"]
    assert body["final_node_id"] == "audit"
    assert body["final_signature"]["status"] == "conflict"
    assert body["final_signature"]["conflicting_conclusions"] == ["no", "yes"]


def test_flow_unknown_and_no_match_routing():
    p_income, p_vip = income_and_vip_policies()
    payload = {
        "start": "income-check",
        "nodes": [
            {"id": "income-check", "policy_id": p_income,
             "routes": [{"on": "unknown", "next": "vip-lane"},
                        {"on": "no_match", "next": "vip-lane"}]},
            {"id": "vip-lane", "policy_id": p_vip, "routes": []},
        ],
    }
    # Missing facts: the income level is undecidable -> unknown route.
    body = evaluate_flow({**payload, "facts": {}}).json()
    assert [s["node_id"] for s in body["steps"]] == ["income-check", "vip-lane"]
    assert body["steps"][0]["status"] == "unknown"
    assert body["steps"][0]["route"] == {"on": "unknown", "next": "vip-lane"}
    assert body["final_signature"]["status"] == "unknown"
    assert body["final_signature"]["minimal_missing_field_sets"] == [["vip"]]

    # Fully known facts with no matching rule -> no_match route.
    facts = {"income": 30000, "debt_ratio": 0.5, "vip": False, "country": "US"}
    body = evaluate_flow({**payload, "facts": facts}).json()
    assert body["steps"][0]["status"] == "no_match"
    assert body["steps"][0]["route"] == {"on": "no_match", "next": "vip-lane"}
    assert body["final_signature"] == {
        "status": "no_match", "conclusion": None, "decided_priority": None}


def test_flow_conflict_routing():
    p_income, p_vip = income_and_vip_policies()
    p_conflict = create_policy(POLICY_CONFLICT)
    body = evaluate_flow({
        "start": "income-check",
        "nodes": [
            {"id": "income-check", "policy_id": p_income,
             "routes": [{"on": "no_match", "next": "audit"}]},
            {"id": "audit", "policy_id": p_conflict,
             "routes": [{"on": "conflict", "next": "manual"}]},
            {"id": "manual", "policy_id": p_vip, "routes": []},
        ],
        "facts": {"income": 30000, "debt_ratio": 0.5, "v": 10, "vip": True},
    })
    assert body.status_code == 200, body.text
    payload = body.json()
    assert [s["node_id"] for s in payload["steps"]] == [
        "income-check", "audit", "manual"]
    assert payload["steps"][1]["status"] == "conflict"
    assert payload["steps"][1]["route"] == {"on": "conflict", "next": "manual"}
    assert payload["final_signature"] == {
        "status": "decided", "conclusion": "fast-track", "decided_priority": 5}


def test_flow_no_matching_route_ends_at_first_node():
    p_income, _ = income_and_vip_policies()
    body = evaluate_flow({
        "start": "only",
        "nodes": [{"id": "only", "policy_id": p_income,
                   "routes": [{"on": "decided", "conclusion": "approve",
                               "next": "other"},
                              {"on": "unknown", "next": "other"}]},
                  {"id": "other", "policy_id": p_income, "routes": []}],
        "facts": {"income": 80000, "debt_ratio": 0.7},  # decided: reject
    }).json()
    assert body["termination_reason"] == "no_matching_route"
    assert body["final_node_id"] == "only"
    assert body["step_count"] == 1
    assert body["steps"][0]["route"] is None
    assert body["final_signature"] == {
        "status": "decided", "conclusion": "reject", "decided_priority": 10}


def test_flow_policy_reused_across_nodes():
    p_income, _ = income_and_vip_policies()
    body = evaluate_flow({
        "start": "first",
        "nodes": [
            {"id": "first", "policy_id": p_income,
             "routes": [{"on": "no_match", "next": "second"}]},
            {"id": "second", "policy_id": p_income, "routes": []},
        ],
        "facts": {"income": 30000, "debt_ratio": 0.5},
    })
    assert body.status_code == 200, body.text
    steps = body.json()["steps"]
    assert [s["node_id"] for s in steps] == ["first", "second"]
    assert all(s["policy_id"] == p_income for s in steps)
    assert all(s["status"] == "no_match" for s in steps)


def test_flow_unreachable_cycle_is_allowed():
    p_income, p_vip = income_and_vip_policies()
    body = evaluate_flow({
        "start": "entry",
        "nodes": [
            {"id": "entry", "policy_id": p_income, "routes": []},
            {"id": "loop-a", "policy_id": p_vip,
             "routes": [{"on": "decided", "conclusion": "fast-track",
                         "next": "loop-b"}]},
            {"id": "loop-b", "policy_id": p_vip,
             "routes": [{"on": "no_match", "next": "loop-a"}]},
        ],
        "facts": {"income": 80000, "debt_ratio": 0.2},
    })
    assert body.status_code == 200, body.text
    payload = body.json()
    assert payload["step_count"] == 1
    assert payload["final_node_id"] == "entry"


def test_flow_three_hop_ordered_trace():
    p_income, p_vip = income_and_vip_policies()
    p_conflict = create_policy(POLICY_CONFLICT)
    body = evaluate_flow({
        "start": "n1",
        "nodes": [
            {"id": "n1", "policy_id": p_income,
             "routes": [{"on": "no_match", "next": "n2"}]},
            {"id": "n2", "policy_id": p_vip,
             "routes": [{"on": "no_match", "next": "n3"}]},
            {"id": "n3", "policy_id": p_conflict, "routes": []},
        ],
        "facts": {"income": 30000, "debt_ratio": 0.5,
                  "vip": False, "country": "US", "v": 10},
    }).json()
    assert [s["node_id"] for s in body["steps"]] == ["n1", "n2", "n3"]
    assert body["step_count"] == 3
    assert body["final_node_id"] == "n3"
    assert body["final_signature"]["status"] == "conflict"


def test_flow_facts_default_to_empty_object():
    p_income, _ = income_and_vip_policies()
    r = evaluate_flow({
        "start": "only",
        "nodes": [{"id": "only", "policy_id": p_income, "routes": []}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["final_signature"]["status"] == "unknown"


def test_flow_store_is_not_modified():
    p_income, p_vip = income_and_vip_policies()
    before = {pid: client.get(f"/api/v1/policies/{pid}").json()
              for pid in (p_income, p_vip)}
    r = evaluate_flow({
        "start": "a",
        "nodes": [
            {"id": "a", "policy_id": p_income,
             "routes": [{"on": "decided", "conclusion": "approve", "next": "b"}]},
            {"id": "b", "policy_id": p_vip, "routes": []},
        ],
        "facts": {"income": 80000, "debt_ratio": 0.2, "vip": True},
    })
    assert r.status_code == 200, r.text
    after = {pid: client.get(f"/api/v1/policies/{pid}").json()
             for pid in (p_income, p_vip)}
    assert before == after
    assert client.get("/health/ready").json()["policies"] == 2


def test_flow_rejects_unknown_policy_reference():
    p_income, _ = income_and_vip_policies()
    r = evaluate_flow({
        "start": "a",
        "nodes": [
            {"id": "a", "policy_id": p_income,
             "routes": [{"on": "no_match", "next": "b"}]},
            {"id": "b", "policy_id": "does-not-exist", "routes": []},
        ],
    })
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any("unknown policy" in e for e in errors)


def test_flow_rejects_reachable_cycle_of_two():
    p_income, _ = income_and_vip_policies()
    r = evaluate_flow({
        "start": "a",
        "nodes": [
            {"id": "a", "policy_id": p_income,
             "routes": [{"on": "no_match", "next": "b"}]},
            {"id": "b", "policy_id": p_income,
             "routes": [{"on": "no_match", "next": "a"}]},
        ],
    })
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any("cycle" in e and "a" in e and "b" in e for e in errors)


def test_flow_rejects_self_loop():
    p_income, _ = income_and_vip_policies()
    r = evaluate_flow({
        "start": "a",
        "nodes": [{"id": "a", "policy_id": p_income,
                   "routes": [{"on": "unknown", "next": "a"}]}],
    })
    assert r.status_code == 422
    assert any("cycle" in e for e in r.json()["errors"])


def test_flow_rejects_unknown_start_and_dangling_next():
    p_income, _ = income_and_vip_policies()
    r = evaluate_flow({
        "start": "ghost",
        "nodes": [{"id": "a", "policy_id": p_income,
                   "routes": [{"on": "no_match", "next": "nowhere"}]}],
    })
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any("start" in e and "ghost" in e for e in errors)
    assert any("next" in e and "nowhere" in e for e in errors)


def test_flow_rejects_malformed_nodes_and_routes():
    p_income, _ = income_and_vip_policies()
    r = evaluate_flow({
        "start": "a",
        "nodes": [
            {"id": "a", "policy_id": p_income,
             "routes": [
                 {"on": "decided", "next": "a"},            # missing conclusion
                 {"on": "unknown", "conclusion": "x", "next": "a"},
                 {"on": "maybe", "next": "a"},
                 {"on": "no_match"},                        # missing next
                 {"on": "no_match", "next": "a"},
                 {"on": "no_match", "next": "a"},           # duplicate
             ]},
            {"id": "a", "policy_id": p_income},            # duplicate id
            "not-an-object",
        ],
    })
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any("conclusion" in e and "decided" in e for e in errors)
    assert any("only allowed on 'decided'" in e for e in errors)
    assert any("must be one of" in e for e in errors)
    assert any("next: must be a non-empty string" in e for e in errors)
    assert any("duplicate route" in e for e in errors)
    assert any("duplicate node id" in e for e in errors)
    assert any("must be an object" in e for e in errors)


def test_flow_rejects_bad_envelope():
    r = client.post("/api/v1/adjudication-flows/evaluate", json=[1, 2])
    assert r.status_code == 422
    r = evaluate_flow({"start": "a"})
    assert r.status_code == 422
    assert any("nodes" in e for e in r.json()["errors"])
    r = evaluate_flow({"start": "a", "nodes": [], "facts": []})
    assert r.status_code == 422
    errors = r.json()["errors"]
    assert any("nodes: must be a non-empty array" in e for e in errors)
    assert any("facts" in e for e in errors)


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
