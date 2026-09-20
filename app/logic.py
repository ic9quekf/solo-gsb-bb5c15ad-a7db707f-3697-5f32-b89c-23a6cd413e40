"""Three-valued expression evaluation and minimal missing-field analysis.

Semantics
---------
Each expression evaluates to one of three values: TRUE / FALSE / UNKNOWN.

* A comparison whose field is absent (or explicitly ``null``) from the facts is
  UNKNOWN; a comparison between present values is always TRUE or FALSE.
* ``all`` (AND), ``any`` (OR) and ``not`` use Kleene strong three-valued logic.

For every expression we additionally compute two "deciding families":

* ``t_family`` — inclusion-minimal sets of currently-missing comparison atoms
  that, if supplied in a way making those comparisons TRUE, force the whole
  expression to TRUE regardless of every other missing atom.
* ``f_family`` — the same for FALSE.

A family term is a frozenset of *signed atoms*: ``(atom_id, required_bool)``.
Families are kept in signed-minimal-DNF form and are exact for the propositional
structure of the rule.  Before they are reported at the policy level, every term
is checked for per-field concrete feasibility (e.g.  a field cannot be both
``> 10`` and ``< 5``), which also rules out impossible cross-comparison
constraints on the same field.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

TRUE = "true"
FALSE = "false"
UNKNOWN = "unknown"

COMPARISONS = ("eq", "ne", "gt", "ge", "lt", "le")
CONNECTIVES = ("all", "any", "not")

# Safety cap on cross-product expansion of deciding families.
MAX_FAMILY_TERMS = 20000


# --------------------------------------------------------------------------- #
# Signed minimal-DNF families
# --------------------------------------------------------------------------- #

Term = frozenset[tuple[int, bool]]
Family = tuple[Term, ...]


def clean_family(terms: Any) -> Family:
    """Remove contradictory, duplicate and subsumed terms (inclusion-minimal)."""
    unique: set[Term] = set()
    for term in terms:
        fs = frozenset(term)
        atoms: dict[int, bool] = {}
        contradiction = False
        for atom, required in fs:
            if atom in atoms and atoms[atom] != required:
                contradiction = True
                break
            atoms[atom] = required
        if not contradiction:
            unique.add(fs)

    terms_sorted = sorted(unique, key=lambda t: (len(t), tuple(sorted(t))))
    minimal: list[Term] = []
    for term in terms_sorted:
        if any(kept <= term for kept in minimal):
            continue
        minimal.append(term)
    return tuple(minimal)


def cross_families(left: Family, right: Family) -> Family:
    """Distribute AND over two DNF families: (L1∨L2) ∧ (R1∨R2)."""
    out: set[Term] = set()
    for lterm in left:
        for rterm in right:
            merged = set(lterm)
            contradiction = False
            for atom, required in rterm:
                if atom in merged:
                    prev = next(
                        req for atm, req in merged if atm == atom
                    )
                    if prev != required:
                        contradiction = True
                        break
                else:
                    merged.add((atom, required))
            if not contradiction:
                out.add(frozenset(merged))
    result = clean_family(out)
    if len(result) > MAX_FAMILY_TERMS:
        # Keep the smallest terms: they are the most useful for callers and the
        # cap only guards pathological, deeply duplicated policies.
        result = tuple(sorted(result, key=len)[:MAX_FAMILY_TERMS])
    return result


# --------------------------------------------------------------------------- #
# Atoms (leaf comparisons against missing fields)
# --------------------------------------------------------------------------- #

@dataclass
class Atom:
    id: int
    field: str
    op: str
    value: Any


class AtomTable:
    """Assigns a stable id to every distinct missing-field comparison in a level."""

    def __init__(self) -> None:
        self._by_key: dict[tuple, int] = {}
        self.atoms: list[Atom] = []

    def intern(self, field_name: str, op: str, value: Any) -> int:
        key = (field_name, op, _hashable(value))
        if key not in self._by_key:
            atom_id = len(self.atoms)
            self._by_key[key] = atom_id
            self.atoms.append(Atom(atom_id, field_name, op, value))
        return self._by_key[key]


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(v) for v in value)
    return value


# --------------------------------------------------------------------------- #
# Comparison semantics (strict, JSON-style typing)
# --------------------------------------------------------------------------- #

def _same_kind(a: Any, b: Any) -> bool:
    """Whether two values share a comparison kind.

    Booleans are deliberately *not* numbers (``True`` shall not equal ``1``).
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool)
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        return isinstance(b, (int, float)) and not isinstance(b, bool)
    return type(a) is type(b)


