"""FastAPI application: policy management, adjudication and health checks."""
from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .batch import BatchPlanSearchError, batch_data_plans
from .counterfactual import CounterfactualSearchError, counterfactuals
from .engine import adjudicate
from .flow import run_flow
from .preview import replacement_preview
from .sources import merge_fact_sources
from .store import store
from .validation import (
    PolicyValidationError,
    SourceValidationError,
    validate_batch_data_plans_request,
    validate_counterfactual_request,
    validate_evaluate_sources_request,
    validate_flow_request,
    validate_policy,
    validate_replacement_preview_request,
)

API_PREFIX = "/api/v1"

app = FastAPI(
    title="Explainable Rule Adjudication API",
    version="1.0.0",
    description=(
        "Submit policies built from all/any/not and comparison expressions; "
        "evaluate facts with three-valued (true/false/unknown) logic and "
        "receive priority-based verdicts with per-node derivation traces, "
        "inclusion-minimal missing-field sets, inclusion-minimal "
        "counterfactual fact edits that force a target conclusion, and "
        "batch data plans: inclusion-minimal global field sets that make "
        "every unknown case decidable. Replacement previews adjudicate a "
        "case set with the stored and candidate rules side by side and "
        "report the business-impact changes without touching storage. "
        "Adjudication flows chain stored policies through verdict routes "
        "and return the termination reason, the final business signature "
        "and the ordered node trace. Source-based evaluation merges facts "
        "from multiple named, integer-priority sources per field before "
        "adjudicating with the same engine."
    ),
)


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #

@app.exception_handler(PolicyValidationError)
async def policy_validation_handler(
    request: Request, exc: PolicyValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Policy structure validation failed",
            "errors": exc.errors,
        },
    )


@app.exception_handler(CounterfactualSearchError)
async def counterfactual_search_handler(
    request: Request, exc: CounterfactualSearchError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Counterfactual search failed",
            "errors": [str(exc)],
        },
    )


@app.exception_handler(BatchPlanSearchError)
async def batch_plan_search_handler(
    request: Request, exc: BatchPlanSearchError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Batch data plan search failed",
            "errors": [str(exc)],
        },
    )


@app.exception_handler(SourceValidationError)
async def source_validation_handler(
    request: Request, exc: SourceValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": "Fact source validation failed",
            "errors": exc.errors,
        },
    )


# --------------------------------------------------------------------------- #
# Request / response models
# --------------------------------------------------------------------------- #

class EvaluateRequest(BaseModel):
    facts: dict[str, Any] = Field(
        default_factory=dict,
        description="Facts keyed by field name. Absent or null fields are unknown.",
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["health"])
async def health_liveness() -> dict:
    """Liveness probe: the process can serve requests."""
    return {"status": "ok"}


@app.get("/health/ready", tags=["health"])
async def health_readiness() -> dict:
    """Readiness probe: storage is reachable."""
    return {"status": "ok", "policies": store.count()}


# --------------------------------------------------------------------------- #
# Policy management
# --------------------------------------------------------------------------- #

@app.post(f"{API_PREFIX}/policies", tags=["policies"], status_code=201)
async def create_policy(request: Request) -> dict:
    payload = await _json_body(request)
    rules = validate_policy(payload)
    policy_id = store.new_id()
    record = store.put(policy_id, {"rules": rules})
    return record


@app.put(f"{API_PREFIX}/policies/{{policy_id}}", tags=["policies"])
async def replace_policy(policy_id: str, request: Request) -> dict:
    payload = await _json_body(request)
    rules = validate_policy(payload)
    record = store.put(policy_id, {"rules": rules})
    return record


@app.get(f"{API_PREFIX}/policies", tags=["policies"])
async def list_policies() -> dict:
    return {"policies": store.list()}


@app.get(f"{API_PREFIX}/policies/{{policy_id}}", tags=["policies"])
async def get_policy(policy_id: str) -> dict:
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return record


@app.delete(f"{API_PREFIX}/policies/{{policy_id}}", tags=["policies"])
async def delete_policy(policy_id: str) -> dict:
    if not store.delete(policy_id):
        raise HTTPException(status_code=404, detail="Policy not found")
    return {"id": policy_id, "deleted": True}


# --------------------------------------------------------------------------- #
# Adjudication
# --------------------------------------------------------------------------- #

@app.post(f"{API_PREFIX}/policies/{{policy_id}}/evaluate", tags=["adjudication"])
async def evaluate_policy(policy_id: str, body: Optional[EvaluateRequest] = None) -> dict:
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    facts = body.facts if body is not None else {}
    verdict = adjudicate(record, facts)
    verdict["policy_id"] = policy_id
    return verdict


