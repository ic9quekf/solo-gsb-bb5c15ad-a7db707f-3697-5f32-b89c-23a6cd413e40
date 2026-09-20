"""Structural validation for submitted policies.

Validation is deliberately strict: unknown keys, wrong types or recursive
expressions deeper than :data:`MAX_EXPR_DEPTH` are rejected with a list of
human-readable errors (one error per detected problem where practical).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Optional

from .logic import COMPARISONS

MAX_EXPR_DEPTH = 64
MAX_RULES = 1000
MAX_PRIORITY = 10**9
MAX_ID_LENGTH = 200
MAX_CONCLUSION_LENGTH = 500
MAX_MAX_CHANGES = 100
MAX_BATCH_CASES = 1000
MAX_CASE_ID_LENGTH = 200
MAX_FLOW_NODES = 500
MAX_FLOW_ROUTES = 100
MAX_FACT_SOURCES = 1000
MAX_SOURCE_NAME_LENGTH = 200
FLOW_ROUTE_STATUSES = ("decided", "unknown", "conflict", "no_match")


class PolicyValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class SourceValidationError(ValueError):
    """Raised when a multi-source fact request body is malformed."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_policy(payload: Any) -> list[dict]:
    """Validate a policy payload. Returns the normalized ``rules`` list."""
    errors: list[str] = []

    if not isinstance(payload, dict):
        raise PolicyValidationError(["policy body must be an object"])

    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("rules: must be a non-empty array")
        raise PolicyValidationError(errors)
    if len(rules) > MAX_RULES:
        errors.append(f"rules: at most {MAX_RULES} rules are allowed")

    seen_ids: set[str] = set()
    normalized: list[dict] = []

    # Reserve every effective id (explicit or default) before validating so an
    # explicit id cannot shadow an auto-generated one.
    effective_ids = [
        rule.get("id", f"rule-{index + 1}")
        if isinstance(rule, dict) else None
        for index, rule in enumerate(rules)
    ]
    id_counts = Counter(i for i in effective_ids if isinstance(i, str))

    for index, rule in enumerate(rules):
        prefix = f"rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        rule_id = effective_ids[index]
        if not isinstance(rule_id, str) or not rule_id.strip():
            errors.append(f"{prefix}.id: must be a non-empty string")
        elif len(rule_id) > MAX_ID_LENGTH:
            errors.append(f"{prefix}.id: must be at most {MAX_ID_LENGTH} characters")
        elif id_counts[rule_id] > 1:
            errors.append(f"{prefix}.id: duplicate rule id {rule_id!r}")
        else:
            seen_ids.add(rule_id)

        priority = rule.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            errors.append(f"{prefix}.priority: must be an integer")
        elif not -MAX_PRIORITY <= priority <= MAX_PRIORITY:
            errors.append(
                f"{prefix}.priority: must be between -{MAX_PRIORITY} and "
                f"{MAX_PRIORITY}"
            )

        conclusion = rule.get("conclusion")
        if not isinstance(conclusion, str) or not conclusion.strip():
            errors.append(f"{prefix}.conclusion: must be a non-empty string")
        elif len(conclusion) > MAX_CONCLUSION_LENGTH:
            errors.append(
                f"{prefix}.conclusion: must be at most "
                f"{MAX_CONCLUSION_LENGTH} characters"
            )

        if "expr" not in rule:
            errors.append(f"{prefix}.expr: expression is required")
        else:
            errors.extend(_validate_expr(rule["expr"], f"{prefix}.expr", 1))

        normalized_rule = dict(rule)
        if "id" not in normalized_rule and isinstance(rule_id, str) and rule_id:
            normalized_rule["id"] = rule_id
        normalized.append(normalized_rule)

    if errors:
        raise PolicyValidationError(errors)
    return normalized


