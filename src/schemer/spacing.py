"""Generic whitespace allocation after semantic placement."""

from __future__ import annotations

from dataclasses import replace
from itertools import combinations
from math import hypot, inf
from statistics import median
from typing import Any

from schemer.layout import LayoutPlan, ModuleLayout, Position, resolve_module_position_ids
from schemer.layout_metrics import Envelope, primary_component, top_level_root_symbol_groups
from schemer.quality import top_level_block_envelopes, top_level_signal_adjacencies
from schemer.symbol_geometry import (
    _NON_OWNER_TYPES,
    _attribute_string,
    _component_groups,
    _group_bounds,
    _is_rail_net,
    _resolved_to_source_ids,
    _terminal_nets,
    _viewer_position,
)
from schemer.toolchain import ToolchainError


def _translate_root_groups(
    root_layout: ModuleLayout,
    assignments: dict[str, str],
    deltas: dict[str, tuple[float, float]],
) -> dict[str, Position]:
    """Translate complete root groups without separating their local branches."""

    updated = dict(root_layout.positions)
    moved_anchors: set[str] = set()
    for symbol_id, position in root_layout.positions.items():
        group_name = assignments.get(symbol_id)
        delta = deltas.get(group_name) if group_name is not None else None
        if delta is None:
            continue
        delta_x, delta_y = delta
        updated[symbol_id] = replace(
            position,
            x=position.x + delta_x,
            y=position.y + delta_y,
        )
        if symbol_id.startswith("comp:"):
            moved_anchors.add(group_name)
    missing = set(deltas) - moved_anchors
    if missing:
        raise ToolchainError(
            "top-level functional groups have no movable root anchors: "
            + ", ".join(sorted(missing))
        )
    return updated


def _module_net_kind(
    schematic: dict[str, Any],
    module_ref: str,
    source_symbol_id: str,
) -> str:
    root_ref = schematic.get("root_ref")
    nets = schematic.get("nets")
    if not isinstance(root_ref, str) or not isinstance(nets, dict):
        raise ToolchainError("schematic root or nets are invalid")
    raw_name = source_symbol_id.removeprefix("sym:").rsplit("#", 1)[0]
    module_path = "" if module_ref == root_ref else module_ref.removeprefix(root_ref + ".")
    scoped_name = f"{module_path}.{raw_name}" if module_path else raw_name
    for candidate in (raw_name, scoped_name):
        net = nets.get(candidate)
        if isinstance(net, dict):
            return str(net.get("kind", ""))
    for net in nets.values():
        if isinstance(net, dict) and net.get("name") in {raw_name, scoped_name}:
            return str(net.get("kind", ""))
    return ""