def compare_values(left: Any, op: str, right: Any) -> tuple[bool, Optional[str]]:
    """Evaluate ``left <op> right`` for two present values.

    Returns ``(result, reason)`` where ``reason`` is set when the comparison is
    FALSE solely because the types are not orderable/comparable.
    """
    if op == "eq":
        return (_strict_eq(left, right), None)
    if op == "ne":
        return (not _strict_eq(left, right), None)

    if not _same_kind(left, right):
        return (False, "type_mismatch")
    if not isinstance(left, (int, float, str, bool)):
        return (False, "not_orderable")
    try:
        if op == "gt":
            return (left > right, None)
        if op == "ge":
            return (left >= right, None)
        if op == "lt":
            return (left < right, None)
        if op == "le":
            return (left <= right, None)
    except TypeError:
        return (False, "not_orderable")
    raise ValueError(f"unknown comparison operator: {op}")


def _strict_eq(left: Any, right: Any) -> bool:
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        return (
            isinstance(right, (int, float))
            and not isinstance(right, bool)
            and left == right
        )
    if isinstance(left, list):
        return (
            isinstance(right, list)
            and len(left) == len(right)
            and all(_strict_eq(a, b) for a, b in zip(left, right))
        )
    if isinstance(left, dict):
        if not isinstance(right, dict) or set(left) != set(right):
            return False
        return all(_strict_eq(left[k], right[k]) for k in left)
    return left == right and type(left) is type(right)


# --------------------------------------------------------------------------- #
# Expression evaluation
# --------------------------------------------------------------------------- #

@dataclass
class ExprResult:
    status: str
    t_family: Family = ()
    f_family: Family = ()
    trace: dict = field(default_factory=dict)


def _present(value: Any) -> bool:
    """A fact is present when the key exists and is not null."""
    return value is not None


