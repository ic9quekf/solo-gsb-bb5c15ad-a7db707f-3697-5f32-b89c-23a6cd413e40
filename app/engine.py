"""Policy adjudication engine: priority levels, conflicts, minimal gaps.

Adjudication order
------------------
Rules are grouped by ``priority``; levels are evaluated from the highest
priority to the lowest.  A level is decisive when every rule in it is known:

* at least one TRUE rule  -> the level fires; distinct conclusions among the
  TRUE rules are a ``conflict``, otherwise the unique conclusion is ``decided``;
* all rules FALSE          -> the level is skipped, evaluation continues below;
* one or more UNKNOWN      -> evaluation stops and returns ``unknown`` with the
  inclusion-minimal sets of missing fields whose provision makes *every rule of
  this level* decidable, together with a per-node derivation trace.

If no level fires and nothing is unknown, the result is ``no_match``.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from .logic import (
    FALSE,
    TRUE,
    UNKNOWN,
    Atom,
    AtomTable,
    Family,
    Term,
    clean_family,
    cross_families,
    evaluate_expr,
    term_is_feasible,
)


@dataclass
class RuleEval:
    rule: dict
    status: str
    t_family: Family
    f_family: Family
    trace: dict


def _evaluate_rule(rule: dict, facts: dict, atoms: AtomTable) -> RuleEval:
    result = evaluate_expr(rule["expr"], facts, atoms)
    return RuleEval(
        rule=rule,
        status=result.status,
        t_family=result.t_family,
        f_family=result.f_family,
        trace=result.trace,
    )


def _rule_decided_family(evals: list[RuleEval]) -> Family:
    """Signed DNF of assignments under which *every* rule becomes known."""
    family: Family = (frozenset(),)
    for ev in evals:
        if ev.status == TRUE or ev.status == FALSE:
            continue  # already known: neutral element of the cross-product
        # Unknown rule is decided when it becomes TRUE or becomes FALSE.
        options = clean_family(list(ev.t_family) + list(ev.f_family))
        family = cross_families(family, options)
    return family


def _to_field_sets(
    family: Family, atoms_by_id: dict[int, Atom]
) -> list[list[str]]:
    """Drop impossible terms, project signed atoms to fields and minimize."""
    feasible = [t for t in family if term_is_feasible(t, atoms_by_id)]
    field_sets = {
        frozenset({atoms_by_id[atom_id].field for atom_id, _ in term})
        for term in feasible
    }
    minimal: list[frozenset[str]] = []
    for fs in sorted(field_sets, key=lambda s: (len(s), tuple(sorted(s)))):
        if not any(kept <= fs for kept in minimal):
            minimal.append(fs)
    return [sorted(fs) for fs in minimal]


def adjudicate(policy: dict, facts: dict) -> dict:
    """Evaluate ``facts`` against a validated policy; returns the verdict dict."""
    rules = policy["rules"]

    levels: "OrderedDict[int, list[dict]]" = OrderedDict()
    for rule in sorted(rules, key=lambda r: -r["priority"]):
        levels.setdefault(rule["priority"], []).append(rule)

    skipped_levels: list[int] = []
    level_reports: list[dict] = []

    for priority, level_rules in levels.items():
        atoms = AtomTable()
        evals = [_evaluate_rule(rule, facts, atoms) for rule in level_rules]

        rule_traces = [
            {
                "rule_id": ev.rule.get("id"),
                "priority": ev.rule["priority"],
                "conclusion": ev.rule["conclusion"],
                "status": ev.status,
                "expr": ev.trace,
            }
            for ev in evals
        ]

        statuses = [ev.status for ev in evals]
        true_evals = [ev for ev in evals if ev.status == TRUE]

        if UNKNOWN in statuses:
            family = _rule_decided_family(evals)
            atoms_by_id = {atom.id: atom for atom in atoms.atoms}
            minimal_sets = _to_field_sets(family, atoms_by_id)
            missing_fields = sorted(
                {f for fs in minimal_sets for f in fs}
            )
            pending_conclusions = sorted(
                {ev.rule["conclusion"] for ev in evals if ev.status != FALSE}
            )
            report = {
                "priority": priority,
                "state": "unknown",
                "rules": rule_traces,
                "true_rule_ids": [ev.rule.get("id") for ev in true_evals],
                "pending_conclusions": pending_conclusions,
                "atoms": [
                    {
                        "id": atom.id,
                        "field": atom.field,
                        "op": atom.op,
                        "value": atom.value,
                    }
                    for atom in atoms.atoms
                ],
                "minimal_missing_field_sets": minimal_sets,
                "missing_fields": missing_fields,
            }
            level_reports.append(report)
            return {
                "status": UNKNOWN,
                "decided_priority": priority,
                "conclusion": None,
                "reason": "undecidable_level",
                "message": (
                    f"Level {priority} contains unknown rules; supply the "
                    "fields in any minimal_missing_field_sets entry to make "
                    "the level decidable."
                ),
                "skipped_priorities": skipped_levels,
                "missing_fields": missing_fields,
                "minimal_missing_field_sets": minimal_sets,
                "pending_conclusions": pending_conclusions,
                "level_trace": report,
                "levels_trace": level_reports,
            }

        # Fully known level.
        if true_evals:
            conclusions = sorted({ev.rule["conclusion"] for ev in true_evals})
            true_rule_ids = [ev.rule.get("id") for ev in true_evals]
            if len(conclusions) > 1:
                state = "conflict"
                verdict = {
                    "status": "conflict",
                    "decided_priority": priority,
                    "conclusion": None,
                    "reason": "opposite_conclusions",
                    "conflicting_conclusions": conclusions,
                }
            else:
                state = "fired"
                verdict = {
                    "status": "decided",
                    "decided_priority": priority,
                    "conclusion": conclusions[0],
                    "reason": "unique_conclusion",
                }
            report = {
                "priority": priority,
                "state": state,
                "rules": rule_traces,
                "true_rule_ids": true_rule_ids,
                "conclusions": conclusions,
            }
            level_reports.append(report)
            verdict.update(
                {
                    "true_rule_ids": true_rule_ids,
                    "skipped_priorities": skipped_levels,
                    "levels_trace": level_reports,
                    "level_trace": report,
                }
            )
            return verdict

        # All FALSE: level skipped, descend to the next priority.
        level_reports.append(
            {
                "priority": priority,
                "state": "skipped_all_false",
                "rules": rule_traces,
                "true_rule_ids": [],
            }
        )
        skipped_levels.append(priority)

    return {
        "status": "no_match",
        "decided_priority": None,
        "conclusion": None,
        "reason": "all_levels_false",
        "message": "No rule matched the supplied facts.",
        "skipped_priorities": skipped_levels,
        "levels_trace": level_reports,
        "level_trace": None,
    }
