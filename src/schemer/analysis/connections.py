from __future__ import annotations

import re
from dataclasses import replace
from statistics import median
from typing import Any

from schemer.analysis.findings import SeriesFeedGeometry
from schemer.core.errors import ToolchainError
from schemer.core.layout import ModuleLayout, Position
from schemer.symbols.geometry import pin_position, placed_symbol_bounds
from schemer.symbols.library import symbol_pin_offsets
from schemer.symbols.model import PlacedBounds, Point
from schemer.symbols.net_symbols import net_symbol_pin_position, net_with_default_signal_symbol


def single_neighbour_pin_anchor(
    net: dict[str, Any],
    *,
    subject_ref: str,
    component_groups: dict[str, list[tuple[str, Position]]],
    instances: dict[str, Any],
    net_symbol_groups: dict[str, list[Position]],
) -> Point | None:
    if is_named_interface_net(net) and len(net_symbol_groups.get(net.get("name"), [])) >= 2:
        # This connection ends at its local named port, not the remote block.
        return None
    component_refs = tuple(component_groups)
    neighbours = {
        component_ref
        for port_ref in net.get("ports", [])
        if isinstance(port_ref, str)
        if (component_ref := port_component_ref(port_ref, component_refs)) is not None
        if component_ref != subject_ref
    }
    if len(neighbours) > 1:
        return None
    if len(neighbours) == 1:
        neighbour_ref = next(iter(neighbours))
        group = component_groups[neighbour_ref]
        neighbour = instances.get(neighbour_ref)
        if len(group) != 1 or not isinstance(neighbour, dict):
            return None
        _, neighbour_position = group[0]
        offsets = symbol_pin_offsets(neighbour)
        prefix = neighbour_ref + "."
        points = []
        for port_ref in net.get("ports", []):
            if not isinstance(port_ref, str) or not port_ref.startswith(prefix):
                continue
            terminal = port_ref.removeprefix(prefix)
            if "." not in terminal and terminal in offsets:
                points.append(pin_position(neighbour, neighbour_position, terminal))
        if points:
            return Point(
                x=float(median(point.x for point in points)),
                y=float(median(point.y for point in points)),
            )

    net_name = net.get("name")
    if isinstance(net_name, str) and net_name in net_symbol_groups:
        projected = net_with_default_signal_symbol(net)
        points = [
            net_symbol_pin_position(projected, position) for position in net_symbol_groups[net_name]
        ]
        return Point(
            x=float(median(point.x for point in points)),
            y=float(median(point.y for point in points)),
        )
    return None


def module_boundary_net_names(module_instance: dict[str, Any]) -> set[str]:
    """Read configured net-valued ports from an evaluated module signature."""

    attributes = module_instance.get("attributes")
    signature = attributes.get("__signature") if isinstance(attributes, dict) else None
    payload = signature.get("Json") if isinstance(signature, dict) else None
    parameters = payload.get("parameters") if isinstance(payload, dict) else None
    if not isinstance(parameters, list):
        return set()
    result: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, dict) or parameter.get("is_config"):
            continue
        value = parameter.get("value")
        if not isinstance(value, dict):
            continue
        for net_value in value.values():
            if not isinstance(net_value, dict):
                continue
            name = net_value.get("name")
            if isinstance(name, str):
                result.add(name)
    return result


def is_named_interface_net(net: dict[str, Any]) -> bool:
    """An explicit presentation endpoint, not an incidental floating label."""
    value = net.get("properties", {}).get("symbol_name")
    if isinstance(value, dict):
        value = value.get("String")
    return value == "SignalTermination"


def internal_signal_symbol_ids(
    positions: dict[str, Position],
    component_groups: dict[str, list[tuple[str, Position]]],
    nets: dict[str, Any],
    boundary_net_names: set[str],
) -> set[str]:
    """Return net-symbol IDs that interrupt an entirely local signal wire."""

    component_refs = tuple(component_groups)
    result: set[str] = set()
    for symbol_id in positions:
        if not symbol_id.startswith("sym:"):
            continue
        net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
        if not separator or not suffix.isdigit():
            continue
        net = nets.get(net_name)
        named_pair = sum(key.startswith(f"sym:{net_name}#") for key in positions) >= 2
        if (
            not isinstance(net, dict)
            or is_rail_net(net_name, net)
            or (is_named_interface_net(net) and named_pair)
        ):
            continue
        ports = tuple(port for port in net.get("ports", ()) if isinstance(port, str))
        local_components = {
            component_ref
            for port in ports
            if (component_ref := port_component_ref(port, component_refs)) is not None
        }
        has_module_boundary = (
            net_name in boundary_net_names or net.get("name") in boundary_net_names
        )
        if len(local_components) >= 2 and not has_module_boundary:
            result.add(symbol_id)
    return result