def spread_parallel_rail_labels(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    row_tolerance: float = 30.0,
    minimum_pitch: float = 160.0,
    average_character_width: float = 6.5,
    label_padding: float = 25.0,
) -> LayoutPlan:
    """Separate crowded power/ground symbols that occupy one label row.

    The viewer renders rail names horizontally above their symbols. Several
    distinct rails on nearby pins therefore need more pitch than their pin
    origins provide. This pass preserves order and y lanes, moving only rail
    symbols and leaving every component anchor untouched.
    """

    if row_tolerance < 0 or minimum_pitch <= 0 or average_character_width <= 0:
        raise ToolchainError("rail-label spacing parameters are invalid")
    modules: list[ModuleLayout] = []
    for module in plan.modules:
        candidates = [
            (symbol_id, position)
            for symbol_id, position in module.positions.items()
            if symbol_id.startswith("sym:")
            and _module_net_kind(schematic, module.instance_ref, symbol_id) in {"Power", "Ground"}
        ]
        rows: list[list[tuple[str, Any]]] = []
        for candidate in sorted(candidates, key=lambda item: (item[1].y, item[1].x, item[0])):
            row = next(
                (
                    current
                    for current in rows
                    if abs(candidate[1].y - median(item[1].y for item in current)) <= row_tolerance
                ),
                None,
            )
            if row is None:
                rows.append([candidate])
            else:
                row.append(candidate)

        updated = dict(module.positions)
        for row in rows:
            if len(row) < 2:
                continue
            ordered = sorted(row, key=lambda item: (item[1].x, item[0]))
            targets = [item[1].x for item in ordered]
            for index in range(1, len(ordered)):
                left_name = ordered[index - 1][0].removeprefix("sym:").rsplit("#", 1)[0]
                right_name = ordered[index][0].removeprefix("sym:").rsplit("#", 1)[0]
                text_pitch = (
                    len(left_name) + len(right_name)
                ) * average_character_width / 2 + label_padding
                pitch = max(minimum_pitch, text_pitch)
                targets[index] = max(targets[index], targets[index - 1] + pitch)
            centering = float(median(item[1].x for item in ordered) - median(targets))
            for (symbol_id, position), target_x in zip(ordered, targets, strict=True):
                updated[symbol_id] = replace(position, x=target_x + centering)
        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


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
    updated = _translate_root_groups(
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
    updated = _translate_root_groups(
        root_layout,
        assignments,
        {block_name: (delta_x, 0.0) for block_name, delta_x in deltas.items()},
    )

    modules = [
        replace(module, positions=updated) if module is root_layout else module
        for module in plan.modules
    ]
    return replace(plan, modules=tuple(modules))


def spread_repeated_active_channels(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    lane_x_tolerance: float = 80.0,
    minimum_body_clearance: float = 180.0,
    minimum_lane_members: int = 3,
    obstacle_clearance: float = 80.0,
) -> LayoutPlan:
    """Give repeated active channels room for wires, fields, and local support.

    Active bodies form a repeated lane when three or more have aligned body
    centres. The bodies are spread as a group. Passives are carried with the
    uniquely connected channel; shared-rail passives use nearest-owner
    proximity. Net symbols follow the nearest connected component. No device
    names, reference designators, or board identities participate.
    """

    if (
        lane_x_tolerance < 0
        or minimum_body_clearance < 0
        or minimum_lane_members < 2
        or obstacle_clearance < 0
    ):
        raise ToolchainError("repeated active-channel spacing parameters are invalid")
    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    nets = proposed.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    excluded_owner_types = _NON_OWNER_TYPES | {"connector"}
    modules: list[ModuleLayout] = []
    for module in plan.modules:
        module_instance = instances.get(module.instance_ref)
        raw_positions = (
            module_instance.get("symbol_positions") if isinstance(module_instance, dict) else None
        )
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        resolved_to_source = _resolved_to_source_ids(proposed, module)

        active: list[tuple[str, Any]] = []
        terminal_by_component: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        bounds_by_component: dict[str, Any] = {}
        for component_ref, group in component_groups.items():
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminals = _terminal_nets(component_ref, component, nets)
            try:
                bounds = _group_bounds(component, group)
            except ToolchainError:
                # Some evaluated service symbols have no drawable primitive.
                # They cannot participate in geometric channel ownership.
                continue
            terminal_by_component[component_ref] = terminals
            bounds_by_component[component_ref] = bounds
            component_type = (_attribute_string(component, "type") or "").casefold()
            if (
                len(group) == 1
                and len(terminals) >= 3
                and component_type not in excluded_owner_types
            ):
                active.append((component_ref, bounds))

        lanes: list[list[tuple[str, Any]]] = []
        for candidate in sorted(active, key=lambda item: (item[1].center_x, item[1].center_y)):
            lane = next(
                (
                    current
                    for current in lanes
                    if abs(candidate[1].center_x - median(member[1].center_x for member in current))
                    <= lane_x_tolerance
                ),
                None,
            )
            if lane is None:
                lanes.append([candidate])
            else:
                lane.append(candidate)

        owner_deltas: dict[str, float] = {}
        repeated_lanes: list[list[str]] = []
        for lane in lanes:
            if len(lane) < minimum_lane_members:
                continue
            ordered = sorted(lane, key=lambda item: (item[1].min_y, item[0]))
            repeated_lanes.append([owner_ref for owner_ref, _ in ordered])
            raw_deltas: list[float] = []
            previous_max_y: float | None = None
            for _, bounds in ordered:
                target_min_y = bounds.min_y
                if previous_max_y is not None:
                    target_min_y = max(
                        target_min_y,
                        previous_max_y + minimum_body_clearance,
                    )
                delta_y = target_min_y - bounds.min_y
                raw_deltas.append(delta_y)
                previous_max_y = bounds.max_y + delta_y
            centering = float(median(raw_deltas))
            for (owner_ref, _), delta_y in zip(ordered, raw_deltas, strict=True):
                owner_deltas[owner_ref] = delta_y - centering

        if not owner_deltas:
            modules.append(module)
            continue

        owner_net_refs = {
            owner_ref: {net_ref for net_ref, _ in terminal_by_component[owner_ref].values()}
            for owner_ref in owner_deltas
        }
        assigned_owner: dict[str, str] = {owner_ref: owner_ref for owner_ref in owner_deltas}
        for component_ref, terminals in terminal_by_component.items():
            if component_ref in assigned_owner or component_ref in owner_net_refs:
                continue
            component_net_refs = {net_ref for net_ref, _ in terminals.values()}
            if not component_net_refs:
                continue
            all_rail = len(component_net_refs) == 2 and all(
                _is_rail_net(net_ref, net) for net_ref, net in terminals.values()
            )
            bounds = bounds_by_component[component_ref]
            candidates: list[tuple[int, int, float, str]] = []
            for owner_ref, owner_refs in owner_net_refs.items():
                shared = component_net_refs & owner_refs
                shared_nonrail = sum(
                    not _is_rail_net(net_ref, nets[net_ref])
                    for net_ref in shared
                    if isinstance(nets.get(net_ref), dict)
                )
                if not shared_nonrail and not (all_rail and component_net_refs <= owner_refs):
                    continue
                owner_bounds = bounds_by_component[owner_ref]
                distance = hypot(
                    bounds.center_x - owner_bounds.center_x,
                    bounds.center_y - owner_bounds.center_y,
                )
                candidates.append((bool(shared_nonrail), shared_nonrail, -distance, owner_ref))
            if candidates:
                assigned_owner[component_ref] = max(candidates)[3]

        # Keep the complete channel lane clear of unrelated structure. Shift
        # all lane owners together; do not sacrifice their internal pitch or
        # move an obstruction that may be a coherent functional chain.
        for lane_owner_refs in repeated_lanes:
            lane_bounds = bounds_by_component[lane_owner_refs[0]].translated(
                0,
                owner_deltas[lane_owner_refs[0]],
            )
            for owner_ref in lane_owner_refs[1:]:
                lane_bounds = lane_bounds.union(
                    bounds_by_component[owner_ref].translated(
                        0,
                        owner_deltas[owner_ref],
                    )
                )
            lane_members = {
                component_ref
                for component_ref, owner_ref in assigned_owner.items()
                if owner_ref in lane_owner_refs
            }
            minimum_shift = -inf
            maximum_shift = inf
            for component_ref, obstacle in bounds_by_component.items():
                if component_ref in lane_members:
                    continue
                overlap_x = min(lane_bounds.max_x, obstacle.max_x) - max(
                    lane_bounds.min_x,
                    obstacle.min_x,
                )
                if overlap_x <= 0:
                    continue
                if obstacle.center_y < lane_bounds.center_y:
                    minimum_shift = max(
                        minimum_shift,
                        obstacle.max_y + obstacle_clearance - lane_bounds.min_y,
                    )
                else:
                    maximum_shift = min(
                        maximum_shift,
                        obstacle.min_y - obstacle_clearance - lane_bounds.max_y,
                    )
            if minimum_shift <= maximum_shift:
                lane_shift = min(max(0.0, minimum_shift), maximum_shift)
                for owner_ref in lane_owner_refs:
                    owner_deltas[owner_ref] += lane_shift

        component_net_anchors: dict[str, list[tuple[float, float, str | None]]] = {}
        for component_ref, terminals in terminal_by_component.items():
            bounds = bounds_by_component[component_ref]
            owner_ref = assigned_owner.get(component_ref)
            for net_ref, _ in terminals.values():
                component_net_anchors.setdefault(net_ref, []).append(
                    (bounds.center_x, bounds.center_y, owner_ref)
                )

        symbol_owner: dict[str, str] = {}
        net_ref_by_name = {
            str(net.get("name", net_ref)): net_ref
            for net_ref, net in nets.items()
            if isinstance(net_ref, str) and isinstance(net, dict)
        }
        for resolved_id, position in resolved_positions.items():
            if not resolved_id.startswith("sym:"):
                continue
            net_name = resolved_id.removeprefix("sym:").rsplit("#", 1)[0]
            net_ref = net_ref_by_name.get(net_name)
            anchors = component_net_anchors.get(net_ref, []) if net_ref is not None else []
            if not anchors:
                continue
            nearest = min(
                anchors,
                key=lambda anchor: hypot(position.x - anchor[0], position.y - anchor[1]),
            )
            if nearest[2] is not None:
                symbol_owner[resolved_id] = nearest[2]

        updated = dict(module.positions)
        for component_ref, owner_ref in assigned_owner.items():
            delta_y = owner_deltas[owner_ref]
            for resolved_id, _ in component_groups[component_ref]:
                source_id = resolved_to_source[resolved_id]
                updated[source_id] = replace(
                    updated[source_id],
                    y=updated[source_id].y + delta_y,
                )
        for resolved_id, owner_ref in symbol_owner.items():
            source_id = resolved_to_source[resolved_id]
            updated[source_id] = replace(
                updated[source_id],
                y=updated[source_id].y + owner_deltas[owner_ref],
            )
        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


def separate_primary_neighbour_overlaps(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    clearance: float = 30.0,
) -> LayoutPlan:
    """Keep an enlarged primary fixed and move colliding local branches away.

    The smallest outward translation is selected from the peer's existing
    side of the primary. Directly connected passive support farther along that
    direction moves with the peer, preserving the local chain.
    """

    if clearance < 0:
        raise ToolchainError("primary-neighbour clearance must be non-negative")
    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    nets = proposed.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")
    primary_ref = primary_component(proposed).instance_ref

    modules: list[ModuleLayout] = []
    for module in plan.modules:
        module_instance = instances.get(module.instance_ref)
        raw_positions = (
            module_instance.get("symbol_positions") if isinstance(module_instance, dict) else None
        )
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        if primary_ref not in component_groups:
            modules.append(module)
            continue

        components: dict[str, dict[str, Any]] = {}
        bounds_by_component: dict[str, Any] = {}
        terminal_by_component: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        for component_ref, group in component_groups.items():
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            try:
                bounds = _group_bounds(component, group)
            except ToolchainError:
                continue
            components[component_ref] = component
            bounds_by_component[component_ref] = bounds
            terminal_by_component[component_ref] = _terminal_nets(
                component_ref,
                component,
                nets,
            )
        primary_bounds = bounds_by_component.get(primary_ref)
        if primary_bounds is None:
            modules.append(module)
            continue

        resolved_to_source = _resolved_to_source_ids(proposed, module)
        translations: dict[str, tuple[float, float]] = {}
        for peer_ref, peer_bounds in bounds_by_component.items():
            if peer_ref == primary_ref:
                continue
            overlap_x = min(primary_bounds.max_x, peer_bounds.max_x) - max(
                primary_bounds.min_x,
                peer_bounds.min_x,
            )
            overlap_y = min(primary_bounds.max_y, peer_bounds.max_y) - max(
                primary_bounds.min_y,
                peer_bounds.min_y,
            )
            if overlap_x <= 0 or overlap_y <= 0:
                continue
            outward_x = (
                primary_bounds.max_x + clearance - peer_bounds.min_x
                if peer_bounds.center_x >= primary_bounds.center_x
                else primary_bounds.min_x - clearance - peer_bounds.max_x
            )
            outward_y = (
                primary_bounds.max_y + clearance - peer_bounds.min_y
                if peer_bounds.center_y >= primary_bounds.center_y
                else primary_bounds.min_y - clearance - peer_bounds.max_y
            )
            relative_x = abs(peer_bounds.center_x - primary_bounds.center_x)
            relative_y = abs(peer_bounds.center_y - primary_bounds.center_y)
            if relative_x > relative_y:
                translations[peer_ref] = (outward_x, 0.0)
            elif relative_y > relative_x:
                translations[peer_ref] = (0.0, outward_y)
            elif abs(outward_x) <= abs(outward_y):
                translations[peer_ref] = (outward_x, 0.0)
            else:
                translations[peer_ref] = (0.0, outward_y)

        if not translations:
            modules.append(module)
            continue

        for peer_ref, (delta_x, delta_y) in tuple(translations.items()):
            peer_nets = {net_ref for net_ref, _ in terminal_by_component[peer_ref].values()}
            peer_bounds = bounds_by_component[peer_ref]
            for support_ref, support in components.items():
                if support_ref in {primary_ref, peer_ref} or support_ref in translations:
                    continue
                support_type = (_attribute_string(support, "type") or "").casefold()
                if support_type not in _NON_OWNER_TYPES:
                    continue
                support_nets = {
                    net_ref for net_ref, _ in terminal_by_component[support_ref].values()
                }
                if not peer_nets & support_nets:
                    continue
                support_bounds = bounds_by_component[support_ref]
                direction_projection = (
                    support_bounds.center_x - peer_bounds.center_x
                ) * delta_x + (support_bounds.center_y - peer_bounds.center_y) * delta_y
                if direction_projection >= 0:
                    translations[support_ref] = (delta_x, delta_y)

        updated = dict(module.positions)
        for component_ref, (delta_x, delta_y) in translations.items():
            for resolved_id, _ in component_groups[component_ref]:
                source_id = resolved_to_source[resolved_id]
                updated[source_id] = replace(
                    updated[source_id],
                    x=updated[source_id].x + delta_x,
                    y=updated[source_id].y + delta_y,
                )
        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


def remove_redundant_root_rails(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    regenerated_modules: set[str],
) -> LayoutPlan:
    """Discard seed rail copies superseded by a completed child's local rails.

    Ownership is per displayed copy, not per entire net: a connector elsewhere
    on the same rail keeps its own root symbol. Ordinary signal labels remain.
    """
    root_ref = schematic["root_ref"]
    root = next(module for module in plan.modules if module.instance_ref == root_ref)
    assignments = top_level_root_symbol_groups(schematic)
    local_rails = {}
    for module in plan.modules:
        if module.instance_ref == root_ref or module.instance_ref not in regenerated_modules:
            continue
        local_rails[module.instance_ref] = {
            symbol_id.removeprefix("sym:").rsplit("#", 1)[0]
            for symbol_id in resolve_module_position_ids(module, schematic)
            if symbol_id.startswith("sym:")
        }
    updated = dict(root.positions)
    for symbol_id in root.positions:
        if not symbol_id.startswith("sym:"):
            continue
        net_name = symbol_id.removeprefix("sym:").rsplit("#", 1)[0]
        net = schematic["nets"].get(net_name)
        owner = assignments.get(symbol_id)
        if (
            isinstance(net, dict)
            and _is_rail_net(net_name, net)
            and owner is not None
            and net_name in local_rails.get(f"{root_ref}.{owner}", set())
        ):
            del updated[symbol_id]
    return replace(
        plan,
        modules=tuple(
            replace(module, positions=updated) if module is root else module
            for module in plan.modules
        ),
    )


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
        updated = _translate_root_groups(root_layout, assignments, deltas)
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
    updated = _translate_root_groups(root_layout, assignments, deltas)

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