def _validate_expr(node: Any, path: str, depth: int) -> list[str]:
    errors: list[str] = []
    if depth > MAX_EXPR_DEPTH:
        return [f"{path}: expression nesting exceeds {MAX_EXPR_DEPTH} levels"]
    if not isinstance(node, dict):
        return [f"{path}: expression node must be an object"]

    keys = set(node)
    connective_keys = keys & {"all", "any"}
    if "not" in keys:
        if connective_keys or "field" in keys:
            errors.append(f"{path}: 'not' cannot combine with other operators")
        if set(node) != {"not"}:
            unknown = set(node) - {"not"}
            errors.append(f"{path}: unknown keys {sorted(unknown)}")
        if "not" in node:
            errors.extend(_validate_expr(node["not"], f"{path}.not", depth + 1))
        return errors

    if connective_keys:
        if len(connective_keys) > 1:
            errors.append(f"{path}: 'all' and 'any' are mutually exclusive")
        if "field" in keys:
            errors.append(f"{path}: connective node cannot contain 'field'")
        key = next(iter(connective_keys))
        children = node.get(key)
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.{key}: must be a non-empty array")
        else:
            for i, child in enumerate(children):
                errors.extend(
                    _validate_expr(child, f"{path}.{key}[{i}]", depth + 1)
                )
        unknown = keys - {key}
        if unknown:
            errors.append(f"{path}: unknown keys {sorted(unknown)}")
        return errors

    # Leaf comparison node.
    required = {"field", "op", "value"}
    missing = required - keys
    if missing:
        errors.append(f"{path}: comparison node missing keys {sorted(missing)}")
    unknown = keys - required
    if unknown:
        errors.append(f"{path}: unknown keys {sorted(unknown)}")
    if missing or unknown:
        return errors

    field_name = node["field"]
    if not isinstance(field_name, str) or not field_name:
        errors.append(f"{path}.field: must be a non-empty string")

    op = node["op"]
    if op not in COMPARISONS:
        errors.append(
            f"{path}.op: must be one of {sorted(COMPARISONS)}"
        )

    value = node["value"]
    if not _json_literal(value):
        errors.append(
            f"{path}.value: must be a JSON literal "
            "(null, bool, number, string, array or object of literals)"
        )
    return errors


def _json_literal(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_json_literal(v) for v in value)
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _json_literal(v) for k, v in value.items()
        )
    return False


def validate_counterfactual_request(payload: Any) -> tuple[dict, str, dict, int]:
    """Validate a counterfactual request body.

    Returns the normalized ``(facts, target_conclusion, candidates,
    max_changes)`` tuple; raises :class:`PolicyValidationError` with one error
    string per detected problem.
    """
    errors: list[str] = []

    if not isinstance(payload, dict):
        raise PolicyValidationError(["request body must be an object"])

    facts = payload.get("facts", {})
    if not isinstance(facts, dict) or not all(
        isinstance(k, str) for k in facts
    ):
        errors.append("facts: must be an object with string keys")

    target = payload.get("target_conclusion")
    if not isinstance(target, str) or not target.strip():
        errors.append("target_conclusion: must be a non-empty string")
    elif len(target) > MAX_CONCLUSION_LENGTH:
        errors.append(
            "target_conclusion: must be at most "
            f"{MAX_CONCLUSION_LENGTH} characters"
        )

    candidates = payload.get("candidates")
    normalized_candidates: dict[str, list] = {}
    if not isinstance(candidates, dict) or not candidates:
        errors.append("candidates: must be a non-empty object")
    else:
        for field_name, values in candidates.items():
            prefix = f"candidates.{field_name}"
            if not isinstance(field_name, str) or not field_name:
                errors.append(f"{prefix}: candidate field name must be a non-empty string")
                continue
            if not isinstance(values, list) or not values:
                errors.append(f"{prefix}: must be a non-empty array")
                continue
            normalized_candidates[field_name] = list(values)

    max_changes = payload.get("max_changes")
    if isinstance(max_changes, bool) or not isinstance(max_changes, int):
        errors.append("max_changes: must be a positive integer")
    elif not 1 <= max_changes <= MAX_MAX_CHANGES:
        errors.append(
            f"max_changes: must be between 1 and {MAX_MAX_CHANGES}"
        )

    if errors:
        raise PolicyValidationError(errors)

    return facts, target, normalized_candidates, max_changes


