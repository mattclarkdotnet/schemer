from __future__ import annotations

from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    is_rail_net,
    port_component_ref,
    terminal_net_map,
)
from schemer.analysis.drawing_model import Envelope
from schemer.analysis.measurements import with_annotation_envelope
from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import ModuleLayout, Position
from schemer.placement.circuits.model import (
    Component,
    SeriesLink,
    SeriesWire,
)
from schemer.placement.circuits.orientation import median_point
from schemer.placement.circuits.policy import PIN_CLUSTER_GAP
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_bounds,
)
from schemer.symbols.model import Point


def collect_components(
    schematic: dict[str, Any], module: ModuleLayout
) -> tuple[dict[str, Any], dict[str, Any], tuple[Component, ...]]:
    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    prefix = module.instance_ref + "."
    result: list[Component] = []
    for ref, instance in instances.items():
        if (
            not isinstance(ref, str)
            or not ref.startswith(prefix)
            or not isinstance(instance, dict)
            or instance.get("kind") != "Component"
            or not instance.get("reference_designator")
        ):
            continue
        local_ref = ref.removeprefix(prefix)
        result.append(
            Component(
                ref,
                f"comp:{local_ref}",
                instance,
                (attribute_string(instance, "type") or "").casefold(),
                terminal_net_map(ref, instance, nets),
            )
        )
    return instances, nets, tuple(sorted(result, key=lambda item: item.ref))


def net_components(net: dict[str, Any], component_refs: tuple[str, ...]) -> set[str]:
    return {
        component_ref
        for port_ref in net.get("ports", ())
        if isinstance(port_ref, str)
        if (component_ref := port_component_ref(port_ref, component_refs)) is not None
    }


def series_links(
    components: tuple[Component, ...],
    central: Component,
    connectors: tuple[Component, ...],
) -> tuple[SeriesLink, ...]:
    component_refs = tuple(component.ref for component in components)
    connector_by_ref = {component.ref: component for component in connectors}
    links: list[SeriesLink] = []
    for passive in components:
        if passive.component_type not in NON_OWNER_TYPES or len(passive.terminals) != 2:
            continue
        if any(is_rail_net(net_ref, net) for net_ref, net in passive.terminals.values()):
            continue
        ends: list[tuple[str, str, set[str]]] = []
        for terminal, (net_ref, net) in passive.terminals.items():
            neighbours = net_components(net, component_refs) - {passive.ref}
            ends.append((terminal, net_ref, neighbours))
        central_end = next(
            (end for end in ends if central.ref in end[2]),
            None,
        )
        connector_end = next(
            (end for end in ends if len(end[2] & connector_by_ref.keys()) == 1),
            None,
        )
        if central_end is None or connector_end is None or central_end is connector_end:
            continue
        connector_ref = next(iter(connector_end[2] & connector_by_ref.keys()))
        connector = connector_by_ref[connector_ref]
        central_terminal = next(
            (
                terminal
                for terminal, (net_ref, _) in central.terminals.items()
                if net_ref == central_end[1]
            ),
            None,
        )
        connector_terminal = next(
            (
                terminal
                for terminal, (net_ref, _) in connector.terminals.items()
                if net_ref == connector_end[1]
            ),
            None,
        )
        if central_terminal is None or connector_terminal is None:
            continue
        links.append(
            SeriesLink(
                passive,
                connector,
                central_terminal,
                connector_end[1],
                connector_terminal,
                central_end[0],
                connector_end[0],
            )
        )
    return tuple(sorted(links, key=lambda link: link.passive.ref))


def component_pin_for_net(
    component: Component,
    position: Position,
    net_ref: str,
) -> tuple[str, Point] | None:
    terminals = [
        terminal
        for terminal, (candidate_ref, _) in component.terminals.items()
        if candidate_ref == net_ref
    ]
    if len(terminals) != 1:
        return None
    terminal = terminals[0]
    return terminal, median_point(pin_positions(component.instance, position, terminal))


def is_boundary_net(
    net_ref: str,
    net: dict[str, Any],
    boundary_net_names: set[str],
) -> bool:
    return net_ref in boundary_net_names or net.get("name") in boundary_net_names


def pin_clusters(points: tuple[Point, ...], side: str) -> tuple[tuple[Point, ...], ...]:
    unique = {(round(point.x, 6), round(point.y, 6)): point for point in points}
    along = (lambda point: point.y) if side in {"left", "right"} else (lambda point: point.x)
    ordered = sorted(unique.values(), key=lambda point: (along(point), point.x, point.y))
    result: list[list[Point]] = []
    for point in ordered:
        if not result or along(point) - along(result[-1][-1]) > PIN_CLUSTER_GAP:
            result.append([point])
        else:
            result[-1].append(point)
    return tuple(tuple(cluster) for cluster in result)


def connector_signal_points(
    connector: Component, position: Position, net_ref: str,
) -> tuple[Point, ...]:
    points = {
        (point.x, point.y): point
        for terminal, (candidate, _) in connector.terminals.items() if candidate == net_ref
        for point in pin_positions(connector.instance, position, terminal)
    }
    return tuple(sorted(points.values(), key=lambda point: (point.y, point.x)))


def has_straight_bundle(
    connector: Component, position: Position, wires: tuple[SeriesWire, ...],
) -> bool:
    """Independent blocks need at least three one-to-one straight lanes."""
    straight: set[tuple[str, float]] = set()
    for wire in wires:
        points = connector_signal_points(connector, position, wire.link.connector_net)
        if len(points) == 1 and abs(points[0].y - wire.owner_pin.y) < 1e-6:
            straight.add((wire.link.connector_net, points[0].y))
    return len(straight) >= 3


def other_terminal(component: Component, terminal: str) -> str | None:
    if len(component.terminals) != 2 or terminal not in component.terminals:
        return None
    return next(candidate for candidate in component.terminals if candidate != terminal)


def terminal_for_net(component: Component, net_ref: str) -> str | None:
    terminals = [
        terminal
        for terminal, (candidate_ref, _) in component.terminals.items()
        if candidate_ref == net_ref
    ]
    return terminals[0] if len(terminals) == 1 else None


def role_terminal(component: Component, local_net_name: str) -> str:
    matches = [
        terminal
        for terminal, (net_ref, net) in component.terminals.items()
        if str(net.get("name", net_ref)).rsplit(".", 1)[-1] == local_net_name
    ]
    if len(matches) != 1:
        raise ToolchainError(
            f"{component.ref}: role net {local_net_name!r} must name exactly one terminal"
        )
    return matches[0]


def component_drawing_envelope(component: Component, position: Position) -> Envelope:
    bounds = placed_symbol_bounds(component.instance, position)
    return with_annotation_envelope(
        Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y),
        (str(component.instance.get("reference_designator", "")),
         attribute_string(component.instance, "value") or ""),
    )


def drawings_overlap(a: Envelope, b: Envelope) -> bool:
    return (a.min_x < b.max_x + 30 and b.min_x < a.max_x + 30
            and a.min_y < b.max_y + 30 and b.min_y < a.max_y + 30)
