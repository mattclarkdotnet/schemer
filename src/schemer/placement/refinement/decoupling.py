from __future__ import annotations

from dataclasses import replace
from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    component_group_bounds,
    component_symbol_groups,
    is_rail_net,
    is_return_net,
    power_branch_exists,
    resolved_to_source_ids,
    terminal_net_map,
)
from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, position_from_viewer
from schemer.symbols.geometry import placed_symbol_bounds
from schemer.symbols.model import PlacedBounds


def cluster_local_decoupling(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    geometry_gap: float = 30.0,
    collision_clearance: float = 20.0,
    attached_symbol_radius: float = 500.0,
) -> LayoutPlan:
    """Attach each local rail-to-return capacitor to its consuming device.

    Ownership is inferred only from evaluated connectivity. A capacitor across
    one supply rail and one return rail belongs to the nearest placed device
    that consumes both nets. A capacitor on the input or output of a placed
    all-rail series element is instead accepted as part of a common supply
    branch. Anything satisfying neither topology is rejected as an unowned
    passive: coordinates cannot make an unexplained component readable.
    """

    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    nets = proposed.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

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
        updated = dict(module.positions)
        moved_positions = dict(resolved_positions)
        claimed_symbols: set[str] = set()

        for capacitor_ref, capacitor_group in sorted(component_groups.items()):
            capacitor = instances.get(capacitor_ref)
            if (
                not isinstance(capacitor, dict)
                or capacitor.get("kind") != "Component"
                or attribute_string(capacitor, "type") != "capacitor"
                or len(capacitor_group) != 1
            ):
                continue
            terminals = terminal_net_map(capacitor_ref, capacitor, nets)
            if len(terminals) != 2:
                continue
            return_entries = [
                (net_ref, net)
                for net_ref, net in terminals.values()
                if is_return_net(net_ref, net)
            ]
            supply_entries = [
                (net_ref, net)
                for net_ref, net in terminals.values()
                if is_rail_net(net_ref, net) and not is_return_net(net_ref, net)
            ]
            if len(return_entries) != 1 or len(supply_entries) != 1:
                continue

            capacitor_nets = {return_entries[0][0], supply_entries[0][0]}
            _, capacitor_position = capacitor_group[0]
            capacitor_bounds = placed_symbol_bounds(capacitor, capacitor_position)
            owners: list[tuple[float, str, PlacedBounds]] = []
            for owner_ref, owner_group in component_groups.items():
                if owner_ref == capacitor_ref:
                    continue
                owner = instances.get(owner_ref)
                if not isinstance(owner, dict) or owner.get("kind") != "Component":
                    continue
                component_type = attribute_string(owner, "type") or ""
                if component_type.casefold() in NON_OWNER_TYPES:
                    continue
                owner_terminals = terminal_net_map(owner_ref, owner, nets)
                owner_nets = {net_ref for net_ref, _ in owner_terminals.values()}
                if not capacitor_nets <= owner_nets:
                    continue
                owner_bounds = component_group_bounds(owner, owner_group)
                distance = abs(capacitor_bounds.center_x - owner_bounds.center_x) + abs(
                    capacitor_bounds.center_y - owner_bounds.center_y
                )
                owners.append((distance, owner_ref, owner_bounds))

            if not owners:
                if power_branch_exists(
                    supply_entries[0][0],
                    subject_ref=capacitor_ref,
                    component_groups=component_groups,
                    instances=instances,
                    nets=nets,
                ):
                    continue
                raise ToolchainError(
                    f"unowned local rail passive {capacitor_ref}: no placed device consumes "
                    "both its supply and return nets, and no common power branch is present"
                )

            _, selected_owner_ref, owner_bounds = min(owners, key=lambda item: (item[0], item[1]))
            candidate_moves = (
                (
                    "right",
                    owner_bounds.max_x + geometry_gap - capacitor_bounds.min_x,
                    owner_bounds.center_y - capacitor_bounds.center_y,
                ),
                (
                    "top",
                    owner_bounds.center_x - capacitor_bounds.center_x,
                    owner_bounds.min_y - geometry_gap - capacitor_bounds.max_y,
                ),
                (
                    "bottom",
                    owner_bounds.center_x - capacitor_bounds.center_x,
                    owner_bounds.max_y + geometry_gap - capacitor_bounds.min_y,
                ),
                (
                    "left",
                    owner_bounds.min_x - geometry_gap - capacitor_bounds.max_x,
                    owner_bounds.center_y - capacitor_bounds.center_y,
                ),
            )
            direction_priority = {name: index for index, (name, _, _) in enumerate(candidate_moves)}
            scored_moves: list[tuple[int, float, int, float, float]] = []
            for direction, candidate_delta_x, candidate_delta_y in candidate_moves:
                candidate_bounds = capacitor_bounds.translated(candidate_delta_x, candidate_delta_y)
                collision_count = 0
                collision_area = 0.0
                for other_ref, other_group in component_groups.items():
                    if other_ref in {capacitor_ref, selected_owner_ref}:
                        continue
                    other = instances.get(other_ref)
                    if not isinstance(other, dict) or other.get("kind") != "Component":
                        continue
                    other_bounds = component_group_bounds(other, other_group)
                    overlap_x = min(
                        candidate_bounds.max_x + collision_clearance,
                        other_bounds.max_x,
                    ) - max(
                        candidate_bounds.min_x - collision_clearance,
                        other_bounds.min_x,
                    )
                    overlap_y = min(
                        candidate_bounds.max_y + collision_clearance,
                        other_bounds.max_y,
                    ) - max(
                        candidate_bounds.min_y - collision_clearance,
                        other_bounds.min_y,
                    )
                    if overlap_x <= 0 or overlap_y <= 0:
                        continue
                    collision_count += 1
                    collision_area += overlap_x * overlap_y
                scored_moves.append(
                    (
                        collision_count,
                        collision_area,
                        direction_priority[direction],
                        candidate_delta_x,
                        candidate_delta_y,
                    )
                )
            _, _, _, delta_x, delta_y = min(scored_moves)

            capacitor_id = capacitor_group[0][0]
            capacitor_source_id = resolved_to_source[capacitor_id]
            updated[capacitor_source_id] = replace(
                updated[capacitor_source_id],
                x=updated[capacitor_source_id].x + delta_x,
                y=updated[capacitor_source_id].y + delta_y,
            )
            moved_positions[capacitor_id] = replace(
                capacitor_position,
                x=capacitor_position.x + delta_x,
                y=capacitor_position.y + delta_y,
            )
            component_groups[capacitor_ref] = [(capacitor_id, moved_positions[capacitor_id])]

            for net_ref, net in terminals.values():
                net_name = net.get("name", net_ref)
                if not isinstance(net_name, str):
                    continue
                candidates = [
                    (symbol_id, position)
                    for symbol_id, position in moved_positions.items()
                    if symbol_id.startswith(f"sym:{net_name}#") and symbol_id not in claimed_symbols
                ]
                if not candidates:
                    continue
                symbol_id, symbol_position = min(
                    candidates,
                    key=lambda item: (
                        abs(item[1].x - capacitor_position.x)
                        + abs(item[1].y - capacitor_position.y),
                        item[0],
                    ),
                )
                distance_to_capacitor = abs(symbol_position.x - capacitor_position.x) + abs(
                    symbol_position.y - capacitor_position.y
                )
                distance_to_owner = abs(symbol_position.x - owner_bounds.center_x) + abs(
                    symbol_position.y - owner_bounds.center_y
                )
                if (
                    distance_to_capacitor > attached_symbol_radius
                    or distance_to_capacitor >= distance_to_owner
                ):
                    continue
                source_id = resolved_to_source[symbol_id]
                updated[source_id] = replace(
                    updated[source_id],
                    x=updated[source_id].x + delta_x,
                    y=updated[source_id].y + delta_y,
                )
                moved_positions[symbol_id] = replace(
                    symbol_position,
                    x=symbol_position.x + delta_x,
                    y=symbol_position.y + delta_y,
                )
                claimed_symbols.add(symbol_id)

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))