def validate_batch_data_plans_request(payload: Any) -> list[dict]:
    """Validate a batch data plans request body.

    Returns the normalized ``cases`` list (one ``{"case_id", "facts"}`` dict
    per case, ``facts`` defaulting to ``{}``); raises
    :class:`PolicyValidationError` with one error string per detected problem.
    """
    errors: list[str] = []

    if not isinstance(payload, dict):
        raise PolicyValidationError(["request body must be an object"])

    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise PolicyValidationError(["cases: must be a non-empty array"])
    if len(cases) > MAX_BATCH_CASES:
        errors.append(f"cases: at most {MAX_BATCH_CASES} cases are allowed")

    seen_ids: set[str] = set()
    normalized: list[dict] = []

    for index, case in enumerate(cases):
        prefix = f"cases[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"{prefix}.case_id: must be a non-empty string")
        elif len(case_id) > MAX_CASE_ID_LENGTH:
            errors.append(
                f"{prefix}.case_id: must be at most "
                f"{MAX_CASE_ID_LENGTH} characters"
            )
        elif case_id in seen_ids:
            errors.append(f"{prefix}.case_id: duplicate case id {case_id!r}")
        else:
            seen_ids.add(case_id)

        facts = case.get("facts", {})
        if not isinstance(facts, dict) or not all(
            isinstance(k, str) for k in facts
        ):
            errors.append(
                f"{prefix}.facts: must be an object with string keys"
            )
            facts = {}

        normalized.append({"case_id": case_id, "facts": facts})

    if errors:
        raise PolicyValidationError(errors)

    return normalized


def validate_replacement_preview_request(payload: Any) -> tuple[list[dict], list[dict]]:
    """Validate a replacement preview request body.

    The body combines a candidate policy (``rules``) with the batch-style
    ``cases`` array.  Returns the normalized ``(candidate_rules, cases)``
    tuple; raises :class:`PolicyValidationError` collecting the errors of
    both parts.
    """
    if not isinstance(payload, dict):
        raise PolicyValidationError(["request body must be an object"])

    errors: list[str] = []
    rules: list[dict] = []
    cases: list[dict] = []
    try:
        rules = validate_policy(payload)
    except PolicyValidationError as exc:
        errors.extend(exc.errors)
    try:
        cases = validate_batch_data_plans_request(payload)
    except PolicyValidationError as exc:
        errors.extend(exc.errors)
    if errors:
        raise PolicyValidationError(errors)
    return rules, cases


