from __future__ import annotations

from dataclasses import replace
from itertools import combinations
from statistics import median
from typing import Any

from schemer.analysis.drawing_model import Envelope
from schemer.analysis.measurements import top_level_root_symbol_groups
from schemer.analysis.quality import top_level_block_envelopes, top_level_signal_adjacencies
from schemer.analysis.sheet_metrics import primary_component
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan
from schemer.placement.spacing.shared import translate_root_groups


def compact_excessive_signal_block_gaps(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    preferred_gap: float = 180.0,
    maximum_gap: float = 360.0,
) -> LayoutPlan:
    """Pull signal-connected top-level peers toward the primary functional block.

    The primary remains fixed. Only a peer separated by more than the maximum
    is moved, and its existing left/right relationship is preserved.
    """

    if preferred_gap < 0 or maximum_gap < preferred_gap:
        raise ToolchainError("top-level signal gap bounds are invalid")
    proposed = plan.apply_to_schematic(schematic)
    root_ref = proposed.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic root reference is invalid")
    root_layout = next(
        (module for module in plan.modules if module.instance_ref == root_ref),
        None,
    )
    if root_layout is None:
        raise ToolchainError("layout plan has no root module")

    envelopes = top_level_block_envelopes(proposed)
    primary_ref = primary_component(proposed).instance_ref
    primary_path = primary_ref.removeprefix(root_ref + ".")
    primary_block = primary_path.split(".", 1)[0]
    primary = envelopes.get(primary_block)
    if primary is None:
        raise ToolchainError("primary IC has no top-level functional block")

    connected = top_level_signal_adjacencies(proposed)
    deltas: dict[str, float] = {}
    for block_name, envelope in envelopes.items():
        if block_name == primary_block:
            continue
        if frozenset((primary_block, block_name)) not in connected:
            continue
        if envelope.max_x <= primary.min_x:
            gap = primary.min_x - envelope.max_x
            direction = 1.0
        elif envelope.min_x >= primary.max_x:
            gap = envelope.min_x - primary.max_x
            direction = -1.0
        else:
            continue
        if gap > maximum_gap:
            deltas[block_name] = direction * (gap - preferred_gap)

    if not deltas:
        return plan
    assignments = top_level_root_symbol_groups(proposed)
    updated = translate_root_groups(
        root_layout,
        assignments,
        {block_name: (delta_x, 0.0) for block_name, delta_x in deltas.items()},
    )

    modules = [
        replace(module, positions=updated) if module is root_layout else module
        for module in plan.modules
    ]
    return replace(plan, modules=tuple(modules))


