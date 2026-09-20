"""Multi-source fact merging: priority tiers, conflicts, no backfilling.

A request submits several *named* fact sources, each carrying an integer
``priority``.  Merging is decided **per field**: among the sources that
provide the field, only the highest-priority tier is compared —

* every value in the top tier strictly equal (and not ``null``) -> the value
  is *adopted* into the merged facts;
* values disagree, or any top-tier value is ``null`` -> the field is
  *unresolved* and stays absent from the merged facts (the adjudication
  engine then treats it as unknown).

Lower-priority sources never backfill an unresolved field: once the top
tier cannot decide a field, no lower tier is consulted for it.  The merge
is a pure function of the source *set* — the order sources appear in the
request never affects the outcome, and every report list is deterministically
sorted.

The merged facts are adjudicated with the regular priority/three-valued
engine; the response pairs the full verdict with a per-field resolution
report (adopted/discarded sources and conflicting values).
"""
from __future__ import annotations

from typing import Any

from .engine import adjudicate
from .logic import strict_eq

# Why a top-priority tier could not decide a field.
REASON_CONFLICTING_VALUES = "conflicting_values"
REASON_NULL_VALUE = "null_value"
# Why a source was discarded for a field.
REASON_LOWER_PRIORITY = "lower_priority"


def merge_sources(sources: list[dict]) -> tuple[dict, list[dict]]:
    """Merge validated sources into ``(merged_facts, field_reports)``.

    ``field_reports`` holds one entry per field offered by any source,
    sorted by field name; every nested list is deterministically ordered,
    so the result does not depend on the order of ``sources``.
    """
    offerings: dict[str, list[tuple[int, str, Any]]] = {}
    for source in sources:
        for field_name, value in source["facts"].items():
            offerings.setdefault(field_name, []).append(
                (source["priority"], source["name"], value)
            )

    merged: dict[str, Any] = {}
    reports: list[dict] = []

    for field_name in sorted(offerings):
        entries = offerings[field_name]
        top_priority = max(priority for priority, _, _ in entries)
        top = sorted(
            (name, value)
            for priority, name, value in entries
            if priority == top_priority
        )
        discarded = [
            {
                "source": name,
                "priority": priority,
                "value": value,
                "reason": REASON_LOWER_PRIORITY,
            }
            for priority, name, value in sorted(
                (entry for entry in entries if entry[0] < top_priority),
                key=lambda entry: (-entry[0], entry[1]),
            )
        ]

        first = top[0][1]
        all_equal = all(strict_eq(first, value) for _, value in top[1:])

        if all_equal and first is not None:
            merged[field_name] = first
            reports.append(
                {
                    "field": field_name,
                    "status": "adopted",
                    "priority": top_priority,
                    "value": first,
                    "adopted_from": [name for name, _ in top],
                    "discarded": discarded,
                }
            )
        else:
            reports.append(
                {
                    "field": field_name,
                    "status": "unresolved",
                    "priority": top_priority,
                    "reason": (
                        REASON_CONFLICTING_VALUES
                        if not all_equal
                        else REASON_NULL_VALUE
                    ),
                    "conflicts": [
                        {"source": name, "value": value} for name, value in top
                    ],
                    "discarded": discarded,
                }
            )

    return merged, reports


def evaluate_sources(policy: dict, sources: list[dict]) -> dict:
    """Merge ``sources`` and adjudicate the merged facts against ``policy``.

    Returns the response payload: the deterministic source summary, the
    merged facts, the per-field resolution report, the unresolved field
    names and the full engine verdict.
    """
    merged_facts, field_reports = merge_sources(sources)
    verdict = adjudicate(policy, merged_facts)
    return {
        "source_count": len(sources),
        "sources": [
            {"name": source["name"], "priority": source["priority"]}
            for source in sorted(
                sources, key=lambda s: (-s["priority"], s["name"])
            )
        ],
        "merged_facts": merged_facts,
        "fields": field_reports,
        "unresolved_fields": [
            report["field"]
            for report in field_reports
            if report["status"] == "unresolved"
        ],
        "verdict": verdict,
    }