def validate_flow_request(
    payload: Any, policy_exists: Callable[[str], Any]
) -> tuple[str, list[dict], dict]:
    """Validate an adjudication-flow request body.

    ``policy_exists`` resolves a policy id to the stored record (or a falsy
    value); it is only consulted, never modified.  Returns the normalized
    ``(start, nodes, facts)`` tuple where each node is
    ``{"id", "policy_id", "routes"}`` and each route ``{"on", "next"}``
    plus ``"conclusion"`` for ``decided`` routes.

    Raises :class:`PolicyValidationError` with one error string per
    detected problem.  Cycle detection covers only the subgraph reachable
    from ``start``; cycles among unreachable nodes are not an error.
    """
    if not isinstance(payload, dict):
        raise PolicyValidationError(["request body must be an object"])

    errors: list[str] = []

    facts = payload.get("facts", {})
    if not isinstance(facts, dict) or not all(
        isinstance(k, str) for k in facts
    ):
        errors.append("facts: must be an object with string keys")
        facts = {}

    start = payload.get("start")
    if not isinstance(start, str) or not start.strip():
        errors.append("start: must be a non-empty string")
        start = None

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("nodes: must be a non-empty array")
        raw_nodes = []
    elif len(raw_nodes) > MAX_FLOW_NODES:
        errors.append(f"nodes: at most {MAX_FLOW_NODES} nodes are allowed")

    id_counts = Counter(
        node["id"]
        for node in raw_nodes
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    )
    declared_ids: set[str] = set(id_counts)
    nodes: list[dict] = []

    for index, node in enumerate(raw_nodes):
        prefix = f"nodes[{index}]"
        if not isinstance(node, dict):
            errors.append(f"{prefix}: must be an object")
            continue
        unknown_keys = set(node) - {"id", "policy_id", "routes"}
        if unknown_keys:
            errors.append(f"{prefix}: unknown keys {sorted(unknown_keys)}")

        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append(f"{prefix}.id: must be a non-empty string")
        elif len(node_id) > MAX_ID_LENGTH:
            errors.append(
                f"{prefix}.id: must be at most {MAX_ID_LENGTH} characters"
            )
        elif id_counts[node_id] > 1:
            errors.append(f"{prefix}.id: duplicate node id {node_id!r}")

        policy_id = node.get("policy_id")
        if not isinstance(policy_id, str) or not policy_id.strip():
            errors.append(f"{prefix}.policy_id: must be a non-empty string")
        elif not policy_exists(policy_id):
            errors.append(
                f"{prefix}.policy_id: unknown policy {policy_id!r}"
            )

        routes = _validate_flow_routes(node.get("routes", []), prefix, errors)
        nodes.append({"id": node_id, "policy_id": policy_id, "routes": routes})

    if declared_ids:
        if start is not None and start not in declared_ids:
            errors.append(
                f"start: no node with id {start!r} declared in nodes"
            )
        for index, node in enumerate(nodes):
            for r_index, route in enumerate(node["routes"]):
                nxt = route.get("next")
                if isinstance(nxt, str) and nxt not in declared_ids:
                    errors.append(
                        f"nodes[{index}].routes[{r_index}].next: no node "
                        f"with id {nxt!r} declared in nodes"
                    )

    if errors:
        raise PolicyValidationError(errors)

    adjacency = {
        node["id"]: [route["next"] for route in node["routes"]]
        for node in nodes
    }
    cycle = _find_reachable_cycle(start, adjacency)
    if cycle is not None:
        raise PolicyValidationError(
            [
                "nodes: cycle detected among nodes reachable from start "
                f"{start!r}: {' -> '.join(cycle)}"
            ]
        )

    return start, nodes, facts


def _validate_flow_routes(
    raw_routes: Any, prefix: str, errors: list[str]
) -> list[dict]:
    """Validate one node's ``routes`` array, appending to ``errors``.

    Returns the normalized routes in declaration order.
    """
    routes_path = f"{prefix}.routes"
    if not isinstance(raw_routes, list):
        errors.append(f"{routes_path}: must be an array")
        return []
    if len(raw_routes) > MAX_FLOW_ROUTES:
        errors.append(
            f"{routes_path}: at most {MAX_FLOW_ROUTES} routes are allowed"
        )

    routes: list[dict] = []
    seen_matches: set[tuple] = set()
    for index, route in enumerate(raw_routes):
        r_prefix = f"{routes_path}[{index}]"
        if not isinstance(route, dict):
            errors.append(f"{r_prefix}: must be an object")
            continue
        unknown_keys = set(route) - {"on", "conclusion", "next"}
        if unknown_keys:
            errors.append(f"{r_prefix}: unknown keys {sorted(unknown_keys)}")

        on = route.get("on")
        if on not in FLOW_ROUTE_STATUSES:
            errors.append(
                f"{r_prefix}.on: must be one of "
                f"{sorted(FLOW_ROUTE_STATUSES)}"
            )
            on = None

        conclusion = route.get("conclusion")
        if on == "decided":
            if not isinstance(conclusion, str) or not conclusion.strip():
                errors.append(
                    f"{r_prefix}.conclusion: 'decided' routes require a "
                    "non-empty string conclusion"
                )
            elif len(conclusion) > MAX_CONCLUSION_LENGTH:
                errors.append(
                    f"{r_prefix}.conclusion: must be at most "
                    f"{MAX_CONCLUSION_LENGTH} characters"
                )
        elif "conclusion" in route:
            errors.append(
                f"{r_prefix}.conclusion: only allowed on 'decided' routes"
            )

        nxt = route.get("next")
        if not isinstance(nxt, str) or not nxt.strip():
            errors.append(f"{r_prefix}.next: must be a non-empty string")
            nxt = None
        elif len(nxt) > MAX_ID_LENGTH:
            errors.append(
                f"{r_prefix}.next: must be at most {MAX_ID_LENGTH} characters"
            )

        if on is not None:
            match_key = (on, conclusion if on == "decided" else None)
            if match_key in seen_matches:
                errors.append(
                    f"{r_prefix}: duplicate route for the same verdict"
                )
            seen_matches.add(match_key)

        normalized: dict[str, Any] = {"on": on}
        if on == "decided" and isinstance(conclusion, str):
            normalized["conclusion"] = conclusion
        normalized["next"] = nxt
        routes.append(normalized)
    return routes


