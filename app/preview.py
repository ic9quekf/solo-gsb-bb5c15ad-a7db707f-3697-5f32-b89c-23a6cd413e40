"""Replacement preview: impact of candidate rules on submitted cases.

The preview adjudicates every submitted case twice — once with the stored
policy and once with the candidate rules — and distils each verdict into a
*business signature*: the status, conclusion and decided priority, plus the
key sets that characterise ``conflict`` (conflicting conclusions) and
``unknown`` (minimal missing field sets and pending conclusions) outcomes.

Only cases whose signature differs between the two adjudications are listed
as changes; the full case list is additionally folded into
``transition_counts`` keyed by the ``old signature -> new signature`` pair
(unchanged cases appear as transitions whose ``from`` equals their ``to``).
Changes are sorted by ``case_id`` and transitions by the canonical JSON of
their signatures, so responses are deterministic.  Nothing is written to
the policy store.
"""
from __future__ import annotations

from .counterfactual import canonical_json
from .engine import adjudicate


def verdict_signature(verdict: dict) -> dict:
    """Business-result signature of a verdict.

    Composed of ``status``, ``conclusion`` and ``decided_priority``; for
    ``conflict`` verdicts the conflicting conclusions are added, for
    ``unknown`` verdicts the minimal missing field sets and the pending
    conclusions.
    """
    signature = {
        "status": verdict["status"],
        "conclusion": verdict.get("conclusion"),
        "decided_priority": verdict.get("decided_priority"),
    }
    if verdict["status"] == "conflict":
        signature["conflicting_conclusions"] = verdict[
            "conflicting_conclusions"
        ]
    elif verdict["status"] == "unknown":
        signature["minimal_missing_field_sets"] = verdict[
            "minimal_missing_field_sets"
        ]
        signature["pending_conclusions"] = verdict["pending_conclusions"]
    return signature


def replacement_preview(
    policy: dict,
    candidate_rules: list[dict],
    cases: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Adjudicate ``cases`` with the current and the candidate rules.

    Returns ``(changes, transition_counts)``.  ``changes`` lists one
    ``{"case_id", "before", "after"}`` entry per case whose signature
    changed, sorted by ``case_id``; ``transition_counts`` aggregates every
    case by its ``old signature -> new signature`` pair, sorted by the
    canonical JSON encoding of the pair.
    """
    candidate = {"rules": candidate_rules}
    changes: list[dict] = []
    transitions: dict[tuple[str, str], dict] = {}

    for case in cases:
        before = verdict_signature(adjudicate(policy, case["facts"]))
        after = verdict_signature(adjudicate(candidate, case["facts"]))
        before_key = canonical_json(before)
        after_key = canonical_json(after)

        key = (before_key, after_key)
        if key in transitions:
            transitions[key]["count"] += 1
        else:
            transitions[key] = {"from": before, "to": after, "count": 1}

        if before_key != after_key:
            changes.append(
                {"case_id": case["case_id"], "before": before, "after": after}
            )

    changes.sort(key=lambda change: change["case_id"])
    transition_counts = sorted(
        transitions.values(),
        key=lambda item: (
            canonical_json(item["from"]),
            canonical_json(item["to"]),
        ),
    )
    return changes, transition_counts
