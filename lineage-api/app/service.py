"""Lineage registration and recursive impact analysis."""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict

from . import db
from .exceptions import (
    ConflictLineageError,
    CycleDetectedError,
    InferenceScopeExceededError,
    NoConsistentSolutionError,
    NotFoundLineageError,
    ValidationLineageError,
)

# Graph traversal may be deep for large lineage graphs.
_MAX_RECURSION_DEPTH = max(10_000, sys.getrecursionlimit())

# Upstream source fields beyond this bound make exhaustive enumeration
# infeasible; the attribution request is rejected instead of guessing.
_MAX_RELEVANT_SOURCES = 16


# --------------------------------------------------------------------------- #
# Graph loading
# --------------------------------------------------------------------------- #

def load_graph(conn: sqlite3.Connection) -> tuple[
    dict[str, str], dict[str, list[list[str]]]
]:
    """Load every node and its candidate input groups from one snapshot."""
    kinds: dict[str, str] = {
        row["name"]: row["kind"] for row in conn.execute("SELECT name, kind FROM nodes")
    }
    groups: dict[str, list[list[str]]] = defaultdict(list)

    group_rows = conn.execute(
        "SELECT id, node_name FROM candidate_groups ORDER BY node_name, position"
    ).fetchall()
    id_to_node = {row["id"]: row["node_name"] for row in group_rows}

    if id_to_node:
        placeholders = ",".join("?" for _ in id_to_node)
        input_rows = conn.execute(
            f"SELECT group_id, input_name FROM group_inputs "
            f"WHERE group_id IN ({placeholders}) ORDER BY group_id, position",
            tuple(id_to_node.keys()),
        ).fetchall()
        inputs_by_group: dict[int, list[str]] = defaultdict(list)
        for row in input_rows:
            inputs_by_group[row["group_id"]].append(row["input_name"])
        for gid, node_name in id_to_node.items():
            groups[node_name].append(inputs_by_group.get(gid, []))

    return kinds, dict(groups)


def adjacency(
    groups: dict[str, list[list[str]]]
) -> dict[str, list[str]]:
    """Flatten candidate groups into node -> unique downstream inputs."""
    edges: dict[str, list[str]] = {}
    for node, node_groups in groups.items():
        seen: set[str] = set()
        ordered: list[str] = []
        for group in node_groups:
            for inp in group:
                if inp not in seen:
                    seen.add(inp)
                    ordered.append(inp)
        edges[node] = ordered
    return edges


# --------------------------------------------------------------------------- #
# Cycle detection (whole graph, DFS coloring)
# --------------------------------------------------------------------------- #

def find_cycle(
    kinds: dict[str, str], groups: dict[str, list[list[str]]]
) -> list[str] | None:
    """Return one cycle path (node repeated at both ends), or None."""
    edges = adjacency(groups)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {name: WHITE for name in kinds}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for nxt in edges.get(node, []):
            if color[nxt] == GRAY:
                idx = stack.index(nxt)
                return stack[idx:] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    sys.setrecursionlimit(_MAX_RECURSION_DEPTH)
    for name in sorted(kinds):
        if color[name] == WHITE:
            cycle = visit(name)
            if cycle is not None:
                return cycle
    return None


# --------------------------------------------------------------------------- #
# Batch registration (all-or-nothing)
# --------------------------------------------------------------------------- #