def _find_reachable_cycle(
    start: str, adjacency: dict[str, list[str]]
) -> Optional[list[str]]:
    """One cycle in the subgraph reachable from ``start``, else ``None``.

    Iterative DFS from ``start``; unreachable nodes are never visited, so
    cycles outside the reachable subgraph do not count.  The returned cycle
    is the node path with the repeated node closing the loop.
    """
    gray, black = 1, 2
    state = {start: gray}
    path = [start]
    stack = [(start, iter(adjacency.get(start, ())))]
    while stack:
        node, edges = stack[-1]
        for nxt in edges:
            nxt_state = state.get(nxt)
            if nxt_state == gray:
                return path[path.index(nxt):] + [nxt]
            if nxt_state is None:
                state[nxt] = gray
                path.append(nxt)
                stack.append((nxt, iter(adjacency.get(nxt, ()))))
                break
        else:
            state[node] = black
            stack.pop()
            path.pop()
    return None


def validate_evaluate_sources_request(payload: Any) -> list[dict]:
    """Validate an evaluate-sources request body.

    The body must be ``{"sources": [{"name", "priority", "facts"}, ...]}``
    with a non-empty array of uniquely named sources whose ``priority`` is an
    integer and whose ``facts`` is an object with string keys.  Returns the
    normalized sources list; raises :class:`SourceValidationError` with one
    error string per detected problem.
    """
    errors: list[str] = []

    if not isinstance(payload, dict):
        raise SourceValidationError(["request body must be an object"])

    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise SourceValidationError(["sources: must be a non-empty array"])
    if len(raw_sources) > MAX_FACT_SOURCES:
        errors.append(
            f"sources: at most {MAX_FACT_SOURCES} sources are allowed"
        )

    seen_names: set[str] = set()
    normalized: list[dict] = []

    for index, source in enumerate(raw_sources):
        prefix = f"sources[{index}]"
        if not isinstance(source, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        unknown_keys = set(source) - {"name", "priority", "facts"}
        if unknown_keys:
            errors.append(f"{prefix}: unknown keys {sorted(unknown_keys)}")

        name = source.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{prefix}.name: must be a non-empty string")
        elif len(name) > MAX_SOURCE_NAME_LENGTH:
            errors.append(
                f"{prefix}.name: must be at most "
                f"{MAX_SOURCE_NAME_LENGTH} characters"
            )
        elif name in seen_names:
            errors.append(f"{prefix}.name: duplicate source name {name!r}")
        else:
            seen_names.add(name)

        priority = source.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            errors.append(f"{prefix}.priority: must be an integer")
        elif not -MAX_PRIORITY <= priority <= MAX_PRIORITY:
            errors.append(
                f"{prefix}.priority: must be between -{MAX_PRIORITY} and "
                f"{MAX_PRIORITY}"
            )

        facts = source.get("facts", {})
        if not isinstance(facts, dict) or not all(
            isinstance(key, str) for key in facts
        ):
            errors.append(
                f"{prefix}.facts: must be an object with string keys"
            )
            facts = {}

        normalized.append(
            {"name": name, "priority": priority, "facts": facts}
        )

    if errors:
        raise SourceValidationError(errors)

    return normalized
