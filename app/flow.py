"""Adjudication flows: chained policy adjudication with verdict routing.

A flow request inlines a small directed graph: every node references a
*stored* policy and declares routes keyed on that policy's verdict — a
specific ``decided`` conclusion, or the ``unknown`` / ``conflict`` /
``no_match`` statuses.  The same policy may back any number of nodes.
Request validation (see ``validation.validate_flow_request``) guarantees
the subgraph reachable from ``start`` is acyclic, so execution visits each
reachable node at most once and always terminates: the flow ends at the
first node whose verdict matches no declared route.

Every node is adjudicated with the regular priority/three-valued engine
against the same submitted facts; the response distils the final verdict
into the business signature used by replacement previews and reports the
ordered node trace.  The policy store is only read, never modified.
"""
from __future__ import annotations

from typing import Optional

from .engine import adjudicate
from .preview import verdict_signature

# The only way a valid flow ends: the current verdict matches no route.
TERMINATION_NO_MATCHING_ROUTE = "no_matching_route"


def _match_route(routes: list[dict], verdict: dict) -> Optional[dict]:
    """First route matching the verdict, else ``None``.

    ``decided`` routes additionally require the conclusion to match;
    duplicate match keys are rejected at validation time, so the first
    match is deterministic.
    """
    status = verdict["status"]
    for route in routes:
        if route["on"] != status:
            continue
        if status == "decided" and route["conclusion"] != verdict["conclusion"]:
            continue
        return route
    return None


def run_flow(
    start: str,
    nodes: list[dict],
    facts: dict,
    policies: dict[str, dict],
) -> dict:
    """Execute a validated flow and return the response payload.

    ``policies`` maps every referenced policy id to its stored record.
    """
    nodes_by_id = {node["id"]: node for node in nodes}
    steps: list[dict] = []
    current_id = start

    # A validated flow is acyclic, so it ends within ``len(nodes)`` steps.
    for _ in range(len(nodes)):
        node = nodes_by_id[current_id]
        verdict = adjudicate(policies[node["policy_id"]], facts)
        route = _match_route(node["routes"], verdict)
        steps.append(
            {
                "node_id": node["id"],
                "policy_id": node["policy_id"],
                "status": verdict["status"],
                "conclusion": verdict.get("conclusion"),
                "decided_priority": verdict.get("decided_priority"),
                "route": dict(route) if route is not None else None,
            }
        )
        if route is None:
            return {
                "termination_reason": TERMINATION_NO_MATCHING_ROUTE,
                "final_node_id": node["id"],
                "final_signature": verdict_signature(verdict),
                "step_count": len(steps),
                "steps": steps,
            }
        current_id = route["next"]

    raise RuntimeError(  # pragma: no cover - validation prevents this
        "adjudication flow did not terminate within the node count"
    )
