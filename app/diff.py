"""Cycle diffing between two adjacent report versions.

The battery state machine only *adds* samples, so between two committed
reports a physical cycle can keep its exact time span, be re-numbered,
change capacity, or participate in structural changes:

* exact span match (same started_at/ended_at):
    - identical capacities        -> unchanged (a different cycle_no is a
      renumbering only, which is NOT a cycle change)
    - different capacities        -> capacity_changed
* containment graph over the remaining cycles:
    - one old cycle spans several new ones  -> split
    - several old cycles span one new one   -> merge
    - one old <-> one new, different spans  -> capacity_changed
      (span_changed=True)
    - any other connected component         -> restructured (defensive;
      additive samples cannot produce m:n components in practice)
* still unmatched: old-only -> removed, new-only -> added

The module is intentionally free of database/FastAPI dependencies so the
classification rules can be unit tested directly.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence

EPS = 1e-12


def _span(c: Mapping[str, Any]) -> tuple[int, int]:
    return int(c["started_at"]), int(c["ended_at"])


def _same_capacity(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return (
        abs(float(a["charge_ah"]) - float(b["charge_ah"])) <= EPS
        and abs(float(a["discharge_ah"]) - float(b["discharge_ah"])) <= EPS
    )


def _contains(outer: Mapping[str, Any], inner: Mapping[str, Any]) -> bool:
    """True if the inner cycle span is inside the outer one (inclusive)."""
    o_start, o_end = _span(outer)
    i_start, i_end = _span(inner)
    return i_start >= o_start and i_end <= o_end


def _capacity_change(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict:
    return {
        "old": dict(old),
        "new": dict(new),
        "span_changed": _span(old) != _span(new),
        "charge_delta_ah": float(new["charge_ah"]) - float(old["charge_ah"]),
        "discharge_delta_ah": float(new["discharge_ah"]) - float(old["discharge_ah"]),
    }


def diff_cycles(
    old_cycles: Sequence[Mapping[str, Any]],
    new_cycles: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Classify how ``new_cycles`` evolved from ``old_cycles``.

    Returns a dict with keys: added, removed, split, merged,
    capacity_changed, unchanged, restructured, summary.
    """
    old_by_span = {_span(c): c for c in old_cycles}
    new_by_span = {_span(c): c for c in new_cycles}

    changes: Dict[str, List[Any]] = {
        "added": [],
        "removed": [],
        "split": [],
        "merged": [],
        "capacity_changed": [],
        "unchanged": [],
        "restructured": [],
    }
    renumber_only = 0

    matched_old: set[tuple[int, int]] = set()
    matched_new: set[tuple[int, int]] = set()

    # ------------------------------------------------------------------ #
    # 1. exact span matches
    # ------------------------------------------------------------------ #
    for span, new_c in new_by_span.items():
        old_c = old_by_span.get(span)
        if old_c is None:
            continue
        matched_old.add(span)
        matched_new.add(span)
        if _same_capacity(old_c, new_c):
            changes["unchanged"].append({"old": dict(old_c), "new": dict(new_c)})
            if old_c["cycle_no"] != new_c["cycle_no"]:
                renumber_only += 1
        else:
            changes["capacity_changed"].append(_capacity_change(old_c, new_c))

    old_left = [c for c in old_cycles if _span(c) not in matched_old]
    new_left = [c for c in new_cycles if _span(c) not in matched_new]

    # ------------------------------------------------------------------ #
    # 2. containment edges between the survivors
    # ------------------------------------------------------------------ #
    # adjacency lists keyed by ("o", index) / ("n", index)
    adj: dict[tuple[str, int], list[tuple[str, int]]] = {}
    for i, o in enumerate(old_left):
        for j, n in enumerate(new_left):
            if _contains(o, n) or _contains(n, o):
                adj.setdefault(("o", i), []).append(("n", j))
                adj.setdefault(("n", j), []).append(("o", i))

    seen: set[tuple[str, int]] = set()
    component_nodes: list[set[tuple[str, int]]] = []
    for node in list(adj.keys()):
        if node in seen:
            continue
        stack = [node]
        comp: set[tuple[str, int]] = set()
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            comp.add(cur)
            stack.extend(adj.get(cur, []))
        component_nodes.append(comp)

    grouped_old: set[int] = set()
    grouped_new: set[int] = set()
    for comp in component_nodes:
        o_idxs = sorted(i for side, i in comp if side == "o")
        n_idxs = sorted(i for side, i in comp if side == "n")
        grouped_old.update(o_idxs)
        grouped_new.update(n_idxs)
        o_members = [dict(old_left[i]) for i in o_idxs]
        n_members = [dict(new_left[j]) for j in n_idxs]
        if len(o_idxs) == 1 and len(n_idxs) == 1:
            old_c, new_c = o_members[0], n_members[0]
            if _same_capacity(old_c, new_c):
                changes["unchanged"].append({"old": old_c, "new": new_c})
                if old_c["cycle_no"] != new_c["cycle_no"]:
                    renumber_only += 1
            else:
                changes["capacity_changed"].append(_capacity_change(old_c, new_c))
        elif len(o_idxs) == 1 and len(n_idxs) > 1:
            changes["split"].append({"from": o_members[0], "into": n_members})
        elif len(o_idxs) > 1 and len(n_idxs) == 1:
            changes["merged"].append({"from": o_members, "into": n_members[0]})
        else:  # pragma: no cover - defensive, unreachable with additive data
            changes["restructured"].append(
                {"from": o_members, "into": n_members}
            )

    # ------------------------------------------------------------------ #
    # 3. everything still unmatched is a pure add / remove
    # ------------------------------------------------------------------ #
    for i, o in enumerate(old_left):
        if i not in grouped_old:
            changes["removed"].append(dict(o))
    for j, n in enumerate(new_left):
        if j not in grouped_new:
            changes["added"].append(dict(n))

    summary = {
        "old_total": len(old_cycles),
        "new_total": len(new_cycles),
        "added": len(changes["added"]),
        "removed": len(changes["removed"]),
        "split": len(changes["split"]),
        "merged": len(changes["merged"]),
        "capacity_changed": len(changes["capacity_changed"]),
        "unchanged": len(changes["unchanged"]),
        "renumber_only": renumber_only,
        "restructured": len(changes["restructured"]),
    }
    return {**changes, "summary": summary}
