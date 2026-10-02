from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Any

from schemer.analysis.connections import (
    component_symbol_groups,
    is_rail_net,
    opposing_anchor_axis,
    port_component_ref,
    resolved_to_source_ids,
    single_neighbour_pin_anchor,
    terminal_net_map,
    two_terminal_axis,
)
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position, position_from_viewer
from schemer.symbols.geometry import pin_position
from schemer.symbols.model import Point
from schemer.symbols.net_symbols import (
    net_symbol_pin_position,
    net_with_default_signal_symbol,
    position_net_symbol_pin,
)


def align_leaf_series_endpoints(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    maximum_adjustment: float = 500.0,
) -> LayoutPlan:
    """Hang a series element and movable leaf net symbol from a real device pin.

    The eligible topology has exactly one placed component on one terminal net
    and no placed component on the other, where a local net symbol provides the
    leaf endpoint. The series element and nearest leaf symbol move together to
    the real component-pin line. Annotation spacing is handled horizontally by
    the later branch-lane pass and never changes that electrical row.
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
        component_refs = tuple(component_groups)
        resolved_to_source = resolved_to_source_ids(proposed, module)
        net_symbols: dict[str, list[tuple[str, Position]]] = {}
        for symbol_id, position in resolved_positions.items():
            if not symbol_id.startswith("sym:"):
                continue
            net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
            if separator and suffix.isdigit():
                net_symbols.setdefault(net_name, []).append((symbol_id, position))

        candidates: list[tuple[str, str, Point, Point, float]] = []
        leaf_nets: dict[str, dict[str, Any]] = {}
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, component_position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = terminal_net_map(component_ref, component, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if two_terminal_axis(component, component_position, terminals) != "x":
                continue
            if any(is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue

            side_data: list[tuple[dict[str, Any], set[str]]] = []
            for _, net in terminal_nets.values():
                neighbours = {
                    neighbour_ref
                    for port_ref in net.get("ports", [])
                    if isinstance(port_ref, str)
                    if (neighbour_ref := port_component_ref(port_ref, component_refs)) is not None
                    if neighbour_ref != component_ref
                }
                side_data.append((net, neighbours))
            component_side: dict[str, Any] | None = None
            leaf_side: dict[str, Any] | None = None
            if len(side_data[0][1]) == 1 and not side_data[1][1]:
                component_side, leaf_side = side_data[0][0], side_data[1][0]
            elif len(side_data[1][1]) == 1 and not side_data[0][1]:
                component_side, leaf_side = side_data[1][0], side_data[0][0]
            if component_side is None or leaf_side is None:
                continue

            component_anchor = single_neighbour_pin_anchor(
                component_side,
                subject_ref=component_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups={},
            )
            leaf_name = leaf_side.get("name")
            leaf_candidates = net_symbols.get(leaf_name, []) if isinstance(leaf_name, str) else []
            if component_anchor is None or not leaf_candidates:
                continue
            leaf_id, leaf_position = min(
                leaf_candidates,
                key=lambda item: (
                    abs(item[1].x - component_position.x) + abs(item[1].y - component_position.y),
                    item[0],
                ),
            )
            terminal_points = [
                pin_position(component, component_position, terminal) for terminal in terminals
            ]
            terminal_center = Point(
                x=float(median(point.x for point in terminal_points)),
                y=float(median(point.y for point in terminal_points)),
            )
            leaf_anchor = net_symbol_pin_position(
                net_with_default_signal_symbol(leaf_side),
                leaf_position,
            )
            if opposing_anchor_axis(component_anchor, leaf_anchor, terminal_center) != "x":
                continue
            if abs(component_anchor.y - terminal_center.y) > maximum_adjustment:
                continue
            leaf_source_id = resolved_to_source[leaf_id]
            leaf_nets[leaf_source_id] = leaf_side
            candidates.append(
                (
                    resolved_to_source[resolved_id],
                    leaf_source_id,
                    terminal_center,
                    leaf_anchor,
                    component_anchor.y,
                )
            )

        updated = dict(module.positions)
        for candidate in sorted(candidates, key=lambda item: (item[2].x, item[4], item[0])):
            series_source_id, leaf_source_id, terminal_center, leaf_anchor, target_y = candidate
            delta_y = target_y - terminal_center.y
            if abs(delta_y) > maximum_adjustment:
                continue
            updated[series_source_id] = replace(
                updated[series_source_id],
                y=updated[series_source_id].y + delta_y,
            )
            updated[leaf_source_id] = position_net_symbol_pin(
                net_with_default_signal_symbol(leaf_nets[leaf_source_id]),
                Point(leaf_anchor.x, target_y),
                rotation=updated[leaf_source_id].rotation,
            )

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


def align_horizontal_series_terminals(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    maximum_endpoint_spread: float = 250.0,
    maximum_adjustment: float = 250.0,
    lane_tolerance: float = 40.0,
    minimum_bank_pitch: float = 100.0,
    bank_group_span: float = 400.0,
) -> LayoutPlan:
    """Centre simple horizontal series parts between unambiguous endpoints.

    Each eligible two-terminal component must have one unambiguous placed
    neighbour (or a net-symbol anchor) on each net, with those anchors on
    opposing horizontal sides. The component shifts until its real transformed
    terminal line lies midway between the endpoint pins on both axes. This also
    prevents a series body from sitting on top of either neighbouring symbol.
    Shared fan-in/fan-out nets are deliberately skipped because their offsets
    may encode circuit grammar rather than accidental misalignment.
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
        net_symbol_groups: dict[str, list[Position]] = {}
        for symbol_id, position in resolved_positions.items():
            if not symbol_id.startswith("sym:"):
                continue
            net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
            if separator and suffix.isdigit():
                net_symbol_groups.setdefault(net_name, []).append(position)

        updated = dict(module.positions)
        candidates: list[tuple[str, Point, float, float]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, component_position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = terminal_net_map(component_ref, component, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if two_terminal_axis(component, component_position, terminals) != "x":
                continue
            if any(is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue

            first_anchor = single_neighbour_pin_anchor(
                terminal_nets[terminals[0]][1],
                subject_ref=component_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups=net_symbol_groups,
            )
            second_anchor = single_neighbour_pin_anchor(
                terminal_nets[terminals[1]][1],
                subject_ref=component_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups=net_symbol_groups,
            )
            if first_anchor is None or second_anchor is None:
                continue
            terminal_points = [
                pin_position(component, component_position, terminal) for terminal in terminals
            ]
            terminal_center = Point(
                x=float(median(point.x for point in terminal_points)),
                y=float(median(point.y for point in terminal_points)),
            )
            if opposing_anchor_axis(first_anchor, second_anchor, terminal_center) != "x":
                endpoint_delta_x = abs(first_anchor.x - second_anchor.x)
                endpoint_delta_y = abs(first_anchor.y - second_anchor.y)
                if endpoint_delta_x <= endpoint_delta_y:
                    continue
            if abs(first_anchor.y - second_anchor.y) > maximum_endpoint_spread:
                continue

            target_y = float(median((first_anchor.y, second_anchor.y)))
            target_x = float(median((first_anchor.x, second_anchor.x)))
            delta_y = target_y - terminal_center.y
            delta_x = target_x - terminal_center.x
            if abs(delta_y) > maximum_adjustment or abs(delta_x) > maximum_adjustment:
                continue
            source_id = resolved_to_source[resolved_id]
            candidates.append((source_id, terminal_center, target_y, target_x))

        lanes: list[list[tuple[str, Point, float, float]]] = []
        for candidate in sorted(candidates, key=lambda item: (item[1].x, item[2], item[0])):
            matching_lane = next(
                (
                    lane
                    for lane in lanes
                    if abs(candidate[1].x - median(item[1].x for item in lane)) <= lane_tolerance
                ),
                None,
            )
            if matching_lane is None:
                lanes.append([candidate])
            else:
                matching_lane.append(candidate)

        for lane in lanes:
            ordered = sorted(lane, key=lambda item: (item[2], item[0]))
            groups: list[list[tuple[str, Point, float, float]]] = []
            for candidate in ordered:
                if not groups or candidate[2] - groups[-1][-1][2] > bank_group_span:
                    groups.append([candidate])
                else:
                    groups[-1].append(candidate)
            for group in groups:
                lane_ys: list[float] = []
                assignments: list[tuple[tuple[str, Point, float, float], int]] = []
                for candidate in group:
                    target_y = candidate[2]
                    lane = next(
                        (
                            lane_index
                            for lane_index, previous_y in enumerate(lane_ys)
                            if target_y - previous_y >= minimum_bank_pitch
                        ),
                        len(lane_ys),
                    )
                    if lane == len(lane_ys):
                        lane_ys.append(target_y)
                    else:
                        lane_ys[lane] = target_y
                    assignments.append((candidate, lane))
                lane_center = (len(lane_ys) - 1) / 2
                for (source_id, terminal_center, target_y, target_x), lane in assignments:
                    delta_y = target_y - terminal_center.y
                    adjusted_x = target_x + (lane - lane_center) * minimum_bank_pitch
                    delta_x = adjusted_x - terminal_center.x
                    if abs(delta_y) > maximum_adjustment or abs(delta_x) > maximum_adjustment:
                        continue
                    updated[source_id] = replace(
                        updated[source_id],
                        x=updated[source_id].x + delta_x,
                        y=updated[source_id].y + delta_y,
                    )

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))