def power_branch_exists(
    rail_ref: str,
    *,
    subject_ref: str,
    component_groups: dict[str, list[tuple[str, Position]]],
    instances: dict[str, Any],
    nets: dict[str, Any],
) -> bool:
    """Recognise a rail capacitor belonging to a shared supply branch."""

    for component_ref in component_groups:
        if component_ref == subject_ref:
            continue
        component = instances.get(component_ref)
        if not isinstance(component, dict) or component.get("kind") != "Component":
            continue
        terminals = terminal_net_map(component_ref, component, nets)
        terminal_refs = {net_ref for net_ref, _ in terminals.values()}
        if rail_ref not in terminal_refs or len(terminal_refs) != 2:
            continue
        if all(is_rail_net(net_ref, net) for net_ref, net in terminals.values()):
            return True
    return False


def component_group_bounds(
    component: dict[str, Any], group: list[tuple[str, Position]]
) -> PlacedBounds:
    bounds: PlacedBounds | None = None
    for _, position in group:
        placed = placed_symbol_bounds(component, position)
        bounds = placed if bounds is None else bounds.union(placed)
    if bounds is None:
        raise ToolchainError("placed component group has no symbols")
    return bounds


NON_OWNER_TYPES = {
    "capacitor",
    "diode",
    "ferrite_bead",
    "inductor",
    "mechanical",
    "mounting_hole",
    "resistor",
    "test_point",
    "transformer",
}


def resolved_to_source_ids(schematic: dict[str, Any], module: ModuleLayout) -> dict[str, str]:
    from schemer.core.layout import resolve_module_position_ids

    result: dict[str, str] = {}
    for source_id, position in module.positions.items():
        single = replace(module, positions={source_id: position})
        resolved = resolve_module_position_ids(single, schematic)
        result[next(iter(resolved))] = source_id
    return result


def two_terminal_axis(
    instance: dict[str, Any], position: Position, terminals: tuple[str, str]
) -> str | None:
    first = pin_position(instance, position, terminals[0])
    second = pin_position(instance, position, terminals[1])
    delta_x = abs(second.x - first.x)
    delta_y = abs(second.y - first.y)
    if delta_x > delta_y:
        return "x"
    if delta_y > delta_x:
        return "y"
    return None


def terminal_net_map(
    component_ref: str,
    instance: dict[str, Any],
    nets: dict[str, Any],
) -> dict[str, tuple[str, dict[str, Any]]]:
    pin_offsets = symbol_pin_offsets(instance)
    prefix = component_ref + "."
    result: dict[str, tuple[str, dict[str, Any]]] = {}
    for net_ref, net in nets.items():
        if not isinstance(net_ref, str) or not isinstance(net, dict):
            continue
        for port_ref in net.get("ports", []):
            if not isinstance(port_ref, str) or not port_ref.startswith(prefix):
                continue
            terminal_name = port_ref.removeprefix(prefix)
            if "." not in terminal_name and terminal_name in pin_offsets:
                result[terminal_name] = (net_ref, net)
    return result


def is_return_net(net_ref: str, net: dict[str, Any]) -> bool:
    kind = str(net.get("kind", "")).casefold()
    name = str(net.get("name", net_ref)).upper()
    return kind == "ground" or bool(re.search(r"(?:^|_)(?:GND|GROUND)(?:$|_)", name))


def is_rail_net(net_ref: str, net: dict[str, Any]) -> bool:
    kind = str(net.get("kind", "")).casefold()
    name = str(net.get("name", net_ref)).upper()
    return kind in {"ground", "power"} or bool(
        re.search(r"(?:^|_)(?:GND|GROUND|VCC|VDD|POWER|VSYS)(?:$|_)", name)
    )


