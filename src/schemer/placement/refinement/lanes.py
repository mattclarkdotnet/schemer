from __future__ import annotations

from dataclasses import replace
from typing import Any

from schemer.analysis.connections import (
    component_symbol_groups,
    is_rail_net,
    resolved_to_source_ids,
    terminal_net_map,
    two_terminal_axis,
)
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position, position_from_viewer


def separate_orthogonal_branch_lanes(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    same_lane_tolerance: float = 40.0,
    vertical_clearance: float = 140.0,
    attached_symbol_radius: float = 300.0,
) -> LayoutPlan:
    """Move vertical rail branches out of colliding horizontal series lanes.

    A pull, bias, termination, or signal shunt is visually subordinate to a
    horizontal signal chain. When both occupy the same x lane at overlapping y
    coordinates, the rail branch and its nearest local rail symbol move onto
    the midpoint of the signal net's connected-component span. That is the
    natural shared-node trunk: it keeps the branch between its neighbours
    instead of sending it beyond the chain and creating a rectangular detour.
    Selection uses only connectivity, rail semantics, orientation, and
    geometry; identifiers and component types are irrelevant.
    """

    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    nets = proposed.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    modules: list[ModuleLayout] = []
    for module in plan.modules:
        instance = instances.get(module.instance_ref)
        raw_positions = instance.get("symbol_positions") if isinstance(instance, dict) else None
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module.instance_ref, resolved_positions)
        resolved_to_source = resolved_to_source_ids(proposed, module)

        series: list[tuple[str, Position, set[str]]] = []
        branches: list[tuple[str, Position, str, str]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = terminal_net_map(component_ref, component, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            axis = two_terminal_axis(component, position, terminals)
            rail_entries = [
                (net_ref, net)
                for net_ref, net in terminal_nets.values()
                if is_rail_net(net_ref, net)
            ]
            if axis == "x" and not rail_entries:
                series.append(
                    (
                        resolved_id,
                        position,
                        {net_ref for net_ref, _ in terminal_nets.values()},
                    )
                )
            elif axis == "y" and len(rail_entries) == 1:
                rail_name = rail_entries[0][1].get("name", rail_entries[0][0])
                if isinstance(rail_name, str):
                    signal_nets = [
                        net_ref
                        for net_ref, net in terminal_nets.values()
                        if not is_rail_net(net_ref, net)
                    ]
                    if len(signal_nets) == 1:
                        branches.append((resolved_id, position, rail_name, signal_nets[0]))

        updated = dict(module.positions)
        moved_resolved_positions = dict(resolved_positions)
        claimed_symbols: set[str] = set()
        for branch_id, branch_position, rail_name, signal_net_ref in sorted(branches):
            colliding = [
                series_position
                for _, series_position, _ in series
                if abs(series_position.x - branch_position.x) <= same_lane_tolerance
                and abs(series_position.y - branch_position.y) < vertical_clearance
            ]
            if not colliding:
                continue
            signal_net = nets.get(signal_net_ref)
            if not isinstance(signal_net, dict):
                continue
            neighbour_x: list[float] = []
            branch_ref = module.instance_ref + "." + branch_id.removeprefix("comp:")
            for port_ref in signal_net.get("ports", []):
                if not isinstance(port_ref, str):
                    continue
                component_ref = port_ref.rsplit(".", 1)[0]
                if component_ref == branch_ref:
                    continue
                group = component_groups.get(component_ref)
                if group:
                    neighbour_x.extend(position.x for _, position in group)
            if len(neighbour_x) < 2 or min(neighbour_x) == max(neighbour_x):
                continue
            target_x = (min(neighbour_x) + max(neighbour_x)) / 2
            delta_x = target_x - branch_position.x
            source_id = resolved_to_source[branch_id]
            updated[source_id] = replace(updated[source_id], x=updated[source_id].x + delta_x)
            moved_resolved_positions[branch_id] = replace(branch_position, x=target_x)

            candidates = [
                (symbol_id, position)
                for symbol_id, position in moved_resolved_positions.items()
                if symbol_id.startswith(f"sym:{rail_name}#") and symbol_id not in claimed_symbols
            ]
            if not candidates:
                continue
            rail_symbol_id, rail_symbol_position = min(
                candidates,
                key=lambda item: (
                    abs(item[1].x - branch_position.x) + abs(item[1].y - branch_position.y)
                ),
            )
            distance = abs(rail_symbol_position.x - branch_position.x) + abs(
                rail_symbol_position.y - branch_position.y
            )
            if distance > attached_symbol_radius:
                continue
            rail_source_id = resolved_to_source[rail_symbol_id]
            updated[rail_source_id] = replace(
                updated[rail_source_id], x=updated[rail_source_id].x + delta_x
            )
            moved_resolved_positions[rail_symbol_id] = replace(
                rail_symbol_position, x=rail_symbol_position.x + delta_x
            )
            claimed_symbols.add(rail_symbol_id)

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))
