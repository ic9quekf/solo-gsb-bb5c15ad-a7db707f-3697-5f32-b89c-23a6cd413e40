"""Priority-based merging of facts arriving from multiple named sources.

Each source carries a name, an integer ``priority`` (larger wins) and a
``facts`` object.  Fields are merged **per field** using only the sources that
provide the field at its *highest* contributing priority:

* every highest-priority provider carries the same non-null value
  -> the field is ``resolved`` with that value, and all agreeing sources are
  reported as adopted;
* the values differ (strict, JSON-style equality; ``True`` is not ``1``)
  -> the field is ``unresolved`` (``conflicting_values``) and the conflicting
     values are reported;
* a highest-priority value is ``null``
  -> the field is ``unresolved`` (``null_value``).

Lower-priority providers are always *discarded* for the field
(``lower_priority``); in particular they never backfill an unresolved field.
Every output ordering is independent of the request order of the sources.
"""
from __future__ import annotations

import json
from typing import Any

from .logic import _strict_eq


def _distinct(values: list[Any]) -> list[Any]:
    """Strict-equality de-duplication, preserving first occurrence."""
    unique: list[Any] = []
    for value in values:
        if not any(_strict_eq(value, kept) for kept in unique):
            unique.append(value)
    return unique


def _value_key(value: Any) -> tuple[int, str]:
    """Deterministic, type-aware ordering key for JSON literals."""
    if value is None:
        rank = 0
    elif isinstance(value, bool):
        rank = 1
    elif isinstance(value, (int, float)):
        rank = 2
    elif isinstance(value, str):
        rank = 3
    elif isinstance(value, list):
        rank = 4
    else:
        rank = 5
    return rank, json.dumps(value, sort_keys=True, ensure_ascii=False)


def merge_fact_sources(sources: list[dict]) -> dict:
    """Merge normalized fact sources.

    ``sources`` is a list of ``{"name", "priority", "facts"}`` dicts (already
    validated).  Returns ``{"facts", "fields", "unresolved_fields"}`` where
    ``facts`` holds only the resolved values and ``fields`` maps each
    contributed field to its adopted/discarded source report.  The result is
    independent of the order in which the sources were submitted.
    """
    # Deterministic processing order: highest priority first, then name.
    ordered = sorted(
        sources, key=lambda source: (-source["priority"], source["name"])
    )

    # field -> [(priority, source_name, value), ...] in processing order
    providers: dict[str, list[tuple[int, str, Any]]] = {}
    for source in ordered:
        for field, value in source["facts"].items():
            providers.setdefault(field, []).append(
                (source["priority"], source["name"], value)
            )

    merged_facts: dict[str, Any] = {}
    field_reports: dict[str, dict] = {}

    for field in sorted(providers):
        entries = providers[field]
        top_priority = entries[0][0]
        top = [entry for entry in entries if entry[0] == top_priority]
        lower = [entry for entry in entries if entry[0] < top_priority]

        discarded = [
            {
                "source": name,
                "priority": priority,
                "value": value,
                "reason": "lower_priority",
            }
            for priority, name, value in lower
        ]

        top_values = sorted(
            _distinct([value for _, _, value in top]), key=_value_key
        )

        if len(top_values) == 1 and top_values[0] is not None:
            value = top_values[0]
            merged_facts[field] = value
            field_reports[field] = {
                "status": "resolved",
                "value": value,
                "adopted_sources": sorted(name for _, name, _ in top),
                "discarded_sources": discarded,
            }
            continue

        if len(top_values) > 1:
            reason = "conflicting_values"
        else:
            reason = "null_value"

        rejected = [
            {
                "source": name,
                "priority": priority,
                "value": value,
                "reason": "null_value" if value is None else reason,
            }
            for priority, name, value in top
        ]
        rejected.extend(discarded)
        rejected.sort(key=lambda item: (-item["priority"], item["source"]))

        field_reports[field] = {
            "status": "unresolved",
            "reason": reason,
            "adopted_sources": [],
            "discarded_sources": rejected,
            "conflicting_values": top_values,
        }

    unresolved_fields = sorted(
        field
        for field, report in field_reports.items()
        if report["status"] == "unresolved"
    )
    return {
        "facts": merged_facts,
        "fields": field_reports,
        "unresolved_fields": unresolved_fields,
    }
