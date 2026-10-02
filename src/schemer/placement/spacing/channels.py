from __future__ import annotations

from dataclasses import replace
from math import hypot, inf
from statistics import median
from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    component_group_bounds,
    component_symbol_groups,
    is_rail_net,
    resolved_to_source_ids,
    terminal_net_map,
)
from schemer.analysis.sheet_metrics import primary_component
from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import (
    LayoutPlan,
    ModuleLayout,
    position_from_viewer,
)


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

    excluded_owner_types = NON_OWNER_TYPES | {"connector"}
    modules: list[ModuleLayout] = []
    for module in plan.modules:
        module_instance = instances.get(module.instance_ref)
        raw_positions = (
            module_instance.get("symbol_positions") if isinstance(module_instance, dict) else None
        )
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module.instance_ref, resolved_positions)
        resolved_to_source = resolved_to_source_ids(proposed, module)

        active: list[tuple[str, Any]] = []
        terminal_by_component: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        bounds_by_component: dict[str, Any] = {}
        for component_ref, group in component_groups.items():
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminals = terminal_net_map(component_ref, component, nets)
            try:
                bounds = component_group_bounds(component, group)
            except ToolchainError:
                # Some evaluated service symbols have no drawable primitive.
                # They cannot participate in geometric channel ownership.
                continue
            terminal_by_component[component_ref] = terminals
            bounds_by_component[component_ref] = bounds
            component_type = (attribute_string(component, "type") or "").casefold()
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
                is_rail_net(net_ref, net) for net_ref, net in terminals.values()
            )
            bounds = bounds_by_component[component_ref]
            candidates: list[tuple[int, int, float, str]] = []
            for owner_ref, owner_refs in owner_net_refs.items():
                shared = component_net_refs & owner_refs
                shared_nonrail = sum(
                    not is_rail_net(net_ref, nets[net_ref])
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
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module.instance_ref, resolved_positions)
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
                bounds = component_group_bounds(component, group)
            except ToolchainError:
                continue
            components[component_ref] = component
            bounds_by_component[component_ref] = bounds
            terminal_by_component[component_ref] = terminal_net_map(
                component_ref,
                component,
                nets,
            )
        primary_bounds = bounds_by_component.get(primary_ref)
        if primary_bounds is None:
            modules.append(module)
            continue

        resolved_to_source = resolved_to_source_ids(proposed, module)
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
                support_type = (attribute_string(support, "type") or "").casefold()
                if support_type not in NON_OWNER_TYPES:
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