def register_batch(
    payload_nodes: list, db_path: str | None = None
) -> dict:
    """Validate and upsert a batch; any failure leaves the graph untouched."""
    # ---- pure payload validation (no lock held yet) ----
    seen_names: set[str] = set()
    duplicates: list[str] = []
    sources_with_inputs: list[str] = []
    empty_group_nodes: list[str] = []
    intra_group_dupes: dict[str, list[list[int]]] = {}

    for node in payload_nodes:
        if node.name in seen_names:
            duplicates.append(node.name)
        seen_names.add(node.name)

        if node.kind == "source" and node.candidate_groups:
            sources_with_inputs.append(node.name)
        if node.kind != "source" and not node.candidate_groups:
            empty_group_nodes.append(node.name)
        for i, group in enumerate(node.candidate_groups):
            if not group:
                empty_group_nodes.append(node.name)
            local_dupes = sorted(
                {inp for inp in group if group.count(inp) > 1}
            )
            if local_dupes:
                intra_group_dupes.setdefault(node.name, []).append(
                    [i, *local_dupes]
                )

    problems: dict[str, list] = {}
    if duplicates:
        problems["duplicate_names"] = sorted(set(duplicates))
    if sources_with_inputs:
        problems["source_nodes_with_inputs"] = sorted(set(sources_with_inputs))
    if empty_group_nodes:
        problems["missing_candidate_groups"] = sorted(set(empty_group_nodes))
    if intra_group_dupes:
        problems["duplicate_inputs_within_group"] = intra_group_dupes
    if problems:
        raise ValidationLineageError(
            "请求数据不合法，整批未登记", problems
        )

    with db.immediate_write(db_path) as conn:
        existing_kinds: dict[str, str] = {
            row["name"]: row["kind"]
            for row in conn.execute("SELECT name, kind FROM nodes")
        }

        # ---- node kind conflicts ----
        conflicts = sorted(
            node.name
            for node in payload_nodes
            if node.name in existing_kinds
            and existing_kinds[node.name] != node.kind
        )
        if conflicts:
            raise ConflictLineageError(
                "节点已存在且类型不同，整批未生效",
                {"kind_conflicts": conflicts},
            )

        # ---- referential integrity against post-batch node set ----
        final_nodes = dict(existing_kinds)
        for node in payload_nodes:
            final_nodes[node.name] = node.kind
        unknown: dict[str, list[str]] = {}
        for node in payload_nodes:
            missing = sorted(
                {
                    inp
                    for group in node.candidate_groups
                    for inp in group
                    if inp not in final_nodes
                }
            )
            if missing:
                unknown[node.name] = missing
        if unknown:
            raise ValidationLineageError(
                "候选输入引用了未登记的字段，整批未生效",
                {"unknown_inputs": unknown},
            )

        # ---- upsert phase 1: all nodes must exist before any edge is written ----
        for node in payload_nodes:
            conn.execute(
                "INSERT INTO nodes(name, kind) VALUES(?, ?) "
                "ON CONFLICT(name) DO UPDATE SET kind = excluded.kind",
                (node.name, node.kind),
            )

        # ---- upsert phase 2: replace candidate groups and their inputs ----
        for node in payload_nodes:
            # Replace all previously registered groups of this node.
            old_groups = conn.execute(
                "SELECT id FROM candidate_groups WHERE node_name = ?",
                (node.name,),
            ).fetchall()
            for row in old_groups:
                conn.execute(
                    "DELETE FROM group_inputs WHERE group_id = ?", (row["id"],)
                )
            conn.execute(
                "DELETE FROM candidate_groups WHERE node_name = ?", (node.name,)
            )
            for pos, group in enumerate(node.candidate_groups):
                cur = conn.execute(
                    "INSERT INTO candidate_groups(node_name, position) "
                    "VALUES(?, ?)",
                    (node.name, pos),
                )
                gid = cur.lastrowid
                for inp_pos, inp in enumerate(group):
                    conn.execute(
                        "INSERT INTO group_inputs(group_id, input_name, "
                        "position) VALUES(?, ?, ?)",
                        (gid, inp, inp_pos),
                    )

        # ---- whole-graph cycle check; roll back everything on a cycle ----
        kinds, groups = load_graph(conn)
        cycle = find_cycle(kinds, groups)
        if cycle is not None:
            raise CycleDetectedError(
                "登记将引入循环依赖，整批不生效",
                {"cycle": cycle},
            )

    return {
        "registered": len(payload_nodes),
        "nodes": sorted(node.name for node in payload_nodes),
        "message": "登记成功（已提交完整血缘快照）",
    }


# --------------------------------------------------------------------------- #
# Recursive impact analysis
# --------------------------------------------------------------------------- #

def analyze_impact(
    retiring: list[str], db_path: str | None = None
) -> dict:
    with db.snapshot(db_path) as conn:
        kinds, groups = load_graph(conn)

    unknown = sorted(name for name in retiring if name not in kinds)
    if unknown:
        raise NotFoundLineageError(
            "存在未登记的字段名", {"unknown": unknown}
        )

    retiring_set = set(retiring)
    sys.setrecursionlimit(_MAX_RECURSION_DEPTH)

    # availability memo: True if at least one candidate group is fully usable.
    available: dict[str, bool] = {}
    # For every unavailable node, the deepest retired causes behind it.
    root_causes: dict[str, list[str]] = {}

    def is_available(node: str) -> bool:
        if node in available:
            return available[node]
        if node in retiring_set:
            available[node] = False
            root_causes[node] = [node]
            return False

        node_groups = groups.get(node, [])
        if not node_groups:  # source node not retired
            available[node] = True
            return True

        group_ok = any(
            all(is_available(inp) for inp in group) for group in node_groups
        )
        available[node] = group_ok
        if not group_ok:
            causes: set[str] = set()
            for group in node_groups:
                for inp in group:
                    if not is_available(inp):
                        causes.update(root_causes.get(inp, [inp]))
            root_causes[node] = sorted(causes)
        return group_ok

    for name in kinds:
        is_available(name)

    unavailable = sorted(name for name, ok in available.items() if not ok)

    failed_columns = []
    for name in sorted(kinds):
        if kinds[name] != "report" or available[name]:
            continue
        failed_groups = []
        for idx, group in enumerate(groups.get(name, [])):
            missing = [
                inp for inp in group if not available[inp]
            ]
            if not missing:
                continue
            failed_groups.append(
                {
                    "group_index": idx,
                    "inputs": list(group),
                    "missing": [
                        {
                            "name": inp,
                            "kind": kinds[inp],
                            "root_causes": root_causes.get(inp, [inp]),
                        }
                        for inp in missing
                    ],
                }
            )
        failed_columns.append(
            {"name": name, "failed_groups": failed_groups}
        )

    return {
        "retiring": sorted(retiring_set),
        "failed_report_columns": failed_columns,
        "unavailable_count": len(unavailable),
        "unavailable_nodes": unavailable,
    }