def separate_tight_signal_block_gaps(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    minimum_gap: float = 100.0,
    aligned_boundary_tolerance: float = 50.0,
) -> LayoutPlan:
    """Push connected blocks away from the primary when their corridor is tight.

    Blocks sharing the moving block's near boundary are translated with it.
    This preserves a semantic output column (for example, two downstream
    interfaces) while still keeping the primary IC fixed.
    """

    if minimum_gap < 0 or aligned_boundary_tolerance < 0:
        raise ToolchainError("top-level signal clearance parameters are invalid")
    proposed = plan.apply_to_schematic(schematic)
    root_ref = proposed.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic root reference is invalid")
    root_layout = next(
        (module for module in plan.modules if module.instance_ref == root_ref),
        None,
    )
    if root_layout is None:
        raise ToolchainError("layout plan has no root module")

    envelopes = top_level_block_envelopes(proposed)
    primary_ref = primary_component(proposed).instance_ref
    primary_block = primary_ref.removeprefix(root_ref + ".").split(".", 1)[0]
    primary = envelopes.get(primary_block)
    if primary is None:
        raise ToolchainError("primary IC has no top-level functional block")

    connected = top_level_signal_adjacencies(proposed)
    required: dict[str, float] = {}
    boundary: dict[str, tuple[str, float]] = {}
    for block_name, envelope in envelopes.items():
        if block_name == primary_block:
            continue
        if frozenset((primary_block, block_name)) not in connected:
            continue
        if envelope.max_x <= primary.min_x:
            gap = primary.min_x - envelope.max_x
            delta_x = -(minimum_gap - gap)
            boundary[block_name] = ("max_x", envelope.max_x)
        elif envelope.min_x >= primary.max_x:
            gap = envelope.min_x - primary.max_x
            delta_x = minimum_gap - gap
            boundary[block_name] = ("min_x", envelope.min_x)
        else:
            gap = -min(primary.max_x - envelope.min_x, envelope.max_x - primary.min_x)
            if envelope.center_x < primary.center_x:
                delta_x = primary.min_x - minimum_gap - envelope.max_x
                boundary[block_name] = ("max_x", envelope.max_x)
            else:
                delta_x = primary.max_x - envelope.min_x + minimum_gap
                boundary[block_name] = ("min_x", envelope.min_x)
        if gap < minimum_gap:
            required[block_name] = delta_x

    if not required:
        return plan

    deltas = dict(required)
    for moving_block, delta_x in required.items():
        boundary_name, boundary_value = boundary[moving_block]
        for peer_name, peer_envelope in envelopes.items():
            if peer_name == primary_block:
                continue
            peer_boundary = getattr(peer_envelope, boundary_name)
            if abs(peer_boundary - boundary_value) <= aligned_boundary_tolerance:
                previous = deltas.get(peer_name)
                if previous is None or abs(delta_x) > abs(previous):
                    deltas[peer_name] = delta_x

    assignments = top_level_root_symbol_groups(proposed)
    updated = translate_root_groups(
        root_layout,
        assignments,
        {block_name: (delta_x, 0.0) for block_name, delta_x in deltas.items()},
    )

    modules = [
        replace(module, positions=updated) if module is root_layout else module
        for module in plan.modules
    ]
    return replace(plan, modules=tuple(modules))


def relative_block_deltas(
    envelopes: dict[str, Envelope],
    right_of: tuple[tuple[str, str], ...],
    clearance: float,
) -> dict[str, tuple[float, float]]:
    """Place complete blocks in stages derived from explicit relative relations.

    Each pair means (subject, predecessor). Peers at the same dependency depth
    are stacked vertically, never serialized into an invented signal chain.
    """
    names = set(envelopes)
    mentioned = {name for pair in right_of for name in pair}
    if mentioned != names:
        raise ToolchainError(
            "right-of relations must cover all visible top-level blocks; "
            f"unknown={sorted(mentioned - names)}, missing={sorted(names - mentioned)}"
        )
    predecessors = {name: set() for name in names}
    for subject, predecessor in right_of:
        predecessors[subject].add(predecessor)
    levels: dict[str, int] = {}
    while len(levels) < len(names):
        ready = sorted(
            name for name in names - levels.keys() if predecessors[name] <= levels.keys()
        )
        if not ready:
            raise ToolchainError("contradictory right-of relations form a cycle")
        for name in ready:
            levels[name] = max((levels[previous] + 1 for previous in predecessors[name]), default=0)
    deltas = {}
    cursor_x = 0.0
    for level in range(max(levels.values()) + 1):
        peers = sorted(name for name in names if levels[name] == level)
        height = sum(envelopes[name].height for name in peers) + clearance * (len(peers) - 1)
        cursor_y = -height / 2
        for name in peers:
            envelope = envelopes[name]
            dx, dy = cursor_x - envelope.min_x, cursor_y - envelope.min_y
            deltas[name] = (0.0 if abs(dx) < 1e-9 else dx, 0.0 if abs(dy) < 1e-9 else dy)
            cursor_y += envelope.height + clearance
        cursor_x += max(envelopes[name].width for name in peers) + clearance
    return deltas