def net_anchor(
    net: dict[str, Any],
    *,
    subject_ref: str,
    component_groups: dict[str, list[tuple[str, Position]]],
    net_symbol_groups: dict[str, list[Position]],
) -> Point | None:
    points: list[Point] = []
    component_refs = tuple(component_groups)
    attached_components = {
        component_ref
        for port_ref in net.get("ports", [])
        if isinstance(port_ref, str)
        if (component_ref := port_component_ref(port_ref, component_refs)) is not None
        if component_ref != subject_ref
    }
    for component_ref in attached_components:
        points.append(_center([position for _, position in component_groups[component_ref]]))

    net_name = net.get("name")
    if isinstance(net_name, str) and net_name in net_symbol_groups:
        projected = net_with_default_signal_symbol(net)
        symbol_points = [
            net_symbol_pin_position(projected, position) for position in net_symbol_groups[net_name]
        ]
        points.append(
            Point(
                x=float(median(point.x for point in symbol_points)),
                y=float(median(point.y for point in symbol_points)),
            )
        )
    if not points:
        return None
    return Point(
        x=float(median(point.x for point in points)),
        y=float(median(point.y for point in points)),
    )


def port_component_ref(port_ref: str, component_refs: tuple[str, ...]) -> str | None:
    matches = [
        component_ref
        for component_ref in component_refs
        if port_ref.startswith(component_ref + ".")
    ]
    return max(matches, key=len) if matches else None


def component_symbol_groups(
    module_ref: str, positions: dict[str, Position]
) -> dict[str, list[tuple[str, Position]]]:
    groups: dict[str, list[tuple[str, Position]]] = {}
    for symbol_id, position in positions.items():
        if not symbol_id.startswith("comp:"):
            continue
        local_ref = symbol_id.removeprefix("comp:").split("@", 1)[0]
        component_ref = module_ref + "." + local_ref
        groups.setdefault(component_ref, []).append((symbol_id, position))
    return groups


def _center(positions: list[Position]) -> Point:
    return Point(
        x=float(median(position.x for position in positions)),
        y=float(median(position.y for position in positions)),
    )


def validate_series_feed_order(
    instance: dict[str, Any],
    component_position: Position,
    *,
    upstream_pin: str,
    downstream_pin: str,
    upstream_feed: Position,
    downstream_feed: Position,
    label: str,
) -> SeriesFeedGeometry:
    """Reject crossed feeds that make a series component look bypassed.

    The viewer may route an upstream feed on the left to a terminal on the
    right, and a downstream feed on the right to a terminal on the left. That
    drawing is topologically false to a reader even when the netlist remains
    correct. A valid horizontal series branch keeps feed and terminal order
    consistent and places the component between the feed anchors.
    """

    upstream_pin_position = pin_position(instance, component_position, upstream_pin)
    downstream_pin_position = pin_position(instance, component_position, downstream_pin)
    upstream_feed_point = Point(upstream_feed.x, upstream_feed.y)
    downstream_feed_point = Point(downstream_feed.x, downstream_feed.y)
    feed_delta = downstream_feed_point.x - upstream_feed_point.x
    pin_delta = downstream_pin_position.x - upstream_pin_position.x

    if feed_delta == 0 or pin_delta == 0 or feed_delta * pin_delta <= 0:
        raise ToolchainError(
            f"{label} has inverted terminal feeds; rotate or mirror the series symbol "
            "before coordinate refinement"
        )
    if (
        not min(upstream_feed_point.x, downstream_feed_point.x)
        < component_position.x
        < max(upstream_feed_point.x, downstream_feed_point.x)
    ):
        raise ToolchainError(f"{label} series component is not between its feed anchors")

    return SeriesFeedGeometry(
        upstream_feed=upstream_feed_point,
        upstream_pin=upstream_pin_position,
        downstream_pin=downstream_pin_position,
        downstream_feed=downstream_feed_point,
    )


def opposing_anchor_axis(first: Point, second: Point, component: Position) -> str | None:
    delta_x = abs(second.x - first.x)
    delta_y = abs(second.y - first.y)
    if delta_x >= delta_y and (first.x - component.x) * (second.x - component.x) < 0:
        return "x"
    if delta_y > delta_x and (first.y - component.y) * (second.y - component.y) < 0:
        return "y"
    return None