# --------------------------------------------------------------------------- #
# Read helpers
# --------------------------------------------------------------------------- #

def get_graph(db_path: str | None = None) -> dict:
    with db.snapshot(db_path) as conn:
        kinds, groups = load_graph(conn)
    nodes = [
        {
            "name": name,
            "kind": kinds[name],
            "candidate_groups": groups.get(name, []),
        }
        for name in sorted(kinds)
    ]
    counts: dict[str, int] = {"source": 0, "derived": 0, "report": 0}
    for kind in kinds.values():
        counts[kind] += 1
    return {"nodes": nodes, "counts": counts}


# --------------------------------------------------------------------------- #
# Supply-outage attribution
# --------------------------------------------------------------------------- #

def _upstream_sources(
    start: str, groups: dict[str, list[list[str]]]
) -> set[str]:
    """Collect every source node reachable from ``start`` (itself included)."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        for group in groups.get(node, []):
            stack.extend(group)
    return {node for node in seen if not groups.get(node)}


def _topological_order(
    nodes: set[str], groups: dict[str, list[list[str]]]
) -> list[str]:
    """Order nodes so every input of a node precedes the node.

    The stored graph is acyclic (enforced at registration time), so plain
    DFS post-order is sufficient.
    """
    color: dict[str, int] = {name: 0 for name in nodes}
    order: list[str] = []

    def visit(node: str) -> None:
        if color[node] == 2:
            return
        color[node] = 1
        for group in groups.get(node, []):
            for inp in group:
                if inp in color:
                    visit(inp)
        color[node] = 2
        order.append(node)

    for name in sorted(nodes):
        visit(name)
    return order


def attribute_outage(payload, db_path: str | None = None) -> dict:
    """Find the minimum additional source outages explaining the observations.

    Availability follows the same candidate-group rules as impact analysis:
    a group is usable when *all* its inputs are usable, and a node is produced
    when *any* group is usable. Every unconfirmed source is assumed available
    unless a minimal explanation requires stopping it.
    """
    stopped = list(payload.stopped_sources)
    succeeded = list(payload.successful_reports)
    failed = list(payload.failed_reports)

    # ---- payload-level validation (duplicates / empty observations) ----
    dupes: dict[str, list[str]] = {}
    for label, values in (
        ("stopped_sources", stopped),
        ("successful_reports", succeeded),
        ("failed_reports", failed),
    ):
        if len(values) != len(set(values)):
            dupes[label] = sorted(
                {name for name in values if values.count(name) > 1}
            )
    overlap = sorted(set(succeeded) & set(failed))
    if dupes or overlap:
        details: dict[str, list] = {}
        if dupes:
            details["duplicate_names"] = dupes
        if overlap:
            details["report_in_both_lists"] = overlap
        raise ValidationLineageError(
            "停供归因请求不合法：存在重复条目或报表同时标记成功与失败", details
        )
    if not succeeded and not failed:
        raise ValidationLineageError(
            "停供归因请求不合法：至少需要一个成功或失败的报表观测",
            {"missing_observations": [
                "successful_reports", "failed_reports"
            ]},
        )

    # ---- whole inference runs against one committed snapshot ----
    with db.snapshot(db_path) as conn:
        kinds, groups = load_graph(conn)

    unknown: dict[str, list[str]] = {}
    bad_sources = sorted(
        name for name in set(stopped)
        if name not in kinds or kinds[name] != "source"
    )
    if bad_sources:
        unknown["stopped_sources"] = bad_sources
    bad_reports: dict[str, list[str]] = {}
    for label, values in (
        ("successful_reports", succeeded),
        ("failed_reports", failed),
    ):
        bad = sorted(
            name for name in values
            if name not in kinds or kinds[name] != "report"
        )
        if bad:
            bad_reports[label] = bad
    if unknown or bad_reports:
        details = dict(unknown)
        details.update(bad_reports)
        raise NotFoundLineageError(
            "存在未登记或类型不符的字段名", details
        )

    observed = set(succeeded) | set(failed)
    confirmed = set(stopped)

    # ---- inference scope: all upstream sources of observed reports ----
    relevant: set[str] = set()
    for name in observed:
        relevant |= _upstream_sources(name, groups)
    if len(relevant) > _MAX_RELEVANT_SOURCES:
        raise InferenceScopeExceededError(
            "相关上游源字段超过 16 个，拒绝推断以避免无界枚举",
            {
                "relevant_source_count": len(relevant),
                "limit": _MAX_RELEVANT_SOURCES,
                "relevant_sources": sorted(relevant),
            },
        )

    variable_sources = sorted(relevant - confirmed)
    n = len(variable_sources)
    full_mask = (1 << (1 << n)) - 1 if n else 1

    sys.setrecursionlimit(_MAX_RECURSION_DEPTH)

    # Bit i of a node's availability mask is 1 when the node is available
    # under assignment i (bit j of i => variable source j is stopped).
    avail: dict[str, int] = {}
    for idx, name in enumerate(variable_sources):
        col = 1 << idx
        # Available exactly on rows where this source is NOT stopped.
        avail[name] = full_mask ^ _column_mask(col, n)
    for name in relevant:  # confirmed sources inside the scope
        if name in confirmed:
            avail[name] = 0

    # Only nodes feeding an observed report can influence the result.
    scope_nodes: set[str] = set(observed)
    for name in observed:
        scope_nodes |= _reachable_inputs(name, groups)

    for node in _topological_order(scope_nodes, groups):
        if node in avail:
            continue  # source handled above
        node_mask = 0
        for group in groups.get(node, []):
            group_mask = full_mask
            for inp in group:
                if inp in scope_nodes:
                    group_mask &= avail.get(inp, full_mask)
            node_mask |= group_mask
        avail[node] = node_mask

    # ---- constraints over the 2**n candidate assignments ----
    feasible = full_mask
    for name in succeeded:
        feasible &= avail[name]
    for name in failed:
        feasible &= full_mask ^ avail[name]
    if not feasible:
        raise NoConsistentSolutionError(
            "不存在能同时解释全部报表观测的停供方案，观测间存在冲突",
            {
                "successful_reports": sorted(succeeded),
                "failed_reports": sorted(failed),
            },
        )

    # ---- minimum number of additional stopped sources ----
    min_extra = None
    minimal_rows: list[int] = []
    for bits in range(n + 1):
        rows = [
            row for row in _rows_with_popcount(bits, n)
            if feasible & (1 << row)
        ]
        if rows:
            min_extra = bits
            minimal_rows = rows
            break

    solutions = [
        sorted(
            variable_sources[j]
            for j in range(n)
            if row & (1 << j)
        )
        for row in minimal_rows
    ]
    must_check = sorted(
        variable_sources[j]
        for j in range(n)
        if all(row & (1 << j) for row in minimal_rows)
    )
    to_check = sorted(
        variable_sources[j]
        for j in range(n)
        if any(row & (1 << j) for row in minimal_rows)
    )

    return {
        "stopped_sources": sorted(confirmed),
        "successful_reports": sorted(succeeded),
        "failed_reports": sorted(failed),
        "relevant_sources": sorted(relevant),
        "minimal_extra_stopped": min_extra,
        "consistent_solution_count": len(minimal_rows),
        "must_check_sources": must_check,
        "to_check_sources": to_check,
        "minimal_solutions": solutions,
    }


def _column_mask(column_bit: int, width: int) -> int:
    """Mask with every truth-table row set whose index contains the column."""
    mask = 0
    for row in range(1 << width):
        if row & column_bit:
            mask |= 1 << row
    return mask


def _reachable_inputs(
    start: str, groups: dict[str, list[list[str]]]
) -> set[str]:
    """Every node that ``start`` transitively depends on."""
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        for group in groups.get(node, []):
            for inp in group:
                if inp not in seen:
                    seen.add(inp)
                    stack.append(inp)
    return seen


def _rows_with_popcount(bits: int, width: int):
    """Yield all ``width``-bit row indices having exactly ``bits`` set."""
    if bits == 0:
        yield 0
        return
    if bits >= width:
        if bits == width:
            yield (1 << width) - 1
        return
    # Gosper's hack: iterate combinations of `bits` columns in increasing row.
    row = (1 << bits) - 1
    limit = 1 << width
    while row < limit:
        yield row
        low = row & -row
        carry = row + low
        row = (((row ^ carry) >> 2) // low) | carry