def evaluate_expr(node: dict, facts: dict, atoms: AtomTable) -> ExprResult:
    """Evaluate one expression node under Kleene three-valued logic."""
    if "all" in node:
        children = [evaluate_expr(c, facts, atoms) for c in node["all"]]
        statuses = [c.status for c in children]
        if FALSE in statuses:
            return ExprResult(
                FALSE,
                (),
                (),
                {"op": "all", "status": FALSE,
                 "children": [c.trace for c in children]},
            )
        if UNKNOWN in statuses:
            # TRUE  requires every child TRUE:  AND of t-families.
            t_family: Family = (frozenset(),)
            for child in children:
                t_family = cross_families(
                    t_family, child.t_family or (frozenset(),)
                )
            # FALSE if at least one child FALSE: OR of f-families
            # (decided-FALSE children contribute the empty disjunct).
            f_terms: list[Term] = []
            for child in children:
                if child.status == FALSE:
                    f_terms.append(frozenset())
                else:
                    f_terms.extend(child.f_family)
            return ExprResult(
                UNKNOWN,
                t_family,
                clean_family(f_terms),
                {"op": "all", "status": UNKNOWN,
                 "children": [c.trace for c in children]},
            )
        return ExprResult(
            TRUE,
            (),
            (),
            {"op": "all", "status": TRUE,
             "children": [c.trace for c in children]},
        )

    if "any" in node:
        children = [evaluate_expr(c, facts, atoms) for c in node["any"]]
        statuses = [c.status for c in children]
        if TRUE in statuses:
            return ExprResult(
                TRUE,
                (),
                (),
                {"op": "any", "status": TRUE,
                 "children": [c.trace for c in children]},
            )
        if UNKNOWN in statuses:
            # FALSE requires every child FALSE: AND of f-families.
            f_family: Family = (frozenset(),)
            for child in children:
                f_family = cross_families(
                    f_family, child.f_family or (frozenset(),)
                )
            # TRUE if at least one child TRUE: OR of t-families
            # (decided-TRUE children contribute the empty disjunct).
            t_terms: list[Term] = []
            for child in children:
                if child.status == TRUE:
                    t_terms.append(frozenset())
                else:
                    t_terms.extend(child.t_family)
            return ExprResult(
                UNKNOWN,
                clean_family(t_terms),
                f_family,
                {"op": "any", "status": UNKNOWN,
                 "children": [c.trace for c in children]},
            )
        return ExprResult(
            FALSE,
            (),
            (),
            {"op": "any", "status": FALSE,
             "children": [c.trace for c in children]},
        )

    if "not" in node:
        child = evaluate_expr(node["not"], facts, atoms)
        if child.status == TRUE:
            status = FALSE
            return ExprResult(
                FALSE, (), (),
                {"op": "not", "status": FALSE, "child": child.trace},
            )
        if child.status == FALSE:
            return ExprResult(
                TRUE, (), (),
                {"op": "not", "status": TRUE, "child": child.trace},
            )
        # Unknown: truth conditions of NOT x are the falsity conditions of x
        # (the same concrete atom assignment, status labels swapped).
        return ExprResult(
            UNKNOWN,
            child.f_family,
            child.t_family,
            {"op": "not", "status": UNKNOWN, "child": child.trace},
        )

    # Leaf comparison: {"field": ..., "op": ..., "value": ...}
    field_name = node["field"]
    op = node["op"]
    expected = node["value"]
    trace: dict = {
        "op": op,
        "field": field_name,
        "value": expected,
    }
    if field_name not in facts or not _present(facts.get(field_name)):
        atom_id = atoms.intern(field_name, op, expected)
        trace.update({
            "status": UNKNOWN,
            "atom": atom_id,
            "reason": "missing_field",
        })
        return ExprResult(
            UNKNOWN,
            (frozenset({(atom_id, True)}),),
            (frozenset({(atom_id, False)}),),
            trace,
        )

    actual = facts[field_name]
    result, reason = compare_values(actual, op, expected)
    trace.update(
        {"status": TRUE if result else FALSE, "actual": actual}
    )
    if reason:
        trace["reason"] = reason
    return ExprResult(TRUE if result else FALSE, (), (), trace)


# --------------------------------------------------------------------------- #
# Concrete feasibility of signed constraints on one field
# --------------------------------------------------------------------------- #

def _signed_constraints(term: Term, atoms_by_id: dict[int, Atom]
                        ) -> dict[str, list[tuple[str, Any, bool]]]:
    """Group a term's signed literals per field: (op, literal, required_bool)."""
    grouped: dict[str, list[tuple[str, Any, bool]]] = {}
    for atom_id, required in term:
        atom = atoms_by_id[atom_id]
        grouped.setdefault(atom.field, []).append(
            (atom.op, atom.value, required)
        )
    return grouped


def term_is_feasible(term: Term, atoms_by_id: dict[int, Atom]) -> bool:
    """Whether some concrete fact assignment realizes every literal in ``term``.

    Each literal says comparison ``field op value`` must be TRUE/FALSE.
    Constraints on different fields are independent, so feasibility is checked
    one field at a time.
    """
    for field_name, constraints in _signed_constraints(term, atoms_by_id).items():
        if not _field_feasible(constraints):
            return False
    return True


