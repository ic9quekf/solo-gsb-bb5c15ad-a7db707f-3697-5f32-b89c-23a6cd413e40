"""Counterfactual search: inclusion-minimal fact edits forcing a target verdict.

Given the current facts and, per field, a list of candidate replacement values,
the search enumerates field modifications (at most one candidate value per field)
using the existing priority/three-valued adjudication engine.  A modification is
reported only when the resulting verdict is uniquely *decided* with the requested
conclusion: outcomes that stay ``unknown``, end in ``conflict`` or fall through to
``no_match`` are excluded.

Minimality is defined on the set of *fields* modified.  Once a field set reaches
the target, every strict superset of it is pruned, while *every* candidate-value
assignment on a minimal field set that reaches the target is reported (distinct
values on the same minimal field set are mutually incomparable).

Results are sorted stably by the number of changed fields, the field names and
the canonical JSON encoding of the chosen values.
"""
from __future__ import annotations

import json
from itertools import combinations, product
from typing import Any

from .engine import adjudicate

# Safety cap on adjudications performed while expanding the candidate space.
MAX_COUNTERFACTUAL_COMBINATIONS = 100_000


class CounterfactualSearchError(ValueError):
    """Raised when the candidate space cannot be exhaustively searched."""


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding used for de-duplication and value ordering."""
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def _dedupe_and_order(values: list[Any]) -> list[Any]:
    """Drop candidate values equal under canonical JSON, then order them."""
    unique: dict[str, Any] = {}
    for value in values:
        unique.setdefault(canonical_json(value), value)
    return [unique[key] for key in sorted(unique)]


def _make_item(changes: dict[str, Any], verdict: dict) -> dict:
    ordered = {field: changes[field] for field in sorted(changes)}
    return {
        "changes": ordered,
        "verdict": verdict,
        "level_trace": verdict.get("level_trace"),
    }


def _hits_target(verdict: dict, target_conclusion: str) -> bool:
    return (
        verdict["status"] == "decided"
        and verdict["conclusion"] == target_conclusion
    )


def counterfactuals(
    policy: dict,
    facts: dict,
    target_conclusion: str,
    candidates: dict[str, list[Any]],
    max_changes: int,
) -> tuple[dict, list[dict]]:
    """Return ``(original_verdict, items)`` with all minimal change sets.

    When the original facts already yield the target conclusion, a single item
    with an empty ``changes`` map is returned.  Otherwise every reported item
    contains ``changes``, the final ``verdict`` and its ``level_trace``.
    """
    original = adjudicate(policy, facts)
    if _hits_target(original, target_conclusion):
        return original, [_make_item({}, original)]

    fields = sorted(candidates)
    options = {field: _dedupe_and_order(candidates[field]) for field in fields}
    depth_limit = min(max_changes, len(fields))

    minimal_field_sets: list[frozenset[str]] = []
    items: list[dict] = []
    evaluated = 0

    for size in range(1, depth_limit + 1):
        for subset in combinations(fields, size):
            subset_set = frozenset(subset)
            # A strict superset of an already successful field set can never be
            # inclusion-minimal, regardless of the values chosen.
            if any(kept <= subset_set for kept in minimal_field_sets):
                continue
            reached = False
            field_options = [options[field] for field in subset]
            for values in product(*field_options):
                changes = dict(zip(subset, values))
                verdict = adjudicate(policy, {**facts, **changes})
                evaluated += 1
                if evaluated > MAX_COUNTERFACTUAL_COMBINATIONS:
                    raise CounterfactualSearchError(
                        "candidate space exceeds "
                        f"{MAX_COUNTERFACTUAL_COMBINATIONS} evaluated "
                        "combinations; narrow candidates or reduce max_changes"
                    )
                if _hits_target(verdict, target_conclusion):
                    reached = True
                    items.append(_make_item(changes, verdict))
            if reached:
                minimal_field_sets.append(subset_set)

    items.sort(
        key=lambda item: (
            len(item["changes"]),
            tuple(item["changes"]),
            tuple(canonical_json(item["changes"][f]) for f in item["changes"]),
        )
    )
    return original, items
