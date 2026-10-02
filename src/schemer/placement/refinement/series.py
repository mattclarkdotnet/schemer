from __future__ import annotations

from dataclasses import replace
from statistics import median
from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    component_symbol_groups,
    is_rail_net,
    port_component_ref,
    resolved_to_source_ids,
    single_neighbour_pin_anchor,
    terminal_net_map,
    two_terminal_axis,
)
from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position, position_from_viewer
from schemer.symbols.geometry import pin_position, placed_symbol_bounds
from schemer.symbols.library import symbol_pin_offsets
from schemer.symbols.model import Point
from schemer.symbols.net_symbols import net_with_default_signal_symbol, position_net_symbol_pin


def hang_leaf_series_from_device_pins(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    series_offset: float = 180.0,
    leaf_offset: float = 180.0,
    minimum_annotation_pitch: float = 90.0,
) -> LayoutPlan:
    """Hang a one-ended series branch from the real side of its device pin.

    The eligible topology has a single non-passive device on one terminal net
    and only a named leaf symbol on the other. Branches are grouped by device
    and pin side. Every branch retains its owner's real pin row; crowded
    annotations use successively farther horizontal lanes instead of creating
    vertical doglegs.
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
        positions = {
            symbol_id: position_from_viewer(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = component_symbol_groups(module.instance_ref, positions)
        component_refs = tuple(component_groups)
        resolved_to_source = resolved_to_source_ids(proposed, module)
        net_symbols: dict[str, list[tuple[str, Position]]] = {}
        for symbol_id, position in positions.items():
            if not symbol_id.startswith("sym:"):
                continue
            net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
            if separator and suffix.isdigit():
                net_symbols.setdefault(net_name, []).append((symbol_id, position))

        candidates: list[tuple[str, int, str, str, Point, Point, Position, Position]] = []
        leaf_nets: dict[str, dict[str, Any]] = {}
        claimed_leaves: set[str] = set()
        for series_ref, series_group in sorted(component_groups.items()):
            if len(series_group) != 1:
                continue
            series_id, series_position = series_group[0]
            series = instances.get(series_ref)
            if not isinstance(series, dict) or series.get("kind") != "Component":
                continue
            terminal_nets = terminal_net_map(series_ref, series, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if any(is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue

            sides: list[tuple[str, dict[str, Any], set[str]]] = []
            for terminal, (_, net) in terminal_nets.items():
                neighbours = {
                    neighbour_ref
                    for port_ref in net.get("ports", [])
                    if isinstance(port_ref, str)
                    if (neighbour_ref := port_component_ref(port_ref, component_refs)) is not None
                    if neighbour_ref != series_ref
                }
                sides.append((terminal, net, neighbours))
            device_terminal: str | None = None
            leaf_terminal: str | None = None
            device_net: dict[str, Any] | None = None
            leaf_net: dict[str, Any] | None = None
            if len(sides[0][2]) == 1 and not sides[1][2]:
                device_terminal, device_net = sides[0][0], sides[0][1]
                leaf_terminal, leaf_net = sides[1][0], sides[1][1]
            elif len(sides[1][2]) == 1 and not sides[0][2]:
                device_terminal, device_net = sides[1][0], sides[1][1]
                leaf_terminal, leaf_net = sides[0][0], sides[0][1]
            if (
                device_terminal is None
                or leaf_terminal is None
                or device_net is None
                or leaf_net is None
            ):
                continue

            owner_refs = {
                owner_ref
                for port_ref in device_net.get("ports", [])
                if isinstance(port_ref, str)
                if (owner_ref := port_component_ref(port_ref, component_refs)) is not None
                if owner_ref != series_ref
            }
            if len(owner_refs) != 1:
                continue
            owner_ref = next(iter(owner_refs))
            owner_group = component_groups[owner_ref]
            owner = instances.get(owner_ref)
            if (
                len(owner_group) != 1
                or not isinstance(owner, dict)
                or (attribute_string(owner, "type") or "").casefold() in NON_OWNER_TYPES
            ):
                continue
            _, owner_position = owner_group[0]
            owner_pin = single_neighbour_pin_anchor(
                device_net,
                subject_ref=series_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups={},
            )
            if owner_pin is None:
                continue
            owner_bounds = placed_symbol_bounds(owner, owner_position)
            direction = -1 if owner_pin.x < owner_bounds.center_x else 1

            orientation_candidates: list[tuple[float, float, Position]] = []
            for rotation in (0.0, 90.0, 180.0, 270.0):
                candidate_position = Position(0.0, 0.0, rotation)
                device_pin = pin_position(series, candidate_position, device_terminal)
                leaf_pin = pin_position(series, candidate_position, leaf_terminal)
                orientation_candidates.append(
                    (
                        direction * (leaf_pin.x - device_pin.x),
                        -rotation,
                        candidate_position,
                    )
                )
            series_base = max(orientation_candidates)[2]

            leaf_name = leaf_net.get("name")
            leaf_candidates = net_symbols.get(leaf_name, []) if isinstance(leaf_name, str) else []
            leaf_candidates = [
                candidate for candidate in leaf_candidates if candidate[0] not in claimed_leaves
            ]
            if not leaf_candidates:
                continue
            leaf_id, leaf_position = min(
                leaf_candidates,
                key=lambda item: (
                    abs(item[1].x - series_position.x) + abs(item[1].y - series_position.y),
                    item[0],
                ),
            )
            claimed_leaves.add(leaf_id)
            leaf_source = resolved_to_source[leaf_id]
            leaf_nets[leaf_source] = leaf_net
            terminal_points = [
                pin_position(series, series_base, terminal) for terminal in terminals
            ]
            series_center = Point(
                x=float(median(point.x for point in terminal_points)),
                y=float(median(point.y for point in terminal_points)),
            )
            candidates.append(
                (
                    owner_ref,
                    direction,
                    resolved_to_source[series_id],
                    leaf_source,
                    owner_pin,
                    series_center,
                    leaf_position,
                    series_base,
                )
            )

        grouped: dict[
            tuple[str, int],
            list[tuple[str, int, str, str, Point, Point, Position, Position]],
        ] = {}
        for candidate in candidates:
            grouped.setdefault((candidate[0], candidate[1]), []).append(candidate)

        updated = dict(module.positions)
        for group in grouped.values():
            ordered = sorted(group, key=lambda candidate: (candidate[4].y, candidate[2]))
            lane_ys: list[float] = []
            for candidate in ordered:
                (
                    _,
                    direction,
                    series_source,
                    leaf_source,
                    owner_pin,
                    series_center,
                    leaf,
                    series_base,
                ) = candidate
                lane = next(
                    (
                        lane_index
                        for lane_index, previous_y in enumerate(lane_ys)
                        if owner_pin.y - previous_y >= minimum_annotation_pitch
                    ),
                    len(lane_ys),
                )
                if lane == len(lane_ys):
                    lane_ys.append(owner_pin.y)
                else:
                    lane_ys[lane] = owner_pin.y
                target_y = owner_pin.y
                target_x = owner_pin.x + direction * (
                    series_offset + lane * minimum_annotation_pitch
                )
                delta_x = target_x - series_center.x
                delta_y = target_y - series_center.y
                updated[series_source] = replace(
                    series_base,
                    x=series_base.x + delta_x,
                    y=series_base.y + delta_y,
                )
                leaf_target = Point(
                    target_x + direction * leaf_offset,
                    target_y,
                )
                updated[leaf_source] = position_net_symbol_pin(
                    net_with_default_signal_symbol(leaf_nets[leaf_source]),
                    leaf_target,
                    rotation=updated[leaf_source].rotation,
                )

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


def align_series_to_device_pins(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    maximum_adjustment: float = 500.0,
) -> LayoutPlan:
    """Register horizontal two-terminal elements to adjacent device pins.

    A series element with a uniquely evidenced non-passive neighbour inherits
    the real y coordinate of that neighbour's connected pin. The pass is
    deliberately rerunnable after symbol projection, when a larger IC body or
    collapsed package has changed its pin pitch.
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
        component_refs = tuple(component_groups)
        resolved_to_source = resolved_to_source_ids(proposed, module)
        updated = dict(module.positions)

        for component_ref, group in sorted(component_groups.items()):
            if len(group) != 1:
                continue
            resolved_id, component_position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminals = terminal_net_map(component_ref, component, nets)
            if len(terminals) != 2:
                continue
            terminal_names = tuple(sorted(terminals))
            if two_terminal_axis(component, component_position, terminal_names) != "x":
                continue

            target_points: list[Point] = []
            for _, net in terminals.values():
                neighbours = {
                    neighbour_ref
                    for port_ref in net.get("ports", [])
                    if isinstance(port_ref, str)
                    if (neighbour_ref := port_component_ref(port_ref, component_refs)) is not None
                    if neighbour_ref != component_ref
                }
                if len(neighbours) != 1:
                    continue
                neighbour_ref = next(iter(neighbours))
                neighbour_group = component_groups[neighbour_ref]
                neighbour = instances.get(neighbour_ref)
                if (
                    len(neighbour_group) != 1
                    or not isinstance(neighbour, dict)
                    or (attribute_string(neighbour, "type") or "").casefold() in NON_OWNER_TYPES
                ):
                    continue
                _, neighbour_position = neighbour_group[0]
                neighbour_offsets = symbol_pin_offsets(neighbour)
                prefix = neighbour_ref + "."
                connected_terminals = [
                    port_ref.removeprefix(prefix)
                    for port_ref in net.get("ports", [])
                    if isinstance(port_ref, str) and port_ref.startswith(prefix)
                ]
                pin_points = [
                    pin_position(neighbour, neighbour_position, terminal)
                    for terminal in connected_terminals
                    if "." not in terminal and terminal in neighbour_offsets
                ]
                if pin_points:
                    target_points.append(
                        Point(
                            x=float(median(point.x for point in pin_points)),
                            y=float(median(point.y for point in pin_points)),
                        )
                    )
            if not target_points:
                continue
            target_y = float(median(point.y for point in target_points))
            own_pins = [
                pin_position(component, component_position, terminal) for terminal in terminal_names
            ]
            own_y = float(median(point.y for point in own_pins))
            delta_y = target_y - own_y
            if abs(delta_y) > maximum_adjustment:
                continue
            source_id = resolved_to_source[resolved_id]
            updated[source_id] = replace(updated[source_id], y=updated[source_id].y + delta_y)

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))