def _field_feasible(constraints: list[tuple[str, Any, bool]]) -> bool:
    eq_values: set[Any] = set()
    ne_values: set[Any] = set()
    # Ordering bounds carry the kind tag when the constraint can only hold for
    # one kind; tagged bounds also force x to be of that kind.
    lower: list[tuple[Any, str]] = []   # (bound, "ge" | "gt")
    upper: list[tuple[Any, str]] = []   # (bound, "le" | "lt")
    kind_tags: set[str] = set()

    def kind_of(v: Any) -> Optional[str]:
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, (int, float)):
            return "num"
        if isinstance(v, str):
            return "str"
        if v is None:
            return "null"
        return None

    for op, value, required in constraints:
        # Normalize to the constraint that must hold on x.
        if not required:
            if op == "eq":
                op, required = "ne", True
            elif op == "ne":
                op, required = "eq", True
            elif op in ("gt", "lt"):
                # Strict ordering of mismatched kinds is false anyway; only
                # same-kind inversions remain.
                op = {"gt": "le", "lt": "ge"}[op]
            elif op in ("ge", "le"):
                op = {"ge": "lt", "le": "gt"}[op]

        if op == "eq":
            eq_values.add(value)
            k = kind_of(value)
            if k is not None:
                kind_tags.add(k)
        elif op == "ne":
            ne_values.add(value)
        elif op == "gt":
            lower.append((value, "gt"))
            k = kind_of(value)
            if k is not None:
                kind_tags.add(k)
        elif op == "ge":
            lower.append((value, "ge"))
            k = kind_of(value)
            if k is not None:
                kind_tags.add(k)
        elif op == "lt":
            upper.append((value, "lt"))
            k = kind_of(value)
            if k is not None:
                kind_tags.add(k)
        elif op == "le":
            upper.append((value, "le"))
            k = kind_of(value)
            if k is not None:
                kind_tags.add(k)

    # At most one concrete kind can satisfy all positive constraints.
    if len(kind_tags) > 1:
        return False

    # Multiple equality values must coincide.
    if len({_hashable(v) for v in eq_values}) > 1:
        return False

    # With a fixed candidate, check all constraints directly.
    if eq_values:
        candidate = next(iter(eq_values))
        return all(
            compare_values(candidate, op, value)[0] is required
            for op, value, required in constraints
        )

    kind = next(iter(kind_tags), None)

    if kind in ("bool", "null"):
        domain = (True, False) if kind == "bool" else (None,)
        # A ne-only constraint with no tag over bool/null is trivially fine.
        for candidate in domain:
            if all(
                compare_values(candidate, op, value)[0] is required
                for op, value, required in constraints
            ):
                return True
        return False

    if kind == "num":
        return _bounds_feasible(lower, upper, ne_values, numeric=True)
    if kind == "str":
        return _bounds_feasible(lower, upper, ne_values, numeric=False)

    # No typed positive constraint: an unrelated object/value satisfies pure
    # ne / ordering-false requirements.
    return True


def _bounds_feasible(
    lower: list[tuple[Any, str]],
    upper: list[tuple[Any, str]],
    exclude: set[Any],
    numeric: bool,
) -> bool:
    lo = max((b for b, _ in lower), default=None)
    hi = min((b for b, _ in upper), default=None)
    lo_strict = any(b == lo and tag == "gt" for b, tag in lower)
    hi_strict = any(b == hi and tag == "lt" for b, tag in upper)

    if lo is not None and hi is not None:
        if lo > hi or (lo == hi and (lo_strict or hi_strict)):
            return False

    candidates: list[Any] = []
    if numeric:
        # Sample the interval densely enough: an interval minus finitely many
        # excluded points is feasible unless the interval is empty. JSON
        # numbers form a dense domain, so fraction-based witnesses always exist
        # up to many simultaneous exclusions.
        offsets = (0, 1, -1) + tuple(i / 8 for i in range(9))
        if lo is not None and hi is not None:
            span = hi - lo
            candidates.extend(lo + span * f for f in offsets)
            candidates.extend(hi + span * (f - 1) for f in offsets)
        elif lo is not None:
            candidates.extend(lo + d for d in offsets)
        elif hi is not None:
            candidates.extend(hi - d for d in offsets)
        else:
            candidates.append(0)
    else:
        if lo is not None:
            candidates.extend([lo, lo + "\x00"])
        if hi is not None:
            candidates.extend([hi, hi[:-1] if hi else ""])
        if not candidates:
            candidates.append("")

    def ok(candidate: Any) -> bool:
        if candidate in exclude:
            return False
        for bound, tag in lower:
            if tag == "ge" and not candidate >= bound:
                return False
            if tag == "gt" and not candidate > bound:
                return False
        for bound, tag in upper:
            if tag == "le" and not candidate <= bound:
                return False
            if tag == "lt" and not candidate < bound:
                return False
        return True

    return any(ok(c) for c in candidates)
