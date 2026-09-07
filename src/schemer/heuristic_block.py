"""Small, deterministic placement rules for one connector/IC functional block."""

from __future__ import annotations

from dataclasses import dataclass, replace
from statistics import median
from typing import Any

from schemer.blocks import (
    BlockPlan,
    LayoutBlock,
    PlacedBlock,
    Rect,
    block_from_positions,
    compose_column,
    compose_row,
)
from schemer.hints import Endpoint, HintSet
from schemer.layout import ModuleLayout, Position
from schemer.layout_metrics import Envelope, _with_annotation_envelope
from schemer.signal_terminations import SYMBOL as SIGNAL_TERMINATION_SYMBOL
from schemer.symbol_geometry import (
    _NON_OWNER_TYPES,
    PlacedBounds,
    Point,
    _attribute_string,
    _is_rail_net,
    _is_return_net,
    _module_boundary_net_names,
    _port_component_ref,
    _terminal_nets,
    pin_outward_side,
    pin_positions,
    placed_symbol_body_bounds,
    placed_symbol_bounds,
    position_net_symbol_pin,
    symbol_pin_electrical_types,
)
from schemer.toolchain import ToolchainError

_SIGNAL_STUB = 240.0
_CONNECTOR_STUB = 240.0
_SERIES_PITCH = 100.0
_SERIES_ANNOTATION_PITCH = 90.0
_SERIES_LANE_OFFSET = 50.0
_STAGGER = 100.0
_LOCAL_GAP = 20.0
_NET_SYMBOL_STUB = 80.0
_LOCAL_RAIL_STUB = 40.0
# Installed viewer's minimum outward pin escape: 50 mil = 1.27 mm.
# A fan-in trunk starts here, not on the bank's physical pin endpoints.
_PIN_EXIT_STUB = 12.7
_PIN_CLUSTER_GAP = 60.0
_DEVICE_STUB = 180.0
_BRANCH_STUB = 80.0
_LOCAL_BRANCH_SPAN = 3 * _NET_SYMBOL_STUB


@dataclass(frozen=True)
class _Component:
    ref: str
    symbol_id: str
    instance: dict[str, Any]
    component_type: str
    terminals: dict[str, tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class _SeriesLink:
    passive: _Component
    connector: _Component
    central_terminal: str
    connector_net: str
    connector_terminal: str
    passive_central_terminal: str
    passive_connector_terminal: str


@dataclass(frozen=True)
class _SeriesWire:
    """One electrical lane derived from a real IC pin before symbols are placed."""

    link: _SeriesLink
    side: str
    owner_pin: Point
    passive_pin_target: Point


@dataclass(frozen=True)
class _BoundaryChannel:
    """One active endpoint, its inline passive, and its public signal net."""

    active: _Component
    series: _Component
    active_terminal: str
    series_active_terminal: str
    series_boundary_terminal: str
    boundary_net_ref: str
    boundary_net: dict[str, Any]
    bypass: _Component


@dataclass(frozen=True)
class _ParallelLink:
    """One output pin and inline passive feeding a common downstream net."""

    passive: _Component
    owner_terminal: str
    passive_owner_terminal: str
    passive_common_terminal: str


@dataclass(frozen=True)
class _TransformerChain:
    """A parallel active driver followed by shunt, transformer, and connector."""

    driver: _Component
    input_net_ref: str
    input_net: dict[str, Any]
    input_terminals: tuple[str, ...]
    links: tuple[_ParallelLink, ...]
    common_net_ref: str
    shunt: _Component
    shunt_common_terminal: str
    shunt_return_terminal: str
    transformer: _Component
    transformer_common_terminal: str
    transformer_return_terminal: str
    transformer_signal_terminal: str
    transformer_connector_return_terminal: str
    coupling: _Component
    coupling_transformer_terminal: str
    coupling_connector_terminal: str
    connector: _Component
    connector_signal_terminal: str
    connector_return_terminal: str
    bypass: _Component


@dataclass(frozen=True)
class _NetSymbolAttachment:
    """A wire endpoint to which a one-pin net symbol will later be attached."""

    net_ref: str
    net: dict[str, Any]
    target: Point
    rotation: float = 0.0
    outward_side: str | None = None

    def position(self) -> Position:
        properties = self.net.get("properties")
        if not isinstance(properties, dict) or "__symbol_value" not in properties:
            return Position(self.target.x, self.target.y, rotation=self.rotation)
        return position_net_symbol_pin(
            self.net,
            self.target,
            rotation=self.rotation,
        )


class _NetSymbols:
    def __init__(self) -> None:
        self._next_suffix: dict[str, int] = {}
        self.rail_envelopes: list[Envelope] = []

    def add(
        self,
        positions: dict[str, Position],
        net_ref: str,
        net: dict[str, Any],
        position: Position,
        outward_side: str | None = None,
    ) -> None:
        if _is_rail_net(net_ref, net):
            envelope = _rail_drawing_envelope(net, position)
            properties = net.get("properties", {})
            if "__symbol_value" in properties:
                bounds = placed_symbol_bounds({"attributes": properties}, position)
                # Routing lanes clear glyph bodies; conservative whole-block
                # text allowances must not push attached wires away from pins.
                envelope = Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)
            # Keep north/south glyphs on separate lanes when their bodies
            # would meet. Moving a lateral termination outward retains
            # exactly one right-angle approach; moving it vertically would not.
            if outward_side in {"left", "right"}:
                for previous in sorted(
                    self.rail_envelopes,
                    key=lambda item: item.min_x,
                    reverse=outward_side == "left",
                ):
                    if (
                        envelope.min_y < previous.max_y + _LOCAL_GAP
                        and previous.min_y < envelope.max_y + _LOCAL_GAP
                        and envelope.min_x < previous.max_x + _LOCAL_GAP
                        and previous.min_x < envelope.max_x + _LOCAL_GAP
                    ):
                        delta = (
                            previous.min_x - _LOCAL_GAP - envelope.max_x
                            if outward_side == "left"
                            else previous.max_x + _LOCAL_GAP - envelope.min_x
                        )
                        position = replace(position, x=position.x + delta)
                        envelope = envelope.translated(delta, 0)
            self.rail_envelopes.append(envelope)
        raw_name = str(net.get("name", net_ref)).rsplit(".", 1)[-1]
        suffix = self._next_suffix.get(raw_name, 0)
        self._next_suffix[raw_name] = suffix + 1
        positions[f"sym:{raw_name}#{suffix}"] = position


def _attach_net_symbols(
    positions: dict[str, Position],
    attachments: tuple[_NetSymbolAttachment, ...],
    *,
    symbols: _NetSymbols | None = None,
) -> None:
    """Attach drawings only after all electrical endpoints have been chosen."""

    symbols = symbols or _NetSymbols()
    for attachment in sorted(attachments, key=_attachment_order):
        symbols.add(
            positions,
            attachment.net_ref,
            attachment.net,
            attachment.position(),
            attachment.outward_side,
        )


def _attachment_order(attachment: _NetSymbolAttachment) -> tuple:
    return (
        attachment.outward_side in {"left", "right"},
        attachment.target.y, attachment.target.x, attachment.net_ref,
    )


def _clear_rail_signal_lanes(
    attachments: list[_NetSymbolAttachment],
    lanes: list[tuple[str, Envelope]],
) -> list[_NetSymbolAttachment]:
    """Let lateral rail glyphs drift outward; keep useful signal lanes fixed."""
    result = []
    for attachment in attachments:
        side = attachment.outward_side
        if side in {"left", "right"} and _is_rail_net(attachment.net_ref, attachment.net):
            for lane_side, lane in sorted(lanes, key=lambda item: item[1].min_x,
                                          reverse=side == "left"):
                if lane_side != side:
                    continue
                bounds = placed_symbol_bounds(
                    {"attributes": attachment.net.get("properties", {})}, attachment.position(),
                )
                if (bounds.min_y < lane.max_y and lane.min_y < bounds.max_y
                        and bounds.min_x < lane.max_x and lane.min_x < bounds.max_x):
                    dx = (lane.min_x - _LOCAL_GAP - bounds.max_x if side == "left"
                          else lane.max_x + _LOCAL_GAP - bounds.min_x)
                    attachment = replace(attachment, target=Point(
                        attachment.target.x + dx, attachment.target.y,
                    ))
        result.append(attachment)
    # Keep repeated terminations on this owner's face on one rail lane.
    # Moving just one past its neighbours can make the renderer assign its
    # pin to a different same-net glyph and draw an around-the-pins loop.
    for side in ("left", "right"):
        for net_ref in {item.net_ref for item in result if item.outward_side == side}:
            indices = [i for i, item in enumerate(result)
                       if item.outward_side == side and item.net_ref == net_ref]
            if not indices:
                continue
            coordinate = (min if side == "left" else max)(result[i].target.x for i in indices)
            for i in indices:
                result[i] = replace(result[i], target=Point(coordinate, result[i].target.y))
    return result


def _rail_drawing_envelope(net: dict[str, Any], position: Position) -> Envelope:
    properties = net.get("properties", {})
    if "__symbol_value" in properties:
        bounds = placed_symbol_bounds({"attributes": properties}, position)
        envelope = Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)
    else:
        envelope = Envelope(position.x, position.y, position.x, position.y)
    return _with_annotation_envelope(envelope, (str(net.get("name", "")).rsplit(".", 1)[-1],))


def _shunt_drawing_envelope(
    component: _Component, position: Position, rail_terminal: str,
) -> Envelope:
    bounds = placed_symbol_bounds(component.instance, position)
    envelope = _with_annotation_envelope(
        Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y),
        (str(component.instance.get("reference_designator", "")),
         _attribute_string(component.instance, "value") or ""),
    )
    rail = _terminal_net_symbol_attachment(
        component, position, rail_terminal, clearance=_LOCAL_RAIL_STUB,
    )
    return envelope.union(_rail_drawing_envelope(rail.net, rail.position()))


def _components(
    schematic: dict[str, Any], module: ModuleLayout
) -> tuple[dict[str, Any], dict[str, Any], tuple[_Component, ...]]:
    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    prefix = module.instance_ref + "."
    result: list[_Component] = []
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
            _Component(
                ref,
                f"comp:{local_ref}",
                instance,
                (_attribute_string(instance, "type") or "").casefold(),
                _terminal_nets(ref, instance, nets),
            )
        )
    return instances, nets, tuple(sorted(result, key=lambda item: item.ref))


def _net_components(net: dict[str, Any], component_refs: tuple[str, ...]) -> set[str]:
    return {
        component_ref
        for port_ref in net.get("ports", ())
        if isinstance(port_ref, str)
        if (component_ref := _port_component_ref(port_ref, component_refs)) is not None
    }