def pack_top_level_groups(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    clearance: float = 100.0,
    right_of: tuple[tuple[str, str], ...] = (),
) -> LayoutPlan:
    """Freeze complete groups, then pack them around the primary IC.

    The supplied schematic must already contain the plan's current positions.
    Groups on each side of the primary form one vertical column. Every group is
    moved once through its root-owned anchors; its internal layout is never
    revisited after packing.
    """

    if clearance < 0:
        raise ToolchainError("top-level block clearance must be non-negative")
    root_ref = schematic.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic root reference is invalid")
    root_layout = next(
        (module for module in plan.modules if module.instance_ref == root_ref),
        None,
    )
    if root_layout is None:
        raise ToolchainError("layout plan has no root module")

    envelopes = top_level_block_envelopes(schematic)
    if right_of:
        assignments = top_level_root_symbol_groups(schematic)
        deltas = relative_block_deltas(envelopes, right_of, clearance)
        updated = translate_root_groups(root_layout, assignments, deltas)
        return replace(
            plan,
            modules=tuple(
                replace(module, positions=updated) if module is root_layout else module
                for module in plan.modules
            ),
        )
    selected_primary = primary_component(schematic)
    primary_ref = selected_primary.instance_ref
    primary_block = primary_ref.removeprefix(root_ref + ".").split(".", 1)[0]
    primary = envelopes.get(primary_block)
    if primary is None:
        raise ToolchainError("primary IC has no top-level functional block")

    assignments = top_level_root_symbol_groups(schematic)
    root_instance = schematic["instances"].get(root_ref)
    root_positions = (
        root_instance.get("symbol_positions") if isinstance(root_instance, dict) else None
    )
    if not isinstance(root_positions, dict):
        raise ToolchainError("schematic root positions are invalid")
    anchor_x: dict[str, float] = {}
    for name in envelopes:
        coordinates = [
            float(raw["x"])
            for symbol_id, raw in root_positions.items()
            if isinstance(symbol_id, str)
            and symbol_id.startswith("comp:")
            and isinstance(raw, dict)
            and assignments.get(symbol_id) == name
        ]
        anchor_x[name] = float(median(coordinates)) if coordinates else envelopes[name].center_x
    primary_anchor_x = anchor_x[primary_block]

    left = sorted(
        (name for name in envelopes if name != primary_block and anchor_x[name] < primary_anchor_x),
        key=lambda name: (envelopes[name].center_y, name),
    )
    right = sorted(
        (
            name
            for name in envelopes
            if name != primary_block and anchor_x[name] >= primary_anchor_x
        ),
        key=lambda name: (envelopes[name].center_y, name),
    )

    deltas: dict[str, tuple[float, float]] = {}
    for side, names in (("left", left), ("right", right)):
        if not names:
            continue
        total_height = sum(envelopes[name].height for name in names) + clearance * (len(names) - 1)
        cursor_y = selected_primary.envelope.center_y - total_height / 2
        for name in names:
            envelope = envelopes[name]
            target_min_y = cursor_y
            if side == "left":
                delta_x = primary.min_x - clearance - envelope.max_x
            else:
                delta_x = primary.max_x + clearance - envelope.min_x
            delta_y = target_min_y - envelope.min_y
            deltas[name] = (
                0.0 if abs(delta_x) < 1e-9 else delta_x,
                0.0 if abs(delta_y) < 1e-9 else delta_y,
            )
            cursor_y += envelope.height + clearance

    if not deltas:
        return plan
    updated = translate_root_groups(root_layout, assignments, deltas)

    packed = {
        name: envelope.translated(*deltas.get(name, (0.0, 0.0)))
        for name, envelope in envelopes.items()
    }
    for first_name, second_name in combinations(sorted(packed), 2):
        first = packed[first_name]
        second = packed[second_name]
        overlap_x = min(first.max_x, second.max_x) - max(first.min_x, second.min_x)
        overlap_y = min(first.max_y, second.max_y) - max(first.min_y, second.min_y)
        if overlap_x > 0 and overlap_y > 0:
            raise ToolchainError(
                f"packed top-level functional groups overlap: {first_name}, {second_name}"
            )
    modules = [
        replace(module, positions=updated) if module is root_layout else module
        for module in plan.modules
    ]
    return replace(plan, modules=tuple(modules))
