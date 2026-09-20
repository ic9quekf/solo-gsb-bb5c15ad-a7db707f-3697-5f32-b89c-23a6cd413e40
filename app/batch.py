"""Batch data plans: inclusion-minimal global field sets for unknown cases.

The batch endpoint adjudicates every submitted case with the regular engine.
Cases that come back ``unknown`` each carry ``minimal_missing_field_sets`` —
the inclusion-minimal field sets whose provision makes *that* case decidable.
This module folds those per-case families into the inclusion-minimal *global*
field sets: supplying every field of one global plan makes all unknown cases
decidable at once, and each plan lists the per-case minimal sets it covers.

Combination is an iterative cross-product of unions: start from the empty set
and fold in one case family at a time, de-duplicating and pruning strict
supersets after every step so the accumulator stays inclusion-minimal
throughout.  The number of union combinations generated is capped
(``MAX_BATCH_PLAN_COMBINATIONS``, configurable via environment variable);
exceeding the cap aborts with an error rather than truncating the result.
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

from .engine import adjudicate

_DEFAULT_MAX_COMBINATIONS = 100_000


def _configured_max_combinations() -> int:
    raw = os.environ.get("MAX_BATCH_PLAN_COMBINATIONS")
    if raw is None:
        return _DEFAULT_MAX_COMBINATIONS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_COMBINATIONS
    return value if value > 0 else _DEFAULT_MAX_COMBINATIONS


# Safety cap on union combinations generated while folding case families.
MAX_BATCH_PLAN_COMBINATIONS = _configured_max_combinations()


class BatchPlanSearchError(ValueError):
    """Raised when the combination space exceeds the configured cap."""


def _minimize(sets: Iterable[Iterable[str]]) -> list[frozenset]:
    """De-duplicate field sets and drop strict supersets (inclusion-minimal)."""
    unique = {frozenset(s) for s in sets}
    minimal: list[frozenset] = []
    for fs in sorted(unique, key=lambda s: (len(s), tuple(sorted(s)))):
        if any(kept <= fs for kept in minimal):
            continue
        minimal.append(fs)
    return minimal


def combine_field_set_families(
    families: list[list[list[str]]],
    max_combinations: Optional[int] = None,
) -> list[frozenset]:
    """All inclusion-minimal global field sets covering every family.

    A global set *covers* a family when it is a superset of at least one
    family member.  The result is sorted by field count, then field names.
    """
    if max_combinations is None:
        max_combinations = MAX_BATCH_PLAN_COMBINATIONS
    accumulator: list[frozenset] = [frozenset()]
    combinations = 0
    for family in families:
        options = _minimize(family)
        step: set[frozenset] = set()
        for base in accumulator:
            for option in options:
                combinations += 1
                if combinations > max_combinations:
                    raise BatchPlanSearchError(
                        "batch data plan combination space exceeds "
                        f"{max_combinations} combinations; reduce the number "
                        "of unknown cases or raise MAX_BATCH_PLAN_COMBINATIONS"
                    )
                step.add(base | option)
        accumulator = _minimize(step)
        if not accumulator:
            break  # An undecidable case exists: no global plan can cover all.
    return sorted(accumulator, key=lambda s: (len(s), tuple(sorted(s))))


def batch_data_plans(
    policy: dict,
    cases: list[dict],
    max_combinations: Optional[int] = None,
) -> tuple[list[dict], list[dict]]:
    """Adjudicate every case and combine the unknown cases' field-set families.

    Returns ``(case_summaries, plans)``.  Summaries are sorted by ``case_id``;
    plans are sorted by field count then field names, and each plan's
    ``covers`` maps every unknown ``case_id`` to the per-case minimal sets
    the plan covers.
    """
    summaries: list[dict] = []
    unknown: list[tuple[str, list[list[str]]]] = []

    for case in cases:
        verdict = adjudicate(policy, case["facts"])
        summary = {
            "case_id": case["case_id"],
            "status": verdict["status"],
            "conclusion": verdict.get("conclusion"),
        }
        if verdict["status"] == "unknown":
            summary["missing_fields"] = verdict["missing_fields"]
            summary["minimal_missing_field_sets"] = verdict[
                "minimal_missing_field_sets"
            ]
            unknown.append(
                (case["case_id"], verdict["minimal_missing_field_sets"])
            )
        summaries.append(summary)

    summaries.sort(key=lambda summary: summary["case_id"])
    unknown.sort(key=lambda item: item[0])

    plans: list[dict] = []
    for field_set in combine_field_set_families(
        [family for _, family in unknown], max_combinations
    ):
        covers = {
            case_id: [list(s) for s in family if set(s) <= field_set]
            for case_id, family in unknown
        }
        plans.append({"fields": sorted(field_set), "covers": covers})
    return summaries, plans