def _series_links(
    components: tuple[_Component, ...],
    central: _Component,
    connectors: tuple[_Component, ...],
) -> tuple[_SeriesLink, ...]:
    component_refs = tuple(component.ref for component in components)
    connector_by_ref = {component.ref: component for component in connectors}
    links: list[_SeriesLink] = []
    for passive in components:
        if passive.component_type not in _NON_OWNER_TYPES or len(passive.terminals) != 2:
            continue
        if any(_is_rail_net(net_ref, net) for net_ref, net in passive.terminals.values()):
            continue
        ends: list[tuple[str, str, set[str]]] = []
        for terminal, (net_ref, net) in passive.terminals.items():
            neighbours = _net_components(net, component_refs) - {passive.ref}
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
            _SeriesLink(
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


def _mean_point(points: tuple[Point, ...]) -> Point:
    return Point(
        float(median(point.x for point in points)),
        float(median(point.y for point in points)),
    )


def _central_orientation(
    central: _Component,
    connectors: tuple[_Component, ...],
    links: tuple[_SeriesLink, ...],
) -> Position:
    connector_points: dict[str, list[str]] = {}
    for link in links:
        connector_points.setdefault(link.connector.ref, []).append(link.central_terminal)

    candidates: list[tuple[float, float, Position]] = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        centres = []
        for connector in connectors:
            points = tuple(
                point
                for terminal in connector_points[connector.ref]
                for point in pin_positions(central.instance, position, terminal)
            )
            centres.append(_mean_point(points))
        horizontal = abs(centres[1].x - centres[0].x)
        vertical = abs(centres[1].y - centres[0].y)
        candidates.append((horizontal - vertical, -rotation, position))
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def _connector_net_point(
    connector: _Component,
    position: Position,
    net_ref: str,
) -> Point:
    points = tuple(
        point
        for terminal, (terminal_net_ref, _) in connector.terminals.items()
        if terminal_net_ref == net_ref
        for point in pin_positions(connector.instance, position, terminal)
    )
    if not points:
        raise ToolchainError(f"connector net has no visible pin: {net_ref}")
    return _mean_point(points)


def _connector_orientation(
    connector: _Component,
    links: tuple[_SeriesLink, ...],
    *,
    side: str,
) -> Position:
    candidates: list[tuple[float, float, Position]] = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        bounds = placed_symbol_bounds(connector.instance, position)
        pin_x = float(
            median(
                _connector_net_point(connector, position, link.connector_net).x for link in links
            )
        )
        outward = pin_x - bounds.center_x if side == "left" else bounds.center_x - pin_x
        candidates.append((outward, -rotation, position))
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def _translated(position: Position, point: Point, target: Point) -> Position:
    return replace(
        position,
        x=position.x + target.x - point.x,
        y=position.y + target.y - point.y,
    )


def _oriented_terminal_vector(
    component: _Component,
    attached_terminal: str,
    remote_terminal: str,
    *,
    axis: str,
    direction: float,
) -> Position:
    """Choose a cardinal orientation whose terminal vector follows one axis."""

    candidates: list[tuple[float, float, float, Position]] = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        attached = _mean_point(pin_positions(component.instance, position, attached_terminal))
        remote = _mean_point(pin_positions(component.instance, position, remote_terminal))
        along = remote.x - attached.x if axis == "x" else remote.y - attached.y
        across = remote.y - attached.y if axis == "x" else remote.x - attached.x
        candidates.append((abs(along) - abs(across), direction * along, -rotation, position))
    return max(candidates, key=lambda candidate: candidate[:3])[3]


def _active_orientation_toward(
    component: _Component,
    terminal: str,
    *,
    outward_direction: float,
) -> Position:
    """Orient an active device so one terminal faces its owner horizontally."""

    candidates: list[tuple[float, float, Position]] = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        bounds = placed_symbol_bounds(component.instance, position)
        pin = _mean_point(pin_positions(component.instance, position, terminal))
        inward_score = bounds.center_x - pin.x if outward_direction > 0 else pin.x - bounds.center_x
        candidates.append((inward_score, -rotation, position))
    return max(candidates, key=lambda candidate: candidate[:2])[2]


def _component_pin_for_net(
    component: _Component,
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
    return terminal, _mean_point(pin_positions(component.instance, position, terminal))


def _is_boundary_net(
    net_ref: str,
    net: dict[str, Any],
    boundary_net_names: set[str],
) -> bool:
    return net_ref in boundary_net_names or net.get("name") in boundary_net_names


def _passive_orientation(
    passive: _Component,
    first_terminal: str,
    second_terminal: str,
) -> Position:
    candidates: list[tuple[float, float, Position]] = []
    for rotation in (90.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        first = _mean_point(pin_positions(passive.instance, position, first_terminal))
        second = _mean_point(pin_positions(passive.instance, position, second_terminal))
        candidates.append((second.x - first.x, -rotation, position))
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def _side(point: Point, bounds: PlacedBounds, *, prefer: str | None = None) -> str:
    distances = {
        "left": abs(point.x - bounds.min_x),
        "right": abs(point.x - bounds.max_x),
        "top": abs(point.y - bounds.min_y),
        "bottom": abs(point.y - bounds.max_y),
    }
    minimum = min(distances.values())
    if prefer is not None and distances[prefer] <= minimum + 1.0:
        return prefer
    return min(distances, key=lambda candidate: (distances[candidate], candidate))


def _fan_in_continuation_coordinate(coordinates: tuple[float, ...]) -> float:
    """Keep an external continuation between rows of a multi-branch trunk.

    Landing opposite a middle branch asks for a four-way junction, which the
    current viewer decomposes into two nearly coincident tees.
    """
    rows = sorted(set(coordinates))
    center = float(median(rows))
    if len(rows) >= 3 and center in rows:
        index = rows.index(center)
        return (center + rows[index + 1]) / 2
    return center


def _net_symbol_attachment(
    net_ref: str,
    net: dict[str, Any],
    points: tuple[Point, ...],
    side: str,
    *,
    clearance: float = _NET_SYMBOL_STUB,
) -> _NetSymbolAttachment:
    point = _mean_point(points)
    if len(points) >= 3:
        if side in {"left", "right"}:
            point = Point(point.x, _fan_in_continuation_coordinate(tuple(p.y for p in points)))
        else:
            point = Point(_fan_in_continuation_coordinate(tuple(p.x for p in points)), point.y)
    if side == "left":
        target = Point(point.x - clearance, point.y)
    elif side == "right":
        target = Point(point.x + clearance, point.y)
    elif side == "top":
        target = Point(point.x, point.y - clearance)
    else:
        target = Point(point.x, point.y + clearance)
    if _is_return_net(net_ref, net):
        if side in {"left", "right"}:
            target = Point(target.x, target.y + _LOCAL_RAIL_STUB)
        # A lateral exit then a downward approach takes only one turn.
        # An upward-facing owner pin needs two turns to reach a south-facing
        # ground, so it is the geometric exception to conventional orientation.
        rotation = 180.0 if side == "top" else 0.0
    elif _is_rail_net(net_ref, net):
        if side in {"left", "right"}:
            target = Point(target.x, target.y - _LOCAL_RAIL_STUB)
        rotation = 180.0 if side == "bottom" else 0.0
    else:
        rotation = 0.0
    return _NetSymbolAttachment(net_ref, net, target, rotation, side)


def _clusters(points: tuple[Point, ...], side: str) -> tuple[tuple[Point, ...], ...]:
    unique = {(round(point.x, 6), round(point.y, 6)): point for point in points}
    along = (lambda point: point.y) if side in {"left", "right"} else (lambda point: point.x)
    ordered = sorted(unique.values(), key=lambda point: (along(point), point.x, point.y))
    result: list[list[Point]] = []
    for point in ordered:
        if not result or along(point) - along(result[-1][-1]) > _PIN_CLUSTER_GAP:
            result.append([point])
        else:
            result[-1].append(point)
    return tuple(tuple(cluster) for cluster in result)


def _anchor_net_symbol_attachments(
    component: _Component,
    position: Position,
    *,
    skipped_terminals: set[str] | None = None,
) -> tuple[_NetSymbolAttachment, ...]:
    skipped_terminals = skipped_terminals or set()
    bounds = placed_symbol_bounds(component.instance, position)
    by_net_side: dict[tuple[str, str], list[Point]] = {}
    nets_by_ref: dict[str, dict[str, Any]] = {}
    for terminal, (net_ref, net) in component.terminals.items():
        if terminal in skipped_terminals or not _is_rail_net(net_ref, net):
            continue
        prefer = "bottom" if _is_return_net(net_ref, net) else "top"
        for point in pin_positions(component.instance, position, terminal):
            side = _side(point, bounds, prefer=prefer)
            by_net_side.setdefault((net_ref, side), []).append(point)
            nets_by_ref[net_ref] = net
    result: list[_NetSymbolAttachment] = []
    for (net_ref, side), points in sorted(by_net_side.items()):
        for cluster in _clusters(tuple(points), side):
            net = nets_by_ref[net_ref]
            result.append(_net_symbol_attachment(net_ref, net, cluster, side))
    return tuple(result)


def _terminal_net_symbol_attachment(
    component: _Component,
    position: Position,
    terminal: str,
    *,
    side: str | None = None,
    clearance: float = _NET_SYMBOL_STUB,
) -> _NetSymbolAttachment:
    net_ref, net = component.terminals[terminal]
    points = pin_positions(component.instance, position, terminal)
    bounds = placed_symbol_bounds(component.instance, position)
    if side is None:
        side = _side(_mean_point(points), bounds)
    return _net_symbol_attachment(
        net_ref,
        net,
        points,
        side,
        clearance=clearance,
    )


def _series_wires_from_ic(
    central: _Component,
    central_position: Position,
    links: tuple[_SeriesLink, ...],
    *,
    side: str,
) -> tuple[_SeriesWire, ...]:
    """Derive the final electrical lanes from IC pins before placing symbols."""

    direction = -1.0 if side == "left" else 1.0
    ordered = sorted(
        links,
        key=lambda link: (
            _mean_point(pin_positions(central.instance, central_position, link.central_terminal)).y,
            link.passive.ref,
        ),
    )
    result: list[_SeriesWire] = []
    for link in ordered:
        central_pin = _mean_point(
            pin_positions(central.instance, central_position, link.central_terminal)
        )
        result.append(
            _SeriesWire(
                link,
                side,
                central_pin,
                Point(central_pin.x + direction * _SIGNAL_STUB, central_pin.y),
            )
        )
    return tuple(result)


def _place_series_symbols(wires: tuple[_SeriesWire, ...]) -> dict[str, Position]:
    """Attach each series component to its already selected IC wire lane."""

    result: dict[str, Position] = {}
    for wire in wires:
        link = wire.link
        if wire.side == "left":
            first_terminal = link.passive_connector_terminal
            second_terminal = link.passive_central_terminal
        else:
            first_terminal = link.passive_central_terminal
            second_terminal = link.passive_connector_terminal
        passive_position = _passive_orientation(
            link.passive,
            first_terminal,
            second_terminal,
        )
        central_pin = _mean_point(
            pin_positions(link.passive.instance, passive_position, link.passive_central_terminal)
        )
        passive_position = _translated(
            passive_position,
            central_pin,
            wire.passive_pin_target,
        )
        result[link.passive.symbol_id] = passive_position
    return result


def _connector_signal_points(
    connector: _Component, position: Position, net_ref: str,
) -> tuple[Point, ...]:
    points = {
        (point.x, point.y): point
        for terminal, (candidate, _) in connector.terminals.items() if candidate == net_ref
        for point in pin_positions(connector.instance, position, terminal)
    }
    return tuple(sorted(points.values(), key=lambda point: (point.y, point.x)))


def _has_straight_bundle(
    connector: _Component, position: Position, wires: tuple[_SeriesWire, ...],
) -> bool:
    """Independent blocks need at least three one-to-one straight lanes."""
    straight: set[tuple[str, float]] = set()
    for wire in wires:
        points = _connector_signal_points(connector, position, wire.link.connector_net)
        if len(points) == 1 and abs(points[0].y - wire.owner_pin.y) < 1e-6:
            straight.add((wire.link.connector_net, points[0].y))
    return len(straight) >= 3


def _named_signal_attachment(
    component: _Component, position: Position, terminal: str, side: str,
) -> _NetSymbolAttachment:
    net_ref, net = component.terminals[terminal]
    presentation_net = {
        **net,
        "properties": {**net.get("properties", {}), "__symbol_value": SIGNAL_TERMINATION_SYMBOL},
    }
    attachment = _net_symbol_attachment(
        net_ref, presentation_net, pin_positions(component.instance, position, terminal), side,
    )
    # The neutral symbol uses a zero-length vertical pin, just like a rail
    # attachment, but its rotation is purely the signal's outward direction.
    return replace(attachment, rotation=90 if side == "right" else 270)


def _place_connector(
    connector: _Component,
    wires: tuple[_SeriesWire, ...],
    passive_positions: dict[str, Position],
    *,
    side: str,
) -> Position:
    links = tuple(wire.link for wire in wires)
    position = _connector_orientation(connector, links, side=side)
    bounds = placed_symbol_bounds(connector.instance, position)
    passive_pins = [
        _mean_point(
            pin_positions(
                link.passive.instance,
                passive_positions[link.passive.symbol_id],
                link.passive_connector_terminal,
            )
        )
        for link in links
    ]
    if side == "left":
        target_edge = min(point.x for point in passive_pins) - _CONNECTOR_STUB
        delta_x = target_edge - bounds.max_x
    else:
        target_edge = max(point.x for point in passive_pins) + _CONNECTOR_STUB
        delta_x = target_edge - bounds.min_x
    # Register the first signal lane, not an average between incompatible
    # connector/IC pin orders. Averaging leaves every connection misaligned.
    first_wire = min(wires, key=lambda wire: wire.owner_pin.y)
    delta_y = (
        first_wire.owner_pin.y
        - _connector_net_point(connector, position, first_wire.link.connector_net).y
    )
    return replace(position, x=position.x + delta_x, y=position.y + delta_y)


def _apply_series_annotation_clearance(
    wires: tuple[_SeriesWire, ...],
    passive_positions: dict[str, Position],
    *,
    side: str,
    obstacles: tuple[Envelope, ...] = (),
) -> dict[str, Position]:
    """Resolve text crowding last by moving symbols only along their wire lanes.

    When the endpoint orders oppose one another, one bend per link is
    unavoidable. The owner endpoint remains straight and crowded parts use
    staggered x distances; annotation clearance cannot change a wire's y.
    """

    targets = [(wire.link, wire.owner_pin.y) for wire in wires]
    crowded = any(
        abs(first[1] - second[1]) < _SERIES_PITCH
        for index, first in enumerate(targets)
        for second in targets[index + 1 :]
    )
    ordered = sorted(targets, key=lambda item: (item[1], item[0].passive.ref))
    result = dict(passive_positions)
    for index, (link, target_y) in enumerate(ordered):
        position = result[link.passive.symbol_id]
        central_pin = _mean_point(
            pin_positions(
                link.passive.instance,
                position,
                link.passive_central_terminal,
            )
        )
        stagger = (index - (len(ordered) - 1) / 2) * _STAGGER if crowded else 0.0
        if side == "left":
            stagger = -stagger
        result[link.passive.symbol_id] = replace(
            position,
            x=position.x + stagger,
            y=position.y,
        )
        # The connector's local return bank is already complete. A series
        # body may slide along its wire, but must not occupy that
        # bank's ground glyph or label. Preserve the electrical row.
        for obstacle in sorted(obstacles, key=lambda item: item.min_x, reverse=side == "right"):
            placed = result[link.passive.symbol_id]
            bounds = placed_symbol_bounds(link.passive.instance, placed)
            envelope = Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y)
            if (envelope.min_y < obstacle.max_y + _LOCAL_GAP
                    and obstacle.min_y < envelope.max_y + _LOCAL_GAP
                    and envelope.min_x < obstacle.max_x + _LOCAL_GAP
                    and obstacle.min_x < envelope.max_x + _LOCAL_GAP):
                delta = (obstacle.max_x + _LOCAL_GAP - envelope.min_x if side == "left"
                         else obstacle.min_x - _LOCAL_GAP - envelope.max_x)
                result[link.passive.symbol_id] = replace(placed, x=placed.x + delta)
        if abs(central_pin.y - target_y) > 1e-6:
            raise ToolchainError("annotation clearance cannot move a series symbol off its wire")
    return result


def _place_shunts(
    components: tuple[_Component, ...],
    connectors: tuple[_Component, ...],
    connector_positions: dict[str, Position],
    positions: dict[str, Position],
) -> tuple[set[str], tuple[_NetSymbolAttachment, ...]]:
    component_refs = tuple(component.ref for component in components)
    used: set[str] = set()
    attachments: list[_NetSymbolAttachment] = []
    for connector in connectors:
        branches: list[tuple[_Component, str, str, float]] = []
        connector_position = connector_positions[connector.ref]
        for passive in components:
            if passive.component_type not in _NON_OWNER_TYPES or len(passive.terminals) != 2:
                continue
            signal = [
                (terminal, net_ref, net)
                for terminal, (net_ref, net) in passive.terminals.items()
                if not _is_rail_net(net_ref, net)
            ]
            returns = [
                (terminal, net_ref, net)
                for terminal, (net_ref, net) in passive.terminals.items()
                if _is_rail_net(net_ref, net)
            ]
            if len(signal) != 1 or len(returns) != 1:
                continue
            neighbours = _net_components(signal[0][2], component_refs) - {passive.ref}
            if neighbours != {connector.ref}:
                continue
            source_y = _connector_net_point(connector, connector_position, signal[0][1]).y
            branches.append((passive, signal[0][0], returns[0][0], source_y))
        if not branches:
            continue
        bounds = placed_symbol_bounds(connector.instance, connector_position)
        ordered = sorted(branches, key=lambda branch: (branch[3], branch[0].ref))
        source_sides = {
            passive.ref: _side(
                _connector_net_point(
                    connector,
                    connector_position,
                    passive.terminals[signal_terminal][0],
                ),
                bounds,
            )
            for passive, signal_terminal, _, _ in ordered
        }
        return_groups: dict[tuple[str, str], list[tuple[_Component, Position, str]]] = {}
        for passive, signal_terminal, return_terminal, source_y in ordered:
            source_side = source_sides[passive.ref]
            if source_side == "left":
                base = _passive_orientation(passive, return_terminal, signal_terminal)
                target_x = bounds.min_x - _LOCAL_RAIL_STUB
            else:
                base = _passive_orientation(passive, signal_terminal, return_terminal)
                target_x = bounds.max_x + _LOCAL_RAIL_STUB
            signal_pin = _mean_point(pin_positions(passive.instance, base, signal_terminal))
            target = Point(target_x, source_y)
            placed = _translated(base, signal_pin, target)
            positions[passive.symbol_id] = placed
            return_net_ref = passive.terminals[return_terminal][0]
            return_groups.setdefault((return_net_ref, source_side), []).append(
                (passive, placed, return_terminal)
            )
            used.add(passive.ref)
        for (_, source_side), group in sorted(return_groups.items()):
            if len(group) == 1:
                passive, placed, return_terminal = group[0]
                attachments.append(
                    _terminal_net_symbol_attachment(
                        passive,
                        placed,
                        return_terminal,
                        clearance=_LOCAL_RAIL_STUB,
                    )
                )
                continue
            return_points = tuple(
                _mean_point(pin_positions(passive.instance, placed, return_terminal))
                for passive, placed, return_terminal in group
            )
            trunk_x = (
                min(point.x for point in return_points) - _LOCAL_RAIL_STUB
                if source_side == "left"
                else max(point.x for point in return_points) + _LOCAL_RAIL_STUB
            )
            passive, _, return_terminal = group[0]
            net_ref, net = passive.terminals[return_terminal]
            target = Point(
                trunk_x,
                # End the bank just beyond its last branch. An unrelated
                # full-length net stub can run into the following pin bank.
                max(point.y for point in return_points) + _PIN_EXIT_STUB
                if _is_return_net(net_ref, net)
                else min(point.y for point in return_points) - _PIN_EXIT_STUB,
            )
            attachments.append(_NetSymbolAttachment(net_ref, net, target))
    return used, tuple(attachments)


def _place_bypasses(
    components: tuple[_Component, ...],
    central: _Component,
    central_position: Position,
    positions: dict[str, Position],
) -> tuple[set[str], set[str], tuple[_NetSymbolAttachment, ...]]:
    used: set[str] = set()
    owner_terminals_used: set[str] = set()
    attachments: list[_NetSymbolAttachment] = []
    central_bounds = placed_symbol_bounds(central.instance, central_position)
    central_nets = {net_ref for net_ref, _ in central.terminals.values()}
    for passive in components:
        if passive.component_type != "capacitor" or len(passive.terminals) != 2:
            continue
        supply = [
            (terminal, net_ref, net)
            for terminal, (net_ref, net) in passive.terminals.items()
            if _is_rail_net(net_ref, net) and not _is_return_net(net_ref, net)
        ]
        returns = [
            (terminal, net_ref, net)
            for terminal, (net_ref, net) in passive.terminals.items()
            if _is_return_net(net_ref, net)
        ]
        if len(supply) != 1 or len(returns) != 1 or {supply[0][1], returns[0][1]} - central_nets:
            continue
        electrical_types = symbol_pin_electrical_types(central.instance)
        owner_terminals = [
            terminal
            for terminal, (net_ref, _) in central.terminals.items()
            if net_ref == supply[0][1] and electrical_types.get(terminal) == "power_in"
        ]
        if not owner_terminals:
            owner_terminals = [
                terminal
                for terminal, (net_ref, _) in central.terminals.items()
                if net_ref == supply[0][1]
            ]
        owner_points = tuple(
            point
            for terminal, (net_ref, _) in central.terminals.items()
            if terminal in owner_terminals and net_ref == supply[0][1]
            for point in pin_positions(central.instance, central_position, terminal)
        )
        owner_point = _mean_point(owner_points)
        owner_side = _side(owner_point, central_bounds)
        side = "left" if owner_side == "left" else "right"
        clustered_owner_terminals = set(owner_terminals)
        # A rail can serve separated banks on the same face. Put its local
        # bypass at the bottom bank (ground leaves downward), and terminate
        # the other bank independently instead of drawing a trunk across
        # intervening signal pins. Never average across those banks.
        same_face = {
            terminal: _mean_point(pin_positions(central.instance, central_position, terminal))
            for terminal, (net_ref, _) in central.terminals.items()
            if net_ref == supply[0][1]
            and _side(_mean_point(pin_positions(
                central.instance, central_position, terminal,
            )), central_bounds) == owner_side
        }
        face_clusters = _clusters(tuple(same_face.values()), side)
        separate_banks = owner_side in {"left", "right"} and len(face_clusters) > 1
        if separate_banks:
            cluster = face_clusters[-1]
            owner_point = max(cluster, key=lambda point: point.y)
            clustered_owner_terminals = {
                terminal for terminal, point in same_face.items() if point in cluster
            }
        owner_along = owner_point.y if side in {"left", "right"} else owner_point.x
        for terminal, (net_ref, _) in central.terminals.items():
            if net_ref != supply[0][1]:
                continue
            terminal_point = _mean_point(
                pin_positions(central.instance, central_position, terminal)
            )
            if _side(terminal_point, central_bounds) != owner_side:
                continue
            terminal_along = terminal_point.y if side in {"left", "right"} else terminal_point.x
            if abs(terminal_along - owner_along) <= _PIN_CLUSTER_GAP:
                clustered_owner_terminals.add(terminal)
        if side == "left":
            base = _passive_orientation(passive, returns[0][0], supply[0][0])
            target_x = central_bounds.min_x - _LOCAL_GAP
        else:
            base = _passive_orientation(passive, supply[0][0], returns[0][0])
            target_x = central_bounds.max_x + _LOCAL_GAP
        supply_pin = _mean_point(pin_positions(passive.instance, base, supply[0][0]))
        placed = _translated(
            base,
            supply_pin,
            Point(target_x, owner_point.y),
        )
        # A bypass is a complete local branch, not just a capacitor body.
        # Move the whole branch outward if its return glyph/caption would
        # encroach on a neighbouring named signal endpoint on this face.
        return_attachment = _terminal_net_symbol_attachment(
            passive, placed, returns[0][0], clearance=_LOCAL_RAIL_STUB,
        )
        branch_return = _rail_drawing_envelope(
            return_attachment.net, return_attachment.position(),
        )
        delta_x = 0.0
        for terminal, (net_ref, net) in central.terminals.items():
            if _is_rail_net(net_ref, net) or net.get("kind") == "NotConnected":
                continue
            for point in pin_positions(central.instance, central_position, terminal):
                if _side(point, central_bounds) != side:
                    continue
                endpoint_x = point.x + (-1 if side == "left" else 1) * _NET_SYMBOL_STUB
                endpoint = _with_annotation_envelope(
                    Envelope(endpoint_x, point.y, endpoint_x, point.y), (terminal,),
                )
                if branch_return.min_y < endpoint.max_y and endpoint.min_y < branch_return.max_y:
                    if side == "right":
                        delta_x = max(delta_x, endpoint.max_x + _LOCAL_GAP - branch_return.min_x)
                    else:
                        delta_x = min(delta_x, endpoint.min_x - _LOCAL_GAP - branch_return.max_x)
        placed = replace(placed, x=placed.x + delta_x)
        if separate_banks:
            # Reserve an outer branch lane beyond the short signal stubs.
            # Its local supply tee must not sit on a signal endpoint row.
            current_pin = _mean_point(pin_positions(passive.instance, placed, supply[0][0]))
            direction = -1 if side == "left" else 1
            reach = _NET_SYMBOL_STUB + 3 * _LOCAL_RAIL_STUB
            target_x = direction * max(direction * current_pin.x,
                                       direction * owner_point.x + reach)
            placed = replace(placed, x=placed.x + target_x - current_pin.x)
            attachments.append(_NetSymbolAttachment(
                supply[0][1], supply[0][2],
                Point(target_x - direction * _LOCAL_RAIL_STUB,
                      owner_point.y - _LOCAL_RAIL_STUB),
                outward_side=side,
            ))
        positions[passive.symbol_id] = placed
        attachments.append(
            _terminal_net_symbol_attachment(
                passive,
                placed,
                returns[0][0],
                clearance=_LOCAL_RAIL_STUB,
            )
        )
        used.add(passive.ref)
        owner_terminals_used.update(clustered_owner_terminals)
    return used, owner_terminals_used, tuple(attachments)


def _single_ended_net_symbol_attachments(
    component: _Component,
    position: Position,
    component_refs: tuple[str, ...],
) -> tuple[_NetSymbolAttachment, ...]:
    result: list[_NetSymbolAttachment] = []
    for terminal, (net_ref, net) in component.terminals.items():
        name = str(net.get("name", net_ref)).rsplit(".", 1)[-1]
        if _is_rail_net(net_ref, net) or name.startswith("NC_"):
            continue
        if _net_components(net, component_refs) == {component.ref}:
            result.append(_terminal_net_symbol_attachment(component, position, terminal))
    return tuple(result)


def _is_not_connected_net(net_ref: str, net: dict[str, Any]) -> bool:
    kind = str(net.get("kind", "")).casefold()
    local_name = str(net.get("name", net_ref)).rsplit(".", 1)[-1].upper()
    return kind == "notconnected" or local_name.startswith("NC_")


def _other_terminal(component: _Component, terminal: str) -> str | None:
    if len(component.terminals) != 2 or terminal not in component.terminals:
        return None
    return next(candidate for candidate in component.terminals if candidate != terminal)


def _terminal_for_net(component: _Component, net_ref: str) -> str | None:
    terminals = [
        terminal
        for terminal, (candidate_ref, _) in component.terminals.items()
        if candidate_ref == net_ref
    ]
    return terminals[0] if len(terminals) == 1 else None


def _local_bypasses(components: tuple[_Component, ...]) -> tuple[_Component, ...]:
    result: list[_Component] = []
    for component in components:
        if component.component_type != "capacitor" or len(component.terminals) != 2:
            continue
        rails = tuple(
            (net_ref, net)
            for net_ref, net in component.terminals.values()
            if _is_rail_net(net_ref, net)
        )
        if len(rails) == 2 and sum(_is_return_net(net_ref, net) for net_ref, net in rails) == 1:
            result.append(component)
    return tuple(sorted(result, key=lambda component: component.ref))


def _assign_local_bypasses(
    active: tuple[_Component, ...],
    bypasses: tuple[_Component, ...],
) -> dict[str, _Component] | None:
    """Match indistinguishable rail shunts to compatible consumers stably.

    Connectivity cannot distinguish two equal decouplers on the same rails.
    Within each rail pair, source-stable component order is therefore used as
    the only tie-break. The rule applies only when consumers and capacitors
    have equal cardinality, so no passive is silently left unowned.
    """

    by_rails: dict[frozenset[str], list[_Component]] = {}
    for bypass in bypasses:
        by_rails.setdefault(
            frozenset(net_ref for net_ref, _ in bypass.terminals.values()), []
        ).append(bypass)

    assigned: dict[str, _Component] = {}
    claimed_active: set[str] = set()
    for rail_pair, candidates in sorted(by_rails.items(), key=lambda item: tuple(sorted(item[0]))):
        consumers = sorted(
            (
                component
                for component in active
                if rail_pair <= {net_ref for net_ref, _ in component.terminals.values()}
                and component.ref not in claimed_active
            ),
            key=lambda component: component.ref,
        )
        candidates.sort(key=lambda component: component.ref)
        if len(consumers) != len(candidates):
            return None
        for consumer, bypass in zip(consumers, candidates, strict=True):
            assigned[consumer.ref] = bypass
            claimed_active.add(consumer.ref)
    if set(assigned) != {component.ref for component in active}:
        return None
    return assigned


def _orientation_for_terminal_sides(
    component: _Component,
    desired_sides: dict[str, str],
) -> Position:
    """Choose the cardinal orientation best matching evidenced pin roles."""

    candidates: list[tuple[int, float, Position]] = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        bounds = placed_symbol_bounds(component.instance, position)
        matches = sum(
            _side(
                _mean_point(pin_positions(component.instance, position, terminal)),
                bounds,
                prefer=side,
            )
            == side
            for terminal, side in desired_sides.items()
        )
        candidates.append((matches, -rotation, position))
    return max(candidates, key=lambda candidate: candidate[:2])[2]


def _consolidated_rail_attachments(
    component: _Component,
    position: Position,
) -> tuple[_NetSymbolAttachment, ...]:
    """Terminate each same-net, same-face pin group with one local symbol."""

    bounds = placed_symbol_bounds(component.instance, position)
    grouped: dict[tuple[str, str], list[Point]] = {}
    nets: dict[str, dict[str, Any]] = {}
    for terminal, (net_ref, net) in component.terminals.items():
        if not _is_rail_net(net_ref, net):
            continue
        preferred = "bottom" if _is_return_net(net_ref, net) else "top"
        for point in pin_positions(component.instance, position, terminal):
            side = _side(point, bounds, prefer=preferred)
            grouped.setdefault((net_ref, side), []).append(point)
            nets[net_ref] = net
    return tuple(
        _net_symbol_attachment(net_ref, nets[net_ref], tuple(points), side)
        for (net_ref, side), points in sorted(grouped.items())
    )


def _component_envelopes(
    components: tuple[_Component, ...],
    positions: dict[str, Position],
) -> dict[str, Rect]:
    occupied: dict[str, Rect] = {}
    for component in components:
        bounds = placed_symbol_body_bounds(component.instance, positions[component.symbol_id])
        occupied[component.symbol_id] = Rect(
            bounds.min_x,
            bounds.min_y,
            bounds.max_x - bounds.min_x,
            bounds.max_y - bounds.min_y,
        )
    return occupied


def _place_local_bypass(
    owner: _Component,
    owner_position: Position,
    bypass: _Component,
    positions: dict[str, Position],
) -> tuple[_NetSymbolAttachment, ...]:
    supply = next(
        terminal
        for terminal, (net_ref, net) in bypass.terminals.items()
        if _is_rail_net(net_ref, net) and not _is_return_net(net_ref, net)
    )
    return_terminal = next(
        terminal
        for terminal, (net_ref, net) in bypass.terminals.items()
        if _is_return_net(net_ref, net)
    )
    base = _oriented_terminal_vector(
        bypass,
        supply,
        return_terminal,
        axis="y",
        direction=1.0,
    )
    supply_net_ref = bypass.terminals[supply][0]
    owner_supply_points = tuple(
        point
        for terminal, (net_ref, _) in owner.terminals.items()
        if net_ref == supply_net_ref
        for point in pin_positions(owner.instance, owner_position, terminal)
    )
    owner_supply = _mean_point(owner_supply_points)
    owner_bounds = placed_symbol_bounds(owner.instance, owner_position)
    supply_pin = _mean_point(pin_positions(bypass.instance, base, supply))
    placed = _translated(
        base,
        supply_pin,
        Point(owner_bounds.max_x + _LOCAL_RAIL_STUB, owner_supply.y - _LOCAL_RAIL_STUB),
    )
    positions[bypass.symbol_id] = placed
    return (
        _terminal_net_symbol_attachment(
            bypass, placed, return_terminal, side="bottom", clearance=_LOCAL_RAIL_STUB
        ),
    )


def _boundary_channel(
    active: _Component,
    components: tuple[_Component, ...],
    boundary_net_names: set[str],
    bypass: _Component,
) -> _BoundaryChannel | None:
    component_refs = tuple(component.ref for component in components)
    signals = [
        (terminal, net_ref, net)
        for terminal, (net_ref, net) in active.terminals.items()
        if not _is_rail_net(net_ref, net) and not _is_not_connected_net(net_ref, net)
    ]
    if len(signals) != 1:
        return None
    active_terminal, signal_ref, signal_net = signals[0]
    peers = _net_components(signal_net, component_refs) - {active.ref}
    if len(peers) != 1:
        return None
    series = next((component for component in components if component.ref in peers), None)
    if (
        series is None
        or series.component_type not in _NON_OWNER_TYPES
        or len(series.terminals) != 2
    ):
        return None
    series_active_terminal = _terminal_for_net(series, signal_ref)
    if series_active_terminal is None:
        return None
    series_boundary_terminal = _other_terminal(series, series_active_terminal)
    if series_boundary_terminal is None:
        return None
    boundary_ref, boundary_net = series.terminals[series_boundary_terminal]
    if not _is_boundary_net(boundary_ref, boundary_net, boundary_net_names):
        return None
    return _BoundaryChannel(
        active,
        series,
        active_terminal,
        series_active_terminal,
        series_boundary_terminal,
        boundary_ref,
        boundary_net,
        bypass,
    )


def _parallel_transformer_chain(
    driver: _Component,
    components: tuple[_Component, ...],
    boundary_net_names: set[str],
    bypass: _Component,
) -> _TransformerChain | None:
    component_refs = tuple(component.ref for component in components)
    by_ref = {component.ref: component for component in components}
    boundary_inputs: dict[str, list[str]] = {}
    boundary_nets: dict[str, dict[str, Any]] = {}
    for terminal, (net_ref, net) in driver.terminals.items():
        if (
            not _is_rail_net(net_ref, net)
            and not _is_not_connected_net(net_ref, net)
            and _is_boundary_net(net_ref, net, boundary_net_names)
        ):
            boundary_inputs.setdefault(net_ref, []).append(terminal)
            boundary_nets[net_ref] = net
    if len(boundary_inputs) != 1:
        return None
    input_net_ref, input_terminals = next(iter(boundary_inputs.items()))
    if len(input_terminals) < 2:
        return None

    links: list[_ParallelLink] = []
    common_refs: set[str] = set()
    for owner_terminal, (net_ref, net) in driver.terminals.items():
        if (
            _is_rail_net(net_ref, net)
            or _is_not_connected_net(net_ref, net)
            or _is_boundary_net(net_ref, net, boundary_net_names)
        ):
            continue
        peers = _net_components(net, component_refs) - {driver.ref}
        if len(peers) != 1:
            return None
        passive = by_ref[next(iter(peers))]
        if passive.component_type not in _NON_OWNER_TYPES or len(passive.terminals) != 2:
            return None
        passive_owner_terminal = _terminal_for_net(passive, net_ref)
        if passive_owner_terminal is None:
            return None
        passive_common_terminal = _other_terminal(passive, passive_owner_terminal)
        if passive_common_terminal is None:
            return None
        common_refs.add(passive.terminals[passive_common_terminal][0])
        links.append(
            _ParallelLink(
                passive,
                owner_terminal,
                passive_owner_terminal,
                passive_common_terminal,
            )
        )
    if len(links) < 2 or len(common_refs) != 1:
        return None
    common_net_ref = common_refs.pop()

    shunts = []
    transformers = []
    for component in components:
        common_terminal = _terminal_for_net(component, common_net_ref)
        if common_terminal is None:
            continue
        if component.component_type == "transformer":
            transformers.append((component, common_terminal))
            continue
        remote_terminal = _other_terminal(component, common_terminal)
        if remote_terminal is not None and _is_return_net(*component.terminals[remote_terminal]):
            shunts.append((component, common_terminal, remote_terminal))
    if len(shunts) != 1 or len(transformers) != 1:
        return None
    shunt, shunt_common_terminal, shunt_return_terminal = shunts[0]
    transformer, transformer_common_terminal = transformers[0]
    transformer_return_terminals = [
        terminal
        for terminal, (net_ref, net) in transformer.terminals.items()
        if terminal != transformer_common_terminal and _is_return_net(net_ref, net)
    ]
    if len(transformer_return_terminals) != 1:
        return None
    transformer_return_terminal = transformer_return_terminals[0]

    connectors = [
        component
        for component in components
        if component.component_type == "connector" and len(component.terminals) == 2
    ]
    if len(connectors) != 1:
        return None
    connector = connectors[0]
    secondary = [
        terminal
        for terminal in transformer.terminals
        if terminal not in {transformer_common_terminal, transformer_return_terminal}
    ]
    if len(secondary) != 2:
        return None

    direct: tuple[str, str] | None = None
    coupled: tuple[str, _Component, str, str, str] | None = None
    for terminal in secondary:
        net_ref, net = transformer.terminals[terminal]
        peers = _net_components(net, component_refs) - {transformer.ref}
        if peers == {connector.ref}:
            connector_terminal = _terminal_for_net(connector, net_ref)
            if connector_terminal is not None:
                direct = (terminal, connector_terminal)
            continue
        if len(peers) != 1:
            continue
        coupling = by_ref[next(iter(peers))]
        coupling_transformer_terminal = _terminal_for_net(coupling, net_ref)
        if coupling_transformer_terminal is None:
            continue
        coupling_connector_terminal = _other_terminal(coupling, coupling_transformer_terminal)
        if coupling_connector_terminal is None:
            continue
        connector_net_ref = coupling.terminals[coupling_connector_terminal][0]
        connector_terminal = _terminal_for_net(connector, connector_net_ref)
        if connector_terminal is not None:
            coupled = (
                terminal,
                coupling,
                coupling_transformer_terminal,
                coupling_connector_terminal,
                connector_terminal,
            )
    if direct is None or coupled is None:
        return None
    return _TransformerChain(
        driver=driver,
        input_net_ref=input_net_ref,
        input_net=boundary_nets[input_net_ref],
        input_terminals=tuple(sorted(input_terminals)),
        links=tuple(sorted(links, key=lambda link: link.passive.ref)),
        common_net_ref=common_net_ref,
        shunt=shunt,
        shunt_common_terminal=shunt_common_terminal,
        shunt_return_terminal=shunt_return_terminal,
        transformer=transformer,
        transformer_common_terminal=transformer_common_terminal,
        transformer_return_terminal=transformer_return_terminal,
        transformer_signal_terminal=coupled[0],
        transformer_connector_return_terminal=direct[0],
        coupling=coupled[1],
        coupling_transformer_terminal=coupled[2],
        coupling_connector_terminal=coupled[3],
        connector=connector,
        connector_signal_terminal=coupled[4],
        connector_return_terminal=direct[1],
        bypass=bypass,
    )


def _boundary_channel_block(
    channel: _BoundaryChannel,
    symbols: _NetSymbols,
    *,
    block_id: str,
    padding: float,
) -> LayoutBlock:
    rail_sides = {
        terminal: ("bottom" if _is_return_net(net_ref, net) else "top")
        for terminal, (net_ref, net) in channel.active.terminals.items()
        if _is_rail_net(net_ref, net)
    }
    active_position = _orientation_for_terminal_sides(
        channel.active,
        {channel.active_terminal: "left", **rail_sides},
    )
    positions = {channel.active.symbol_id: active_position}
    active_pin = _mean_point(
        pin_positions(channel.active.instance, active_position, channel.active_terminal)
    )
    base = _oriented_terminal_vector(
        channel.series,
        channel.series_active_terminal,
        channel.series_boundary_terminal,
        axis="x",
        direction=-1.0,
    )
    series_pin = _mean_point(
        pin_positions(channel.series.instance, base, channel.series_active_terminal)
    )
    series_position = _translated(
        base,
        series_pin,
        Point(active_pin.x - 120.0, active_pin.y),
    )
    positions[channel.series.symbol_id] = series_position
    attachments = list(_consolidated_rail_attachments(channel.active, active_position))
    attachments.extend(
        _place_local_bypass(
            channel.active,
            active_position,
            channel.bypass,
            positions,
        )
    )
    attachments.append(
        _terminal_net_symbol_attachment(
            channel.series,
            series_position,
            channel.series_boundary_terminal,
            side="left",
        )
    )
    symbols.rail_envelopes = []
    _attach_net_symbols(positions, tuple(attachments), symbols=symbols)
    members = (channel.active, channel.series, channel.bypass)
    return block_from_positions(
        block_id,
        positions,
        occupied=_component_envelopes(members, positions),
        padding=padding,
    )


def _transformer_chain_block(
    chain: _TransformerChain,
    symbols: _NetSymbols,
    *,
    padding: float,
    hints: HintSet,
) -> LayoutBlock:
    # Select semantic relationships before creating electrical lanes. Only
    # the recognized local secondary circuit can consume this return pairing.
    local_return = hints.take(
        "local-return",
        Endpoint(
            chain.transformer.symbol_id.removeprefix("comp:"),
            chain.transformer_connector_return_terminal,
        ),
        Endpoint(chain.connector.symbol_id.removeprefix("comp:"), chain.connector_return_terminal),
    )
    pin_exit = hints.take(
        "pin-exit",
        Endpoint(
            chain.transformer.symbol_id.removeprefix("comp:"), chain.transformer_return_terminal
        ),
    )
    # A local return requires the paired outgoing chain to stay within a
    # local wiring span too. The normal short branch stub is generator policy;
    # no distance or coordinate is supplied by a hint.
    secondary_stub = _LOCAL_RAIL_STUB if local_return else 120.0
    connector_stub = _LOCAL_RAIL_STUB if local_return else 180.0
    electrical_types = symbol_pin_electrical_types(chain.driver.instance)
    desired = {terminal: "left" for terminal in chain.input_terminals}
    desired.update({link.owner_terminal: "right" for link in chain.links})
    desired.update(
        {
            terminal: ("bottom" if _is_return_net(net_ref, net) else "top")
            for terminal, (net_ref, net) in chain.driver.terminals.items()
            if _is_rail_net(net_ref, net) and electrical_types.get(terminal) == "power_in"
        }
    )
    driver_position = _orientation_for_terminal_sides(chain.driver, desired)
    positions = {chain.driver.symbol_id: driver_position}

    remote_points: list[Point] = []
    for link in sorted(
        chain.links,
        key=lambda candidate: (
            _mean_point(
                pin_positions(
                    chain.driver.instance,
                    driver_position,
                    candidate.owner_terminal,
                )
            ).y
        ),
    ):
        owner_pin = _mean_point(
            pin_positions(chain.driver.instance, driver_position, link.owner_terminal)
        )
        base = _oriented_terminal_vector(
            link.passive,
            link.passive_owner_terminal,
            link.passive_common_terminal,
            axis="x",
            direction=1.0,
        )
        passive_pin = _mean_point(
            pin_positions(link.passive.instance, base, link.passive_owner_terminal)
        )
        placed = _translated(
            base,
            passive_pin,
            Point(owner_pin.x + 120.0, owner_pin.y),
        )
        positions[link.passive.symbol_id] = placed
        remote_points.append(
            _mean_point(
                pin_positions(
                    link.passive.instance,
                    placed,
                    link.passive_common_terminal,
                )
            )
        )

    common_x = float(median(point.x for point in remote_points))
    # Continue from the top of the bank; the shunt leaves from its bottom.
    # A continuation opposite an interior branch creates a four-way node.
    common_y = min(point.y for point in remote_points)
    transformer_base = _orientation_for_terminal_sides(
        chain.transformer,
        {
            chain.transformer_common_terminal: "left",
            chain.transformer_return_terminal: "left",
            chain.transformer_signal_terminal: "right",
            chain.transformer_connector_return_terminal: "right",
        },
    )
    transformer_common_pin = _mean_point(
        pin_positions(
            chain.transformer.instance,
            transformer_base,
            chain.transformer_common_terminal,
        )
    )
    transformer_position = _translated(
        transformer_base,
        transformer_common_pin,
        Point(common_x + 180.0, common_y),
    )
    positions[chain.transformer.symbol_id] = transformer_position

    shunt_base = _oriented_terminal_vector(
        chain.shunt,
        chain.shunt_common_terminal,
        chain.shunt_return_terminal,
        axis="y",
        direction=1.0,
    )
    shunt_common_pin = _mean_point(
        pin_positions(chain.shunt.instance, shunt_base, chain.shunt_common_terminal)
    )
    shunt_position = _translated(
        shunt_base,
        shunt_common_pin,
        Point(common_x + _PIN_EXIT_STUB, max(point.y for point in remote_points) + 140.0),
    )
    positions[chain.shunt.symbol_id] = shunt_position

    transformer_signal_pin = _mean_point(
        pin_positions(
            chain.transformer.instance,
            transformer_position,
            chain.transformer_signal_terminal,
        )
    )
    coupling_base = _oriented_terminal_vector(
        chain.coupling,
        chain.coupling_transformer_terminal,
        chain.coupling_connector_terminal,
        axis="x",
        direction=1.0,
    )
    coupling_signal_pin = _mean_point(
        pin_positions(
            chain.coupling.instance,
            coupling_base,
            chain.coupling_transformer_terminal,
        )
    )
    coupling_position = _translated(
        coupling_base,
        coupling_signal_pin,
        Point(transformer_signal_pin.x + secondary_stub, transformer_signal_pin.y),
    )
    positions[chain.coupling.symbol_id] = coupling_position
    coupling_connector_pin = _mean_point(
        pin_positions(
            chain.coupling.instance,
            coupling_position,
            chain.coupling_connector_terminal,
        )
    )

    connector_base = _orientation_for_terminal_sides(
        chain.connector,
        {
            chain.connector_signal_terminal: "left",
            chain.connector_return_terminal: "left",
        },
    )
    connector_signal_pin = _mean_point(
        pin_positions(
            chain.connector.instance,
            connector_base,
            chain.connector_signal_terminal,
        )
    )
    connector_position = _translated(
        connector_base,
        connector_signal_pin,
        Point(coupling_connector_pin.x + connector_stub, coupling_connector_pin.y),
    )
    positions[chain.connector.symbol_id] = connector_position

    input_points = tuple(
        point
        for terminal in chain.input_terminals
        for point in pin_positions(chain.driver.instance, driver_position, terminal)
    )
    attachments = [
        _net_symbol_attachment(
            chain.input_net_ref,
            chain.input_net,
            input_points,
            "left",
        ),
        _terminal_net_symbol_attachment(
            chain.shunt,
            shunt_position,
            chain.shunt_return_terminal,
            side="bottom",
            clearance=_LOCAL_RAIL_STUB,
        ),
        _terminal_net_symbol_attachment(
            chain.transformer,
            transformer_position,
            chain.transformer_return_terminal,
            side=(
                pin_outward_side(
                    chain.transformer.instance,
                    transformer_position,
                    chain.transformer_return_terminal,
                )
                if pin_exit
                else "bottom"
            ),
            clearance=_LOCAL_RAIL_STUB if pin_exit else _NET_SYMBOL_STUB,
        ),
    ]
    attachments.extend(_consolidated_rail_attachments(chain.driver, driver_position))
    attachments.extend(
        _place_local_bypass(
            chain.driver,
            driver_position,
            chain.bypass,
            positions,
        )
    )
    symbols.rail_envelopes = []
    _attach_net_symbols(positions, tuple(attachments), symbols=symbols)

    members = (
        chain.driver,
        *(link.passive for link in chain.links),
        chain.shunt,
        chain.transformer,
        chain.coupling,
        chain.connector,
        chain.bypass,
    )
    return block_from_positions(
        "parallel-transformer-chain",
        positions,
        occupied=_component_envelopes(members, positions),
        padding=padding,
    )


def multi_active_interface_block_from_zero(
    schematic: dict[str, Any],
    module: ModuleLayout,
    *,
    padding: float = 40.0,
    hints: HintSet | None = None,
) -> BlockPlan | None:
    """Compose repeated endpoint channels with a parallel transformer chain.

    This intentionally small grammar is recognized solely from evaluated
    topology: several one-signal active endpoints, a unique higher-pin-count
    active with parallel series outputs, a common shunt, a transformer, one
    coupling passive, and a two-pin connector. Existing coordinates never
    participate, and the transform aborts unless every component is owned.
    """

    instances, _, components = _components(schematic, module)
    active = tuple(
        component
        for component in components
        if component.component_type not in _NON_OWNER_TYPES
        and component.component_type != "connector"
    )
    if len(active) < 3:
        return None
    largest_count = max(len(component.terminals) for component in active)
    drivers = tuple(component for component in active if len(component.terminals) == largest_count)
    if len(drivers) != 1 or largest_count < 8:
        return None
    driver = drivers[0]
    endpoint_active = tuple(component for component in active if component != driver)
    bypasses = _local_bypasses(components)
    bypass_by_active = _assign_local_bypasses(active, bypasses)
    if bypass_by_active is None:
        return None
    boundary_net_names = _module_boundary_net_names(instances[module.instance_ref])
    channels = tuple(
        _boundary_channel(
            component,
            components,
            boundary_net_names,
            bypass_by_active[component.ref],
        )
        for component in endpoint_active
    )
    if any(channel is None for channel in channels):
        return None
    resolved_channels = tuple(channel for channel in channels if channel is not None)
    chain = _parallel_transformer_chain(
        driver,
        components,
        boundary_net_names,
        bypass_by_active[driver.ref],
    )
    if chain is None:
        return None

    classified = {
        driver.ref,
        chain.shunt.ref,
        chain.transformer.ref,
        chain.coupling.ref,
        chain.connector.ref,
        chain.bypass.ref,
        *(link.passive.ref for link in chain.links),
    }
    for channel in resolved_channels:
        classified.update({channel.active.ref, channel.series.ref, channel.bypass.ref})
    if classified != {component.ref for component in components}:
        return None

    symbols = _NetSymbols()
    channel_blocks = tuple(
        _boundary_channel_block(
            channel,
            symbols,
            block_id=f"endpoint-channel-{index}",
            padding=padding,
        )
        for index, channel in enumerate(
            sorted(resolved_channels, key=lambda candidate: candidate.active.ref)
        )
    )
    endpoint_bank = compose_column(
        "endpoint-channel-bank",
        channel_blocks,
        gap=120.0,
        alignment="start",
    )
    transformer_block = _transformer_chain_block(
        chain, symbols, padding=padding, hints=hints if hints is not None else HintSet()
    )
    root = compose_row(
        "multi-active-interface",
        (endpoint_bank, transformer_block),
        gap=180.0,
        padding=padding,
        alignment="center",
    )
    result = BlockPlan(root)
    result.validate()
    return result


def primary_ic_block_from_zero(
    schematic: dict[str, Any],
    module: ModuleLayout,
    *,
    padding: float = 40.0,
) -> BlockPlan | None:
    """Build a simple IC-local module without consulting seed coordinates.

    This deliberately supports one small, structural vocabulary: a unique
    largest IC, smaller active devices directly controlled by it, series
    boundary passives, rail shunts, and a two-terminal rail feed. Every
    component must match one of those roles or the module is left untouched.
    """

    instances, _, components = _components(schematic, module)
    if any(component.component_type == "connector" for component in components):
        return None
    active = tuple(
        component
        for component in components
        if component.component_type not in _NON_OWNER_TYPES
        and component.component_type != "connector"
    )
    if not active:
        return None
    largest_terminal_count = max(len(component.terminals) for component in active)
    largest = tuple(
        component for component in active if len(component.terminals) == largest_terminal_count
    )
    if len(largest) != 1 or largest_terminal_count < 8:
        return None
    primary = largest[0]
    subordinate = tuple(component for component in active if component != primary)
    component_refs = tuple(component.ref for component in components)
    boundary_net_names = _module_boundary_net_names(instances[module.instance_ref])
    primary_nets = {net_ref for net_ref, _ in primary.terminals.values()}

    subordinate_links: dict[str, tuple[str, str, str]] = {}
    for component in subordinate:
        shared = [
            (net_ref, primary_terminal, subordinate_terminal)
            for subordinate_terminal, (net_ref, net) in component.terminals.items()
            if not _is_rail_net(net_ref, net)
            for primary_terminal, (candidate_ref, _) in primary.terminals.items()
            if candidate_ref == net_ref
        ]
        if len(shared) != 1:
            return None
        subordinate_links[component.ref] = shared[0]

    series: list[tuple[_Component, str, str, str]] = []
    shunts: list[tuple[_Component, str, str, str, str]] = []
    rail_feeds: list[tuple[_Component, str, str, str]] = []
    for component in components:
        if component in active:
            continue
        if len(component.terminals) != 2:
            return None
        terminals = tuple(component.terminals)
        rail_terminals = [
            terminal
            for terminal, (net_ref, net) in component.terminals.items()
            if _is_rail_net(net_ref, net)
        ]
        if not rail_terminals:
            primary_ends = [
                terminal
                for terminal, (net_ref, _) in component.terminals.items()
                if net_ref in primary_nets
            ]
            if len(primary_ends) != 1:
                return None
            attached_terminal = primary_ends[0]
            remote_terminal = next(
                terminal for terminal in terminals if terminal != attached_terminal
            )
            remote_net_ref, remote_net = component.terminals[remote_terminal]
            if not _is_boundary_net(remote_net_ref, remote_net, boundary_net_names):
                return None
            series.append(
                (
                    component,
                    next(
                        primary_terminal
                        for primary_terminal, (net_ref, _) in primary.terminals.items()
                        if net_ref == component.terminals[attached_terminal][0]
                    ),
                    attached_terminal,
                    remote_terminal,
                )
            )
        elif len(rail_terminals) == 1:
            rail_terminal = rail_terminals[0]
            signal_terminal = next(terminal for terminal in terminals if terminal != rail_terminal)
            signal_net_ref, signal_net = component.terminals[signal_terminal]
            owners = _net_components(signal_net, component_refs) & {
                candidate.ref for candidate in active
            }
            if not owners:
                return None
            shunts.append(
                (
                    component,
                    signal_terminal,
                    rail_terminal,
                    signal_net_ref,
                    component.terminals[rail_terminal][0],
                )
            )
        elif len(rail_terminals) == 2:
            primary_ends = [
                terminal
                for terminal in rail_terminals
                if component.terminals[terminal][0] in primary_nets
            ]
            if len(primary_ends) != 1:
                return None
            attached_terminal = primary_ends[0]
            remote_terminal = next(
                terminal for terminal in terminals if terminal != attached_terminal
            )
            remote_net_ref, remote_net = component.terminals[remote_terminal]
            if not _is_boundary_net(remote_net_ref, remote_net, boundary_net_names):
                return None
            rail_feeds.append(
                (
                    component,
                    next(
                        primary_terminal
                        for primary_terminal, (net_ref, _) in primary.terminals.items()
                        if net_ref == component.terminals[attached_terminal][0]
                    ),
                    attached_terminal,
                    remote_terminal,
                )
            )
        else:
            return None

    primary_position = Position(0.0, 0.0, 0.0)
    primary_bounds = placed_symbol_bounds(primary.instance, primary_position)
    positions: dict[str, Position] = {primary.symbol_id: primary_position}
    attachments: list[_NetSymbolAttachment] = []
    labelled_nets: set[str] = set()
    skipped_primary_rails: set[str] = set()

    series_lanes: dict[str, int] = {}
    for side in ("left", "right"):
        candidates = []
        for component, primary_terminal, _, _ in series:
            primary_pin = _mean_point(
                pin_positions(primary.instance, primary_position, primary_terminal)
            )
            if _side(primary_pin, primary_bounds) == side:
                candidates.append((primary_pin.y, component.ref))
        lane_ys: list[float] = []
        for pin_y, component_ref in sorted(candidates):
            lane = next(
                (
                    lane_index
                    for lane_index, previous_y in enumerate(lane_ys)
                    if pin_y - previous_y >= _SERIES_ANNOTATION_PITCH
                ),
                len(lane_ys),
            )
            if lane == len(lane_ys):
                lane_ys.append(pin_y)
            else:
                lane_ys[lane] = pin_y
            series_lanes[component_ref] = lane

    for component, primary_terminal, attached_terminal, remote_terminal in (
        *series,
        *rail_feeds,
    ):
        primary_pin = _mean_point(
            pin_positions(primary.instance, primary_position, primary_terminal)
        )
        side = _side(primary_pin, primary_bounds)
        if side not in {"left", "right"}:
            return None
        direction = -1.0 if side == "left" else 1.0
        base = _oriented_terminal_vector(
            component,
            attached_terminal,
            remote_terminal,
            axis="x",
            direction=direction,
        )
        attached_pin = _mean_point(pin_positions(component.instance, base, attached_terminal))
        placed = _translated(
            base,
            attached_pin,
            Point(
                primary_pin.x
                + direction
                * (
                    (
                        _LOCAL_BRANCH_SPAN
                        if any(component.ref == item[0].ref for item in rail_feeds)
                        else _DEVICE_STUB
                    )
                    + series_lanes.get(component.ref, 0) * _SERIES_LANE_OFFSET
                ),
                primary_pin.y,
            ),
        )
        positions[component.symbol_id] = placed
        attachments.append(
            _terminal_net_symbol_attachment(
                component,
                placed,
                remote_terminal,
                side=side,
            )
        )
        remote_net_ref = component.terminals[remote_terminal][0]
        labelled_nets.add(remote_net_ref)
        if any(component.ref == item[0].ref for item in rail_feeds):
            skipped_primary_rails.add(primary_terminal)
            local_net_ref, local_net = component.terminals[attached_terminal]
            if _net_components(local_net, component_refs) - {component.ref, primary.ref}:
                # One local supply termination on the visible feed/consumer
                # chain, another at each remote consumer; do not force their
                # support wiring to join across the primary's pin field.
                feed_pin = _mean_point(pin_positions(component.instance, placed, attached_terminal))
                attachments.append(_net_symbol_attachment(
                    local_net_ref, local_net,
                    (Point((primary_pin.x + feed_pin.x) / 2, primary_pin.y),),
                    "bottom" if _is_return_net(local_net_ref, local_net) else "top",
                    clearance=_LOCAL_RAIL_STUB,
                ))

    # Complete the primary's own rail terminations before measuring space for
    # a neighbouring active circuit. These are not subordinate-owned symbols.
    primary_attachments = list(_anchor_net_symbol_attachments(
        primary, primary_position, skipped_terminals=skipped_primary_rails,
    ))
    signal_lanes = []
    for component, terminal, _, remote_terminal in series:
        pin = _mean_point(pin_positions(primary.instance, primary_position, terminal))
        bounds = placed_symbol_bounds(component.instance, positions[component.symbol_id])
        side = _side(pin, primary_bounds)
        remote = _mean_point(pin_positions(
            component.instance, positions[component.symbol_id], remote_terminal,
        ))
        net_ref, net = component.terminals[remote_terminal]
        caption = str(net.get("name", net_ref)).rsplit(".", 1)[-1]
        caption_width = _with_annotation_envelope(Envelope(0, 0, 0, 0), (caption,)).width
        outer = remote.x + (-caption_width if side == "left" else caption_width)
        signal_lanes.append((side, Envelope(
            min(pin.x, bounds.min_x, outer), min(pin.y, bounds.min_y) - _PIN_EXIT_STUB,
            max(pin.x, bounds.max_x, outer), max(pin.y, bounds.max_y) + _PIN_EXIT_STUB,
        )))
    attachments.extend(_clear_rail_signal_lanes(primary_attachments, signal_lanes))
    preview_symbols = _NetSymbols()
    _attach_net_symbols({}, tuple(attachments), symbols=preview_symbols)
    primary_obstacles = list(preview_symbols.rail_envelopes)
    # Primary-only bias branches occupy real space before a neighbour is
    # placed, including when their return nets alternate supply and ground.
    for branch, signal_terminal, rail_terminal, signal_net_ref, _ in shunts:
        signal_net = branch.terminals[signal_terminal][1]
        if _net_components(signal_net, component_refs) & {c.ref for c in active} != {primary.ref}:
            continue
        pin = _component_pin_for_net(primary, primary_position, signal_net_ref)
        if pin is None:
            continue
        owner_pin = pin[1]
        side = _side(owner_pin, primary_bounds)
        direction_y = 1.0 if _is_return_net(*branch.terminals[rail_terminal]) else -1.0
        base = _oriented_terminal_vector(
            branch, signal_terminal, rail_terminal, axis="y", direction=direction_y,
        )
        target = (
            Point(owner_pin.x + (-1 if side == "left" else 1) * _BRANCH_STUB,
                  owner_pin.y + direction_y * _LOCAL_RAIL_STUB)
            if side in {"left", "right"}
            else Point(owner_pin.x, owner_pin.y + direction_y * _BRANCH_STUB)
        )
        placed_branch = _translated(
            base, _mean_point(pin_positions(branch.instance, base, signal_terminal)), target,
        )
        primary_obstacles.append(_shunt_drawing_envelope(branch, placed_branch, rail_terminal))
    for local_component in components:
        if local_component == primary or local_component.symbol_id not in positions:
            continue
        bounds = placed_symbol_bounds(
            local_component.instance, positions[local_component.symbol_id],
        )
        primary_obstacles.append(_with_annotation_envelope(
            Envelope(bounds.min_x, bounds.min_y, bounds.max_x, bounds.max_y),
            (str(local_component.instance.get("reference_designator", "")),
             _attribute_string(local_component.instance, "value") or ""),
        ))

    placed_active: dict[str, tuple[_Component, Position]] = {
        primary.ref: (primary, primary_position)
    }
    for component in subordinate:
        net_ref, primary_terminal, subordinate_terminal = subordinate_links[component.ref]
        primary_pin = _mean_point(
            pin_positions(primary.instance, primary_position, primary_terminal)
        )
        primary_side = _side(primary_pin, primary_bounds)
        if primary_side not in {"left", "right"}:
            return None
        direction = -1.0 if primary_side == "left" else 1.0
        base = _active_orientation_toward(
            component,
            subordinate_terminal,
            outward_direction=direction,
        )
        component_pin = _mean_point(pin_positions(component.instance, base, subordinate_terminal))
        distance = _DEVICE_STUB
        for branch, signal_terminal, rail_terminal, signal_net_ref, _ in shunts:
            if signal_net_ref != net_ref:
                continue
            direction_y = 1.0 if _is_return_net(*branch.terminals[rail_terminal]) else -1.0
            branch_base = _oriented_terminal_vector(
                branch, signal_terminal, rail_terminal, axis="y", direction=direction_y,
            )
            branch_pin = _mean_point(pin_positions(branch.instance, branch_base, signal_terminal))
            branch_position = _translated(branch_base, branch_pin, Point(
                0, primary_pin.y + direction_y * _LOCAL_RAIL_STUB,
            ))
            envelope = _shunt_drawing_envelope(branch, branch_position, rail_terminal)
            for obstacle in sorted(primary_obstacles, key=lambda item: item.min_x,
                                   reverse=direction < 0):
                if envelope.min_y >= obstacle.max_y or obstacle.min_y >= envelope.max_y:
                    continue
                current = envelope.translated(primary_pin.x + direction * distance / 2, 0)
                if (current.max_x + _LOCAL_RAIL_STUB <= obstacle.min_x
                        or obstacle.max_x + _LOCAL_RAIL_STUB <= current.min_x):
                    continue
                midpoint = (
                    obstacle.max_x + _LOCAL_RAIL_STUB - envelope.min_x
                    if direction > 0
                    else obstacle.min_x - _LOCAL_RAIL_STUB - envelope.max_x
                )
                distance = max(distance, 2 * direction * (midpoint - primary_pin.x))
        placed = _translated(
            base,
            component_pin,
            Point(primary_pin.x + direction * distance, primary_pin.y),
        )
        positions[component.symbol_id] = placed
        placed_active[component.ref] = (component, placed)
        labelled_nets.discard(net_ref)

    for component, signal_terminal, rail_terminal, signal_net_ref, _ in shunts:
        signal_net = component.terminals[signal_terminal][1]
        owner_pins: list[tuple[_Component, Position, Point]] = []
        for owner_ref in sorted(_net_components(signal_net, component_refs)):
            owner = placed_active.get(owner_ref)
            if owner is None:
                continue
            owner_component, owner_position = owner
            pin = _component_pin_for_net(owner_component, owner_position, signal_net_ref)
            if pin is not None:
                owner_pins.append((owner_component, owner_position, pin[1]))
        if not owner_pins:
            return None
        direction_y = 1.0 if _is_return_net(*component.terminals[rail_terminal]) else -1.0
        base = _oriented_terminal_vector(
            component,
            signal_terminal,
            rail_terminal,
            axis="y",
            direction=direction_y,
        )
        signal_pin = _mean_point(pin_positions(component.instance, base, signal_terminal))
        if len(owner_pins) > 1:
            target = Point(
                float(median(point.x for _, _, point in owner_pins)),
                float(median(point.y for _, _, point in owner_pins)),
            )
            label_origin = target
            target = Point(target.x, target.y + direction_y * _LOCAL_RAIL_STUB)
        else:
            owner_component, owner_position, owner_pin = owner_pins[0]
            owner_bounds = placed_symbol_bounds(owner_component.instance, owner_position)
            owner_side = _side(owner_pin, owner_bounds)
            if owner_side in {"left", "right"}:
                direction_x = -1.0 if owner_side == "left" else 1.0
                target = Point(owner_pin.x + direction_x * _BRANCH_STUB, owner_pin.y)
                label_origin = target
                target = Point(target.x, target.y + direction_y * _LOCAL_RAIL_STUB)
            else:
                target = Point(owner_pin.x, owner_pin.y + direction_y * _BRANCH_STUB)
                label_origin = target
        placed = _translated(base, signal_pin, target)
        positions[component.symbol_id] = placed
        rail_side = "bottom" if direction_y > 0 else "top"
        attachments.append(
            _terminal_net_symbol_attachment(
                component,
                placed,
                rail_terminal,
                side=rail_side,
                clearance=_LOCAL_RAIL_STUB,
            )
        )
        if signal_net_ref not in labelled_nets and _is_boundary_net(
            signal_net_ref, signal_net, boundary_net_names
        ):
            direction_x = -1.0 if label_origin.x < primary_bounds.center_x else 1.0
            attachments.append(
                _NetSymbolAttachment(
                    signal_net_ref,
                    signal_net,
                    Point(
                        label_origin.x + direction_x * _NET_SYMBOL_STUB,
                        label_origin.y,
                    ),
                )
            )
            labelled_nets.add(signal_net_ref)

    for component, position in placed_active.values():
        if component != primary:
            attachments.extend(_anchor_net_symbol_attachments(component, position))
        for terminal, (net_ref, net) in component.terminals.items():
            if (
                net_ref in labelled_nets
                or _is_rail_net(net_ref, net)
                or str(net.get("name", net_ref)).rsplit(".", 1)[-1].startswith("NC_")
                or not _is_boundary_net(net_ref, net, boundary_net_names)
            ):
                continue
            if _net_components(net, component_refs) != {component.ref}:
                continue
            attachments.append(_terminal_net_symbol_attachment(component, position, terminal))
            labelled_nets.add(net_ref)

    _attach_net_symbols(positions, tuple(attachments))
    occupied: dict[str, Rect] = {}
    for component in components:
        position = positions[component.symbol_id]
        bounds = placed_symbol_body_bounds(component.instance, position)
        occupied[component.symbol_id] = Rect(
            bounds.min_x,
            bounds.min_y,
            bounds.max_x - bounds.min_x,
            bounds.max_y - bounds.min_y,
        )
    block = block_from_positions(
        "primary-ic-local",
        positions,
        occupied=occupied,
        padding=padding,
    )
    result = BlockPlan(block)
    result.validate()
    return result


def functional_ic_block_from_zero(
    schematic: dict[str, Any],
    module: ModuleLayout,
    *,
    padding: float = 40.0,
) -> BlockPlan | None:
    """Generate one simple connector/series/IC block without input coordinates.

    The deliberately narrow eligibility rule is structural: exactly one active
    non-connector anchor, two connectors, a series signal path from each
    connector to the anchor, and only local shunts or bypass capacitors beyond
    those paths. Unsupported modules are left to their existing layout.
    """

    _, _, components = _components(schematic, module)
    connectors = tuple(
        component for component in components if component.component_type == "connector"
    )
    active = tuple(
        component
        for component in components
        if component.component_type not in _NON_OWNER_TYPES
        and component.component_type != "connector"
    )
    if len(connectors) != 2 or len(active) != 1:
        return None
    central = active[0]
    links = _series_links(components, central, connectors)
    links_by_connector = {
        connector.ref: tuple(link for link in links if link.connector.ref == connector.ref)
        for connector in connectors
    }
    if any(not connector_links for connector_links in links_by_connector.values()):
        return None

    central_position = _central_orientation(central, connectors, links)
    connector_signal_x = {
        connector.ref: float(
            median(
                _mean_point(
                    pin_positions(central.instance, central_position, link.central_terminal)
                ).x
                for link in links_by_connector[connector.ref]
            )
        )
        for connector in connectors
    }
    left, right = sorted(connectors, key=lambda connector: connector_signal_x[connector.ref])

    # Phase 1: establish the IC anchor. No support symbol or annotation may
    # influence its orientation or origin.
    positions: dict[str, Position] = {central.symbol_id: central_position}
    owners = {central.symbol_id: central.ref}

    # Phase 2: derive immutable electrical lanes from the IC's real pins.
    wire_groups = {
        connector.ref: _series_wires_from_ic(
            central,
            central_position,
            links_by_connector[connector.ref],
            side=side,
        )
        for connector, side in ((left, "left"), (right, "right"))
    }

    # Phase 3: attach component bodies to those lanes, then attach connectors,
    # branches, and one-pin net symbols to the resulting wire endpoints.
    connector_positions: dict[str, Position] = {}
    direct_bundles: dict[str, bool] = {}
    series_refs = {link.passive.ref for link in links}
    for connector, side in ((left, "left"), (right, "right")):
        wires = wire_groups[connector.ref]
        passive_positions = _place_series_symbols(wires)
        positions.update(passive_positions)
        owners.update({key: connector.ref for key in passive_positions})
        connector_position = _place_connector(
            connector,
            wires,
            passive_positions,
            side=side,
        )
        positions[connector.symbol_id] = connector_position
        owners[connector.symbol_id] = connector.ref
        connector_positions[connector.ref] = connector_position
        direct_bundles[connector.ref] = _has_straight_bundle(connector, connector_position, wires)
        if not direct_bundles[connector.ref]:
            # The resistor belongs to the connector breakout. Choose that
            # physical pin's row before placing it; do not inherit an IC row
            # across a connection that will terminate by name.
            local_wires = []
            for wire in wires:
                point = _connector_signal_points(
                    connector, connector_position, wire.link.connector_net,
                )[0]
                local_wires.append(replace(
                    wire, owner_pin=point,
                    passive_pin_target=Point(wire.passive_pin_target.x, point.y),
                ))
            wire_groups[connector.ref] = tuple(local_wires)
            positions.update(_place_series_symbols(tuple(local_wires)))

    attachments: list[tuple[str, _NetSymbolAttachment]] = []
    shunt_refs: set[str] = set()
    for connector in (left, right):
        used, local_attachments = _place_shunts(
            components, (connector,), connector_positions, positions,
        )
        shunt_refs.update(used)
        owners.update({item.symbol_id: connector.ref for item in components if item.ref in used})
        attachments.extend((connector.ref, item) for item in local_attachments)
    bypass_refs, bypass_owner_terminals, bypass_attachments = _place_bypasses(
        components,
        central,
        central_position,
        positions,
    )
    owners.update({item.symbol_id: central.ref for item in components if item.ref in bypass_refs})
    attachments.extend((central.ref, item) for item in bypass_attachments)
    classified = {central.ref, *(connector.ref for connector in connectors)}
    classified.update(series_refs)
    classified.update(shunt_refs)
    classified.update(bypass_refs)
    if classified != {component.ref for component in components}:
        return None

    for component in (central, *connectors):
        position = positions[component.symbol_id]
        local_attachments = list(
            _anchor_net_symbol_attachments(
                component,
                position,
                skipped_terminals=(bypass_owner_terminals if component == central else None),
            )
        )
        local_attachments.extend(
            _single_ended_net_symbol_attachments(
                component,
                position,
                tuple(item.ref for item in components),
            )
        )
        attachments.extend((component.ref, item) for item in local_attachments)
    # Finish passive annotation clearance before fixing named endpoints.
    for connector, side in ((left, "left"), (right, "right")):
        positions.update(_apply_series_annotation_clearance(
            wire_groups[connector.ref], positions, side=side,
            obstacles=tuple(
                _rail_drawing_envelope(item.net, item.position())
                for owner, item in attachments if owner == connector.ref
                and _is_rail_net(item.net_ref, item.net)
            ),
        ))
        if not direct_bundles[connector.ref]:
            for wire in wire_groups[connector.ref]:
                link = wire.link
                attachments.extend((
                    (connector.ref, _named_signal_attachment(
                        link.passive, positions[link.passive.symbol_id],
                        link.passive_central_terminal, "right" if side == "left" else "left",
                    )),
                    (central.ref, _named_signal_attachment(
                        central, central_position, link.central_terminal, side,
                    )),
                ))
    net_symbols = _NetSymbols()
    net_by_symbol: dict[str, dict[str, Any]] = {}
    owner_rail_envelopes: dict[str, list[Envelope]] = {}
    for owner, attachment in sorted(
        attachments, key=lambda item: (item[0], _attachment_order(item[1])),
    ):
        # These drawings are still in independent owner-local coordinates.
        # Only same-owner rails can collide before the blocks are packed.
        net_symbols.rail_envelopes = owner_rail_envelopes.setdefault(owner, [])
        before = set(positions)
        _attach_net_symbols(positions, (attachment,), symbols=net_symbols)
        symbol_id, = set(positions) - before
        owners[symbol_id] = owner
        net_by_symbol[symbol_id] = attachment.net

    occupied = {}
    visual: dict[str, Envelope] = {}
    for component in components:
        position = positions[component.symbol_id]
        bounds = placed_symbol_body_bounds(component.instance, position)
        occupied[component.symbol_id] = Rect(
            bounds.min_x,
            bounds.min_y,
            bounds.max_x - bounds.min_x,
            bounds.max_y - bounds.min_y,
        )
        full = placed_symbol_bounds(component.instance, position)
        labels = (
            str(component.instance.get("reference_designator", "")),
            _attribute_string(component.instance, "value") or "",
        )
        visual[component.symbol_id] = _with_annotation_envelope(
            Envelope(full.min_x, full.min_y, full.max_x, full.max_y), labels,
        )
    for symbol_id, net in net_by_symbol.items():
        position = positions[symbol_id]
        properties = net.get("properties", {})
        if "__symbol_value" in properties:
            full = placed_symbol_bounds({"attributes": properties}, position)
            envelope = Envelope(full.min_x, full.min_y, full.max_x, full.max_y)
        else:
            envelope = Envelope(position.x, position.y, position.x, position.y)
        visual[symbol_id] = _with_annotation_envelope(
            envelope, (str(net.get("name", "")).rsplit(".", 1)[-1],),
        )
    if set(owners) != set(positions) or set(visual) != set(positions):
        raise ToolchainError("every local drawing must have an owner and a visual envelope")
    for key, position in positions.items():
        # Rotated symbols can have their stored anchor outside the painted
        # bounds. Use the same origin for measuring and normalizing a child.
        visual[key] = visual[key].union(
            Envelope(position.x, position.y, position.x, position.y),
        )

    # Pack completed local drawings, not bare connector/IC bodies. Inline
    # parts travel with their connector breakout; connections to the IC span
    # the gap. Preserve every electrical row during the rigid translation.
    owner_blocks = []
    top = min(envelope.min_y for envelope in visual.values()) - padding
    cursor_x = 0.0
    # Four millimetres of whitespace outside the caption allowances. The
    # diagram is scale independent; this is not tied to bitmap/page size.
    gap = 40.0
    for owner in (left, central, right):
        members = {key: position for key, position in positions.items() if owners[key] == owner.ref}
        envelope = visual[next(iter(members))]
        for key in members:
            envelope = envelope.union(visual[key])
        child = block_from_positions(
            owner.symbol_id.removeprefix("comp:"), members,
            occupied={key: occupied[key] for key in members if key in occupied},
            content_bounds=Rect(envelope.min_x, envelope.min_y, envelope.width, envelope.height),
            padding=0.0,
        )
        y = envelope.min_y - top
        owner_blocks.append(PlacedBlock(child, cursor_x, y))
        cursor_x += child.width + gap
    block = LayoutBlock(
        "functional-ic", cursor_x - gap,
        max(child.y + child.block.height for child in owner_blocks),
        children=tuple(owner_blocks), minimum_child_spacing=gap,
    )
    result = BlockPlan(block)
    result.validate()
    return result