@app.post(
    f"{API_PREFIX}/policies/{{policy_id}}/evaluate-sources",
    tags=["adjudication"],
)
async def evaluate_policy_sources(policy_id: str, request: Request) -> dict:
    """Merge priority-ordered named fact sources, then adjudicate.

    For every field only the sources providing it at the highest priority
    participate: strictly equal non-null values are adopted, while null or
    disagreeing values leave the field ``unresolved`` (lower-priority sources
    are reported as discarded and never backfill).  The merged facts are then
    adjudicated with the regular engine.
    """
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    payload = await _json_body(request)
    sources = validate_evaluate_sources_request(payload)

    merged = merge_fact_sources(sources)
    verdict = adjudicate(record, merged["facts"])

    # Field reports sorted by field name for a stable response envelope.
    merged_fields = {
        field: merged["fields"][field]
        for field in sorted(merged["fields"])
    }
    return {
        "policy_id": policy_id,
        "source_count": len(sources),
        "merged_facts": merged["facts"],
        "unresolved_fields": merged["unresolved_fields"],
        "fields": merged_fields,
        "verdict": verdict,
    }


@app.post(
    f"{API_PREFIX}/policies/{{policy_id}}/counterfactuals",
    tags=["adjudication"],
)
async def policy_counterfactuals(policy_id: str, request: Request) -> dict:
    """Find inclusion-minimal fact edits that uniquely force a target verdict."""
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    payload = await _json_body(request)
    facts, target, candidates, max_changes = validate_counterfactual_request(
        payload
    )

    original_verdict, items = counterfactuals(
        record, facts, target, candidates, max_changes
    )
    return {
        "policy_id": policy_id,
        "target_conclusion": target,
        "original_status": original_verdict["status"],
        "original_conclusion": original_verdict.get("conclusion"),
        "count": len(items),
        "counterfactuals": items,
    }


@app.post(
    f"{API_PREFIX}/policies/{{policy_id}}/batch-data-plans",
    tags=["adjudication"],
)
async def policy_batch_data_plans(policy_id: str, request: Request) -> dict:
    """Combine unknown cases' minimal field sets into global data plans."""
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    payload = await _json_body(request)
    cases = validate_batch_data_plans_request(payload)

    summaries, plans = batch_data_plans(record, cases)
    return {
        "policy_id": policy_id,
        "case_count": len(summaries),
        "unknown_count": sum(
            1 for summary in summaries if summary["status"] == "unknown"
        ),
        "cases": summaries,
        "count": len(plans),
        "plans": plans,
    }


@app.post(
    f"{API_PREFIX}/policies/{{policy_id}}/replacement-preview",
    tags=["adjudication"],
)
async def policy_replacement_preview(policy_id: str, request: Request) -> dict:
    """Preview the impact of replacing the policy's rules on submitted cases.

    Adjudicates every case with the stored policy and with the candidate
    ``rules`` from the request body, then reports the cases whose business
    signature changed plus the aggregated old-to-new signature transitions.
    The stored policy is never modified.
    """
    record = store.get(policy_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Policy not found")

    payload = await _json_body(request)
    candidate_rules, cases = validate_replacement_preview_request(payload)

    changes, transition_counts = replacement_preview(
        record, candidate_rules, cases
    )
    return {
        "policy_id": policy_id,
        "case_count": len(cases),
        "change_count": len(changes),
        "changes": changes,
        "transition_counts": transition_counts,
    }


@app.post(f"{API_PREFIX}/adjudication-flows/evaluate", tags=["adjudication"])
async def evaluate_adjudication_flow(request: Request) -> dict:
    """Execute an inline adjudication flow over stored policies.

    The body inlines ``start``, ``nodes`` (each referencing a stored
    policy plus verdict routes) and the shared ``facts``.  Validation
    covers node and route shape, policy references and cycle-freedom of
    the subgraph reachable from ``start``.  Every node is adjudicated
    with the regular engine; the flow ends at the first node whose
    verdict matches no declared route.  The policy store is only read,
    never modified.
    """
    payload = await _json_body(request)
    start, nodes, facts = validate_flow_request(payload, store.get)
    policies: dict[str, dict] = {}
    for node in nodes:
        policy_id = node["policy_id"]
        if policy_id not in policies:
            policies[policy_id] = store.get(policy_id)
    return run_flow(start, nodes, facts, policies)


async def _json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON",
        )
