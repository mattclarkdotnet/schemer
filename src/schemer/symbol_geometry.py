"""Pin-aware geometry checks used before a schematic is accepted for rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from math import cos, hypot, inf, radians, sin
from statistics import median
from typing import TYPE_CHECKING, Any

from schemer.layout import Position
from schemer.toolchain import ToolchainError

if TYPE_CHECKING:
    from schemer.layout import LayoutPlan, ModuleLayout


@dataclass(frozen=True)
class Point:
    """A point in the viewer's schematic coordinate system."""

    x: float
    y: float


@dataclass(frozen=True)
class SeriesFeedGeometry:
    """Resolved endpoints for one directed two-terminal series branch."""

    upstream_feed: Point
    upstream_pin: Point
    downstream_pin: Point
    downstream_feed: Point


@dataclass(frozen=True)
class SymbolBounds:
    """Unrotated KiCad symbol bounds in millimetres, before viewer expansion."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float


@dataclass(frozen=True)
class PlacedBounds:
    """Axis-aligned symbol bounds in the viewer coordinate system."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def center_x(self) -> float:
        return (self.min_x + self.max_x) / 2

    @property
    def center_y(self) -> float:
        return (self.min_y + self.max_y) / 2

    def union(self, other: PlacedBounds) -> PlacedBounds:
        return PlacedBounds(
            min(self.min_x, other.min_x),
            min(self.min_y, other.min_y),
            max(self.max_x, other.max_x),
            max(self.max_y, other.max_y),
        )

    def translated(self, delta_x: float, delta_y: float) -> PlacedBounds:
        return PlacedBounds(
            self.min_x + delta_x,
            self.min_y + delta_y,
            self.max_x + delta_x,
            self.max_y + delta_y,
        )


@dataclass(frozen=True)
class QualityFinding:
    """One machine-readable geometric concern for review, not an ERC result."""

    code: str
    module_ref: str
    symbol_ids: tuple[str, ...]
    measured: float
    threshold: float
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "measured": round(self.measured, 4),
            "message": self.message,
            "module_ref": self.module_ref,
            "symbol_ids": list(self.symbol_ids),
            "threshold": self.threshold,
        }


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_VIEWER_UNITS_PER_MM = 10.0
_VIEWER_BOUNDS_EXPANSION_MM = 0.1


def _attribute_string(instance: dict[str, Any], name: str) -> str | None:
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return None
    value = attributes.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        string_value = value.get("String")
        if isinstance(string_value, str):
            return string_value
    return None


def _balanced_blocks(source: str, head: str) -> list[str]:
    """Extract balanced S-expression blocks with the requested head."""

    blocks: list[str] = []
    search_from = 0
    marker = re.compile(rf"\({re.escape(head)}(?=\s)")
    while (match := marker.search(source, search_from)) is not None:
        start = match.start()
        depth = 0
        quoted = False
        escaped = False
        for index in range(start, len(source)):
            character = source[index]
            if quoted:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    quoted = False
                continue
            if character == '"':
                quoted = True
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    blocks.append(source[start : index + 1])
                    search_from = index + 1
                    break
        else:
            break
    return blocks


def symbol_pin_offsets(instance: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Read named and numbered pin offsets from an evaluated KiCad symbol."""

    symbol = _attribute_string(instance, "__symbol_value")
    if symbol is None:
        return {}
    offsets: dict[str, tuple[float, float]] = {}
    for block in _balanced_blocks(symbol, "pin"):
        at_match = re.search(
            rf"\(at\s+({_NUMBER})\s+({_NUMBER})(?:\s+{_NUMBER})?\)",
            block,
        )
        if at_match is None:
            continue
        offset = (float(at_match.group(1)), float(at_match.group(2)))
        for field in ("name", "number"):
            field_match = re.search(rf'\({field}\s+"([^"]*)"', block)
            if field_match is not None and field_match.group(1):
                offsets[field_match.group(1)] = offset
    return offsets


def pin_outward_side(instance: dict[str, Any], position: Position, terminal: str) -> str:
    """Read a terminal's stroke direction, including pins at bounding-box corners."""
    if position.mirror is not None:
        raise ToolchainError("pin-exit hints do not yet support mirrored symbols")
    symbol = _attribute_string(instance, "__symbol_value") or ""
    sides = set()
    for block in _balanced_blocks(symbol, "pin"):
        names = [
            match.group(1)
            for field in ("name", "number")
            if (match := re.search(rf'\({field}\s+"([^"]*)"', block)) is not None
        ]
        if terminal not in names:
            continue
        at = re.search(rf"\(at\s+{_NUMBER}\s+{_NUMBER}\s+({_NUMBER})\)", block)
        if at is None:
            raise ToolchainError(f"pin-exit cannot resolve direction of {terminal}")
        angle = radians(float(at.group(1)))
        dx, dy = rotated_offset((-cos(angle), -sin(angle)), position.rotation)
        if abs(dx) > 1e-6 and abs(dy) > 1e-6:
            raise ToolchainError(f"pin-exit requires an orthogonal pin: {terminal}")
        sides.add(
            ("right" if dx > 0 else "left") if abs(dx) > 1e-6 else ("bottom" if dy > 0 else "top")
        )
    if len(sides) != 1:
        raise ToolchainError(f"pin-exit requires one unambiguous direction: {terminal}")
    return sides.pop()


def symbol_pin_offset_groups(
    instance: dict[str, Any],
) -> dict[str, tuple[tuple[float, float], ...]]:
    """Read every physical pin offset grouped by logical KiCad pin name.

    A logical terminal may be painted more than once, such as two ground pins
    on one IC face. Unlike ``symbol_pin_offsets``, this representation does not
    discard duplicate names.
    """

    symbol = _attribute_string(instance, "__symbol_value")
    if symbol is None:
        return {}
    grouped: dict[str, list[tuple[float, float]]] = {}
    for block in _balanced_blocks(symbol, "pin"):
        at_match = re.search(
            rf"\(at\s+({_NUMBER})\s+({_NUMBER})(?:\s+{_NUMBER})?\)",
            block,
        )
        if at_match is None:
            continue
        offset = (float(at_match.group(1)), float(at_match.group(2)))
        for field in ("name", "number"):
            field_match = re.search(rf'\({field}\s+"([^"]*)"', block)
            if field_match is not None and field_match.group(1):
                grouped.setdefault(field_match.group(1), []).append(offset)
    return {name: tuple(offsets) for name, offsets in grouped.items()}


def symbol_pin_electrical_types(instance: dict[str, Any]) -> dict[str, str]:
    """Read KiCad electrical pin types by both pin name and pin number."""

    symbol = _attribute_string(instance, "__symbol_value")
    if symbol is None:
        return {}
    result: dict[str, str] = {}
    for block in _balanced_blocks(symbol, "pin"):
        type_match = re.match(r"\(pin\s+([a-zA-Z0-9_]+)\s+", block)
        if type_match is None:
            continue
        electrical_type = type_match.group(1)
        for field in ("name", "number"):
            field_match = re.search(rf'\({field}\s+"([^"]*)"', block)
            if field_match is not None and field_match.group(1):
                result[field_match.group(1)] = electrical_type
    return result


def symbol_pin_numbers(instance: dict[str, Any]) -> dict[str, str]:
    """Map each non-empty KiCad pin name to its physical pin number."""

    symbol = _attribute_string(instance, "__symbol_value")
    if symbol is None:
        return {}
    result: dict[str, str] = {}
    for block in _balanced_blocks(symbol, "pin"):
        name_match = re.search(r'\(name\s+"([^"]*)"', block)
        number_match = re.search(r'\(number\s+"([^"]*)"', block)
        if (
            name_match is not None
            and name_match.group(1)
            and number_match is not None
            and number_match.group(1)
        ):
            result[name_match.group(1)] = number_match.group(1)
    return result


def _symbol_local_bounds(
    instance: dict[str, Any],
    *,
    include_pins: bool,
) -> SymbolBounds:
    """Calculate local primitive bounds, optionally including pin strokes."""

    symbol = _attribute_string(instance, "__symbol_value")
    if symbol is None:
        raise ToolchainError("evaluated component has no symbol geometry")
    points: list[tuple[float, float]] = []

    def add_fields(block: str, fields: tuple[str, ...]) -> None:
        for field in fields:
            for match in re.finditer(rf"\({field}\s+({_NUMBER})\s+({_NUMBER})\)", block):
                points.append((float(match.group(1)), float(match.group(2))))

    for block in _balanced_blocks(symbol, "rectangle"):
        add_fields(block, ("start", "end"))
    for tag in ("polyline", "bezier"):
        for block in _balanced_blocks(symbol, tag):
            add_fields(block, ("xy",))
    for block in _balanced_blocks(symbol, "circle"):
        center = re.search(rf"\(center\s+({_NUMBER})\s+({_NUMBER})\)", block)
        radius = re.search(rf"\(radius\s+({_NUMBER})\)", block)
        if center is not None and radius is not None:
            x = float(center.group(1))
            y = float(center.group(2))
            value = float(radius.group(1))
            points.extend(((x - value, y - value), (x + value, y + value)))
    for block in _balanced_blocks(symbol, "arc"):
        add_fields(block, ("start", "mid", "end"))
    if include_pins:
        for block in _balanced_blocks(symbol, "pin"):
            at = re.search(rf"\(at\s+({_NUMBER})\s+({_NUMBER})(?:\s+({_NUMBER}))?\)", block)
            if at is None:
                continue
            x = float(at.group(1))
            y = float(at.group(2))
            angle = radians(float(at.group(3) or 0.0))
            length_match = re.search(rf"\(length\s+({_NUMBER})\)", block)
            length = float(length_match.group(1)) if length_match is not None else 0.0
            points.extend(((x, y), (x + length * cos(angle), y + length * sin(angle))))

    if not points:
        raise ToolchainError("evaluated symbol has no bounded primitives")
    expansion = _VIEWER_BOUNDS_EXPANSION_MM
    return SymbolBounds(
        min_x=min(point[0] for point in points) - expansion,
        min_y=min(point[1] for point in points) - expansion,
        max_x=max(point[0] for point in points) + expansion,
        max_y=max(point[1] for point in points) + expansion,
    )


def symbol_local_bounds(instance: dict[str, Any]) -> SymbolBounds:
    """Calculate the complete geometry bounds used by the viewer's stored anchor.

    The local pcb importer documents persisted schematic positions as 0.1 mm
    units at the unrotated symbol bounding-box top-left. This mirrors its
    primitive coverage: rectangles, polylines, Beziers, circles, arcs, and pin
    endpoints. Text properties are intentionally excluded.
    """

    return _symbol_local_bounds(instance, include_pins=True)


def symbol_body_local_bounds(instance: dict[str, Any]) -> SymbolBounds:
    """Calculate painted component-body bounds without pin strokes or endpoints."""

    return _symbol_local_bounds(instance, include_pins=False)


def rotated_offset(offset: tuple[float, float], rotation: float) -> tuple[float, float]:
    """Map a KiCad y-up local offset through the viewer's stored rotation."""

    x, y = offset
    normalized = int(rotation) % 360
    if normalized == 0:
        return x, -y
    if normalized == 90:
        return y, x
    if normalized == 180:
        return -x, y
    if normalized == 270:
        return -y, -x
    raise ToolchainError(f"unsupported orthogonal rotation: {rotation}")


def placed_symbol_bounds(instance: dict[str, Any], position: Position) -> PlacedBounds:
    """Resolve one stored symbol anchor into its visible geometry bounds."""

    if position.mirror is not None:
        raise ToolchainError("placed bounds do not yet support mirrored symbols")
    bounds = symbol_local_bounds(instance)
    origin_x = position.x - bounds.min_x * _VIEWER_UNITS_PER_MM
    origin_y = position.y + bounds.max_y * _VIEWER_UNITS_PER_MM
    corners = (
        (bounds.min_x, bounds.min_y),
        (bounds.min_x, bounds.max_y),
        (bounds.max_x, bounds.min_y),
        (bounds.max_x, bounds.max_y),
    )
    transformed = [
        (
            origin_x + rotated_offset(corner, position.rotation)[0] * _VIEWER_UNITS_PER_MM,
            origin_y + rotated_offset(corner, position.rotation)[1] * _VIEWER_UNITS_PER_MM,
        )
        for corner in corners
    ]
    return PlacedBounds(
        min(point[0] for point in transformed),
        min(point[1] for point in transformed),
        max(point[0] for point in transformed),
        max(point[1] for point in transformed),
    )


def placed_symbol_body_bounds(instance: dict[str, Any], position: Position) -> PlacedBounds:
    """Resolve painted body geometry using the viewer's complete-symbol anchor."""

    if position.mirror is not None:
        raise ToolchainError("placed bounds do not yet support mirrored symbols")
    anchor_bounds = symbol_local_bounds(instance)
    body_bounds = symbol_body_local_bounds(instance)
    origin_x = position.x - anchor_bounds.min_x * _VIEWER_UNITS_PER_MM
    origin_y = position.y + anchor_bounds.max_y * _VIEWER_UNITS_PER_MM
    corners = (
        (body_bounds.min_x, body_bounds.min_y),
        (body_bounds.min_x, body_bounds.max_y),
        (body_bounds.max_x, body_bounds.min_y),
        (body_bounds.max_x, body_bounds.max_y),
    )
    transformed = [
        (
            origin_x + rotated_offset(corner, position.rotation)[0] * _VIEWER_UNITS_PER_MM,
            origin_y + rotated_offset(corner, position.rotation)[1] * _VIEWER_UNITS_PER_MM,
        )
        for corner in corners
    ]
    return PlacedBounds(
        min(point[0] for point in transformed),
        min(point[1] for point in transformed),
        max(point[0] for point in transformed),
        max(point[1] for point in transformed),
    )


def pin_position(instance: dict[str, Any], position: Position, pin_name: str) -> Point:
    """Resolve a named terminal to viewer coordinates for a placed symbol."""

    if position.mirror is not None:
        raise ToolchainError("pin geometry validation does not yet support mirrored symbols")
    offsets = symbol_pin_offsets(instance)
    if pin_name not in offsets:
        raise ToolchainError(f"evaluated symbol has no pin geometry for {pin_name!r}")
    bounds = symbol_local_bounds(instance)
    origin_x = position.x - bounds.min_x * _VIEWER_UNITS_PER_MM
    origin_y = position.y + bounds.max_y * _VIEWER_UNITS_PER_MM
    offset_x, offset_y = rotated_offset(offsets[pin_name], position.rotation)
    return Point(
        origin_x + offset_x * _VIEWER_UNITS_PER_MM,
        origin_y + offset_y * _VIEWER_UNITS_PER_MM,
    )


def pin_positions(instance: dict[str, Any], position: Position, pin_name: str) -> tuple[Point, ...]:
    """Resolve every physical occurrence of one logical terminal."""

    if position.mirror is not None:
        raise ToolchainError("pin geometry validation does not yet support mirrored symbols")
    grouped = symbol_pin_offset_groups(instance)
    offsets = grouped.get(pin_name)
    if not offsets:
        raise ToolchainError(f"evaluated symbol has no pin geometry for {pin_name!r}")
    bounds = symbol_local_bounds(instance)
    origin_x = position.x - bounds.min_x * _VIEWER_UNITS_PER_MM
    origin_y = position.y + bounds.max_y * _VIEWER_UNITS_PER_MM
    result: list[Point] = []
    for offset in offsets:
        offset_x, offset_y = rotated_offset(offset, position.rotation)
        result.append(
            Point(
                origin_x + offset_x * _VIEWER_UNITS_PER_MM,
                origin_y + offset_y * _VIEWER_UNITS_PER_MM,
            )
        )
    return tuple(result)


def net_symbol_pin_position(net: dict[str, Any], position: Position) -> Point:
    """Resolve the sole electrical pin of a placed one-pin net symbol."""

    properties = net.get("properties")
    if not isinstance(properties, dict):
        raise ToolchainError("evaluated net has no symbol properties")
    instance = {"attributes": properties}
    offsets = symbol_pin_offset_groups(instance)
    if not offsets:
        raise ToolchainError("evaluated net symbol has no pin geometry")
    points = {
        (
            round(point.x, 9),
            round(point.y, 9),
        )
        for pin_name in offsets
        for point in pin_positions(instance, position, pin_name)
    }
    if len(points) != 1:
        raise ToolchainError("evaluated net symbol must have exactly one electrical pin")
    x, y = next(iter(points))
    return Point(x, y)


def position_net_symbol_pin(
    net: dict[str, Any],
    target: Point,
    *,
    rotation: float = 0.0,
) -> Position:
    """Return a stored anchor whose net-symbol pin lands exactly on ``target``."""

    origin = Position(0.0, 0.0, rotation=rotation)
    pin = net_symbol_pin_position(net, origin)
    return Position(target.x - pin.x, target.y - pin.y, rotation=rotation)


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


def _viewer_position(raw: dict[str, Any]) -> Position:
    return Position(
        x=float(raw["x"]),
        y=float(raw["y"]),
        rotation=float(raw.get("rotation", 0)),
        mirror=raw.get("mirror"),
    )


def _center(positions: list[Position]) -> Point:
    return Point(
        x=float(median(position.x for position in positions)),
        y=float(median(position.y for position in positions)),
    )


def _component_groups(
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


def _port_component_ref(port_ref: str, component_refs: tuple[str, ...]) -> str | None:
    matches = [
        component_ref
        for component_ref in component_refs
        if port_ref.startswith(component_ref + ".")
    ]
    return max(matches, key=len) if matches else None


def _net_anchor(
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
        if (component_ref := _port_component_ref(port_ref, component_refs)) is not None
        if component_ref != subject_ref
    }
    for component_ref in attached_components:
        points.append(_center([position for _, position in component_groups[component_ref]]))

    net_name = net.get("name")
    if isinstance(net_name, str) and net_name in net_symbol_groups:
        points.append(_center(net_symbol_groups[net_name]))
    if not points:
        return None
    return Point(
        x=float(median(point.x for point in points)),
        y=float(median(point.y for point in points)),
    )


def _opposing_anchor_axis(first: Point, second: Point, component: Position) -> str | None:
    delta_x = abs(second.x - first.x)
    delta_y = abs(second.y - first.y)
    if delta_x >= delta_y and (first.x - component.x) * (second.x - component.x) < 0:
        return "x"
    if delta_y > delta_x and (first.y - component.y) * (second.y - component.y) < 0:
        return "y"
    return None


def _module_orientations(
    schematic: dict[str, Any], module: ModuleLayout, resolved_positions: dict[str, Position]
) -> ModuleLayout:
    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    component_groups = _component_groups(module.instance_ref, resolved_positions)
    net_symbol_groups: dict[str, list[Position]] = {}
    for symbol_id, position in resolved_positions.items():
        if not symbol_id.startswith("sym:"):
            continue
        net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
        if separator and suffix.isdigit():
            net_symbol_groups.setdefault(net_name, []).append(position)

    updated = dict(module.positions)
    for component_ref, group in component_groups.items():
        if len(group) != 1:
            continue
        symbol_id, component_position = group[0]
        instance = instances.get(component_ref)
        if not isinstance(instance, dict) or instance.get("kind") != "Component":
            continue

        pin_offsets = symbol_pin_offsets(instance)
        terminal_nets: dict[str, dict[str, Any]] = {}
        prefix = component_ref + "."
        for net in nets.values():
            if not isinstance(net, dict):
                continue
            for port_ref in net.get("ports", []):
                if not isinstance(port_ref, str) or not port_ref.startswith(prefix):
                    continue
                terminal_name = port_ref.removeprefix(prefix)
                if "." not in terminal_name and terminal_name in pin_offsets:
                    terminal_nets[terminal_name] = net
        if len(terminal_nets) != 2 or len({id(net) for net in terminal_nets.values()}) != 2:
            continue

        terminals = sorted(terminal_nets)
        first_terminal, second_terminal = terminals
        first_anchor = _net_anchor(
            terminal_nets[first_terminal],
            subject_ref=component_ref,
            component_groups=component_groups,
            net_symbol_groups=net_symbol_groups,
        )
        second_anchor = _net_anchor(
            terminal_nets[second_terminal],
            subject_ref=component_ref,
            component_groups=component_groups,
            net_symbol_groups=net_symbol_groups,
        )
        if first_anchor is None or second_anchor is None:
            continue
        if _opposing_anchor_axis(first_anchor, second_anchor, component_position) is None:
            continue

        first_pin = pin_position(instance, component_position, first_terminal)
        second_pin = pin_position(instance, component_position, second_terminal)
        anchor_vector = (
            second_anchor.x - first_anchor.x,
            second_anchor.y - first_anchor.y,
        )
        pin_vector = (second_pin.x - first_pin.x, second_pin.y - first_pin.y)
        if anchor_vector[0] * pin_vector[0] + anchor_vector[1] * pin_vector[1] >= 0:
            continue

        local_symbol_id = symbol_id
        source_position = module.positions[local_symbol_id]
        updated[local_symbol_id] = replace(
            source_position,
            rotation=(source_position.rotation + 180) % 360,
        )

    return replace(module, positions=updated)


def orient_two_terminal_components(schematic: dict[str, Any], plan: LayoutPlan) -> LayoutPlan:
    """Orient every evidenced two-terminal branch toward its own neighbouring nets.

    This pass is board-agnostic. It considers every placed two-terminal
    component, uses evaluated connectivity to find the geometric centre of the
    components and net symbols on each terminal's net, and flips the symbol by
    180 degrees when its real pin order opposes those anchors. Components whose
    two nets do not have clear, opposing anchors retain their semantic
    orientation; no reference-designator or component-type exceptions exist.
    """

    proposed = plan.apply_to_schematic(schematic)
    instances = proposed.get("instances")
    if not isinstance(instances, dict):
        raise ToolchainError("schematic instances were not an object")
    modules = []
    for module in plan.modules:
        instance = instances.get(module.instance_ref)
        if not isinstance(instance, dict):
            raise ToolchainError(f"layout module is absent: {module.instance_ref}")
        raw_positions = instance.get("symbol_positions")
        if not isinstance(raw_positions, dict):
            raise ToolchainError(f"layout module has no positions: {module.instance_ref}")
        resolved_positions = {
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        modules.append(_module_orientations(proposed, module, resolved_positions))
    return replace(plan, modules=tuple(modules))


def _is_rail_net(net_ref: str, net: dict[str, Any]) -> bool:
    kind = str(net.get("kind", "")).casefold()
    name = str(net.get("name", net_ref)).upper()
    return kind in {"ground", "power"} or bool(
        re.search(r"(?:^|_)(?:GND|GROUND|VCC|VDD|POWER|VSYS)(?:$|_)", name)
    )


def _is_return_net(net_ref: str, net: dict[str, Any]) -> bool:
    kind = str(net.get("kind", "")).casefold()
    name = str(net.get("name", net_ref)).upper()
    return kind == "ground" or bool(re.search(r"(?:^|_)(?:GND|GROUND)(?:$|_)", name))


def _terminal_nets(
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


def _two_terminal_axis(
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


def _resolved_to_source_ids(schematic: dict[str, Any], module: ModuleLayout) -> dict[str, str]:
    from schemer.layout import resolve_module_position_ids

    result: dict[str, str] = {}
    for source_id, position in module.positions.items():
        single = replace(module, positions={source_id: position})
        resolved = resolve_module_position_ids(single, schematic)
        result[next(iter(resolved))] = source_id
    return result


_NON_OWNER_TYPES = {
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


def _group_bounds(component: dict[str, Any], group: list[tuple[str, Position]]) -> PlacedBounds:
    bounds: PlacedBounds | None = None
    for _, position in group:
        placed = placed_symbol_bounds(component, position)
        bounds = placed if bounds is None else bounds.union(placed)
    if bounds is None:
        raise ToolchainError("placed component group has no symbols")
    return bounds


def _power_branch_exists(
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
        terminals = _terminal_nets(component_ref, component, nets)
        terminal_refs = {net_ref for net_ref, _ in terminals.values()}
        if rail_ref not in terminal_refs or len(terminal_refs) != 2:
            continue
        if all(_is_rail_net(net_ref, net) for net_ref, net in terminals.values()):
            return True
    return False


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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        resolved_to_source = _resolved_to_source_ids(proposed, module)
        updated = dict(module.positions)
        moved_positions = dict(resolved_positions)
        claimed_symbols: set[str] = set()

        for capacitor_ref, capacitor_group in sorted(component_groups.items()):
            capacitor = instances.get(capacitor_ref)
            if (
                not isinstance(capacitor, dict)
                or capacitor.get("kind") != "Component"
                or _attribute_string(capacitor, "type") != "capacitor"
                or len(capacitor_group) != 1
            ):
                continue
            terminals = _terminal_nets(capacitor_ref, capacitor, nets)
            if len(terminals) != 2:
                continue
            return_entries = [
                (net_ref, net)
                for net_ref, net in terminals.values()
                if _is_return_net(net_ref, net)
            ]
            supply_entries = [
                (net_ref, net)
                for net_ref, net in terminals.values()
                if _is_rail_net(net_ref, net) and not _is_return_net(net_ref, net)
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
                component_type = _attribute_string(owner, "type") or ""
                if component_type.casefold() in _NON_OWNER_TYPES:
                    continue
                owner_terminals = _terminal_nets(owner_ref, owner, nets)
                owner_nets = {net_ref for net_ref, _ in owner_terminals.values()}
                if not capacitor_nets <= owner_nets:
                    continue
                owner_bounds = _group_bounds(owner, owner_group)
                distance = abs(capacitor_bounds.center_x - owner_bounds.center_x) + abs(
                    capacitor_bounds.center_y - owner_bounds.center_y
                )
                owners.append((distance, owner_ref, owner_bounds))

            if not owners:
                if _power_branch_exists(
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
                    other_bounds = _group_bounds(other, other_group)
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


def _internal_signal_symbol_ids(
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
        if (not isinstance(net, dict) or _is_rail_net(net_name, net)
                or (_is_named_interface_net(net) and named_pair)):
            continue
        ports = tuple(port for port in net.get("ports", ()) if isinstance(port, str))
        local_components = {
            component_ref
            for port in ports
            if (component_ref := _port_component_ref(port, component_refs)) is not None
        }
        has_module_boundary = (
            net_name in boundary_net_names or net.get("name") in boundary_net_names
        )
        if len(local_components) >= 2 and not has_module_boundary:
            result.add(symbol_id)
    return result


def _is_named_interface_net(net: dict[str, Any]) -> bool:
    """An explicit presentation endpoint, not an incidental floating label."""
    value = net.get("properties", {}).get("symbol_name")
    if isinstance(value, dict):
        value = value.get("String")
    return value == "SignalTermination"


def _module_boundary_net_names(module_instance: dict[str, Any]) -> set[str]:
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


def suppress_internal_signal_net_symbols(
    schematic: dict[str, Any],
    plan: LayoutPlan,
) -> LayoutPlan:
    """Remove label anchors that split an entirely local signal connection.

    A non-rail net joining two or more physical components inside one module
    should be drawn as their wire. A net symbol is retained when the net reaches
    that module's boundary, because it then represents a real external port.
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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        resolved_to_source = _resolved_to_source_ids(proposed, module)
        suppressed = {
            resolved_to_source[symbol_id]
            for symbol_id in _internal_signal_symbol_ids(
                resolved_positions,
                component_groups,
                nets,
                _module_boundary_net_names(module_instance),
            )
        }

        modules.append(
            replace(
                module,
                positions={
                    symbol_id: position
                    for symbol_id, position in module.positions.items()
                    if symbol_id not in suppressed
                },
            )
        )
    return replace(plan, modules=tuple(modules))


def refine_symbol_geometry(schematic: dict[str, Any], plan: LayoutPlan) -> LayoutPlan:
    """Apply the generic pin-aware refinement sequence to a semantic plan."""

    direct_wires = suppress_internal_signal_net_symbols(schematic, plan)
    clustered = cluster_local_decoupling(schematic, direct_wires)
    oriented = orient_two_terminal_components(schematic, clustered)
    connectors_oriented = orient_terminal_connectors_toward_series_banks(schematic, oriented)
    connectors_aligned = align_terminal_connectors_to_series_banks(schematic, connectors_oriented)
    pins_aligned = align_series_to_device_pins(
        schematic, connectors_aligned, maximum_adjustment=inf
    )
    leaves_aligned = align_leaf_series_endpoints(schematic, pins_aligned, maximum_adjustment=inf)
    leaves_hung = hang_leaf_series_from_device_pins(schematic, leaves_aligned)
    aligned = align_horizontal_series_terminals(
        schematic,
        leaves_hung,
        maximum_endpoint_spread=inf,
        maximum_adjustment=inf,
    )
    separated = separate_orthogonal_branch_lanes(schematic, aligned)
    compacted = compact_terminal_connector_gaps(schematic, separated)
    return cluster_local_decoupling(schematic, compacted)


def align_terminal_connectors_to_series_banks(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    maximum_adjustment: float = 500.0,
) -> LayoutPlan:
    """Translate a terminal connector so its bank pins meet opposite device pins.

    Connector and device pin pitches often match while their initial vertical
    origins do not. The median paired-pin delta moves the connector as a body;
    the later series alignment pass can then produce straight horizontal runs.
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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, positions)
        component_refs = tuple(component_groups)
        resolved_to_source = _resolved_to_source_ids(proposed, module)
        updated = dict(module.positions)

        series: list[tuple[str, dict[str, tuple[str, dict[str, Any]]]]] = []
        connectors: list[tuple[str, str, Position]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            symbol_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminals = _terminal_nets(component_ref, component, nets)
            if (_attribute_string(component, "type") or "").casefold() == "connector":
                connectors.append((component_ref, symbol_id, position))
                continue
            if len(terminals) != 2:
                continue
            terminal_names = tuple(sorted(terminals))
            if _two_terminal_axis(component, position, terminal_names) != "x":
                continue
            if any(_is_rail_net(net_ref, net) for net_ref, net in terminals.values()):
                continue
            series.append((component_ref, terminals))

        for connector_ref, connector_id, connector_position in sorted(connectors):
            connector = instances[connector_ref]
            connector_terminals = _terminal_nets(connector_ref, connector, nets)
            deltas: list[float] = []
            for series_ref, series_terminals in series:
                shared = [
                    (terminal, net_ref, net)
                    for terminal, (net_ref, net) in series_terminals.items()
                    if net_ref
                    in {candidate_ref for candidate_ref, _ in connector_terminals.values()}
                ]
                if len(shared) != 1:
                    continue
                _, shared_net_ref, shared_net = shared[0]
                connector_pin_names = [
                    terminal
                    for terminal, (net_ref, _) in connector_terminals.items()
                    if net_ref == shared_net_ref
                ]
                if len(connector_pin_names) != 1:
                    continue
                connector_pin = pin_position(connector, connector_position, connector_pin_names[0])
                opposite_nets = [
                    net for net_ref, net in series_terminals.values() if net_ref != shared_net_ref
                ]
                if len(opposite_nets) != 1:
                    continue
                opposite_net = opposite_nets[0]
                neighbours = {
                    neighbour_ref
                    for port_ref in opposite_net.get("ports", [])
                    if isinstance(port_ref, str)
                    if (neighbour_ref := _port_component_ref(port_ref, component_refs)) is not None
                    if neighbour_ref not in {series_ref, connector_ref}
                }
                if len(neighbours) != 1:
                    continue
                opposite_pin = _single_neighbour_pin_anchor(
                    opposite_net,
                    subject_ref=series_ref,
                    component_groups=component_groups,
                    instances=instances,
                    net_symbol_groups={},
                )
                if opposite_pin is None:
                    continue
                # The shared net object is deliberately resolved above: it
                # proves this is a direct connector-to-series branch.
                if not isinstance(shared_net, dict):
                    continue
                deltas.append(opposite_pin.y - connector_pin.y)

            if not deltas:
                continue
            delta_y = float(median(deltas))
            if abs(delta_y) > maximum_adjustment:
                continue
            source_id = resolved_to_source[connector_id]
            updated[source_id] = replace(updated[source_id], y=updated[source_id].y + delta_y)

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, positions)
        component_refs = tuple(component_groups)
        resolved_to_source = _resolved_to_source_ids(proposed, module)
        net_symbols: dict[str, list[tuple[str, Position]]] = {}
        for symbol_id, position in positions.items():
            if not symbol_id.startswith("sym:"):
                continue
            net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
            if separator and suffix.isdigit():
                net_symbols.setdefault(net_name, []).append((symbol_id, position))

        candidates: list[tuple[str, int, str, str, Point, Point, Position, Position]] = []
        claimed_leaves: set[str] = set()
        for series_ref, series_group in sorted(component_groups.items()):
            if len(series_group) != 1:
                continue
            series_id, series_position = series_group[0]
            series = instances.get(series_ref)
            if not isinstance(series, dict) or series.get("kind") != "Component":
                continue
            terminal_nets = _terminal_nets(series_ref, series, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if any(_is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue

            sides: list[tuple[str, dict[str, Any], set[str]]] = []
            for terminal, (_, net) in terminal_nets.items():
                neighbours = {
                    neighbour_ref
                    for port_ref in net.get("ports", [])
                    if isinstance(port_ref, str)
                    if (neighbour_ref := _port_component_ref(port_ref, component_refs)) is not None
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
                if (owner_ref := _port_component_ref(port_ref, component_refs)) is not None
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
                or (_attribute_string(owner, "type") or "").casefold() in _NON_OWNER_TYPES
            ):
                continue
            _, owner_position = owner_group[0]
            owner_pin = _single_neighbour_pin_anchor(
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
                    resolved_to_source[leaf_id],
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
                updated[leaf_source] = replace(
                    updated[leaf_source],
                    x=target_x + direction * leaf_offset,
                    y=target_y,
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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        component_refs = tuple(component_groups)
        resolved_to_source = _resolved_to_source_ids(proposed, module)
        updated = dict(module.positions)

        for component_ref, group in sorted(component_groups.items()):
            if len(group) != 1:
                continue
            resolved_id, component_position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminals = _terminal_nets(component_ref, component, nets)
            if len(terminals) != 2:
                continue
            terminal_names = tuple(sorted(terminals))
            if _two_terminal_axis(component, component_position, terminal_names) != "x":
                continue

            target_points: list[Point] = []
            for _, net in terminals.values():
                neighbours = {
                    neighbour_ref
                    for port_ref in net.get("ports", [])
                    if isinstance(port_ref, str)
                    if (neighbour_ref := _port_component_ref(port_ref, component_refs)) is not None
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
                    or (_attribute_string(neighbour, "type") or "").casefold() in _NON_OWNER_TYPES
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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        resolved_to_source = _resolved_to_source_ids(proposed, module)

        series: list[tuple[str, Position, set[str]]] = []
        branches: list[tuple[str, Position, str, str]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = _terminal_nets(component_ref, component, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            axis = _two_terminal_axis(component, position, terminals)
            rail_entries = [
                (net_ref, net)
                for net_ref, net in terminal_nets.values()
                if _is_rail_net(net_ref, net)
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
                        if not _is_rail_net(net_ref, net)
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


def orient_terminal_connectors_toward_series_banks(
    schematic: dict[str, Any], plan: LayoutPlan
) -> LayoutPlan:
    """Rotate terminal connectors so their connected pins face a series bank.

    Candidate rotations are scored from real evaluated pin geometry to the
    centres of directly connected horizontal two-terminal components. The
    current rotation wins exact ties, avoiding gratuitous changes. Connector
    classification and adjacency are both generic evaluated semantics.
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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        resolved_to_source = _resolved_to_source_ids(proposed, module)

        series_by_net: dict[str, list[Position]] = {}
        connectors: list[tuple[str, str, Position, dict[str, tuple[str, dict[str, Any]]]]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = _terminal_nets(component_ref, component, nets)
            if _attribute_string(component, "type") == "connector":
                connectors.append((component_ref, resolved_id, position, terminal_nets))
                continue
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if _two_terminal_axis(component, position, terminals) != "x":
                continue
            if any(_is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue
            for net_ref, _ in terminal_nets.values():
                series_by_net.setdefault(net_ref, []).append(position)

        updated = dict(module.positions)
        for connector_ref, connector_id, connector_position, terminal_nets in connectors:
            component = instances[connector_ref]
            evidenced_terminals = {
                terminal: series_by_net[net_ref]
                for terminal, (net_ref, _) in terminal_nets.items()
                if net_ref in series_by_net
            }
            if not evidenced_terminals:
                continue

            current_rotation = int(connector_position.rotation) % 360

            def rotation_score(rotation: int) -> tuple[float, int]:
                candidate_position = replace(connector_position, rotation=rotation)
                distance = 0.0
                for terminal, series_positions in evidenced_terminals.items():
                    terminal_position = pin_position(component, candidate_position, terminal)
                    distance += min(
                        abs(terminal_position.x - series_position.x)
                        + abs(terminal_position.y - series_position.y)
                        for series_position in series_positions
                    )
                return distance, 0 if rotation == current_rotation else 1

            best_rotation = min((0, 90, 180, 270), key=rotation_score)
            if best_rotation == current_rotation:
                continue
            connector_source_id = resolved_to_source[connector_id]
            updated[connector_source_id] = replace(
                updated[connector_source_id], rotation=best_rotation
            )

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))


def _single_neighbour_pin_anchor(
    net: dict[str, Any],
    *,
    subject_ref: str,
    component_groups: dict[str, list[tuple[str, Position]]],
    instances: dict[str, Any],
    net_symbol_groups: dict[str, list[Position]],
) -> Point | None:
    if _is_named_interface_net(net) and len(net_symbol_groups.get(net.get("name"), [])) >= 2:
        # This connection ends at its local named port, not the remote block.
        return None
    component_refs = tuple(component_groups)
    neighbours = {
        component_ref
        for port_ref in net.get("ports", [])
        if isinstance(port_ref, str)
        if (component_ref := _port_component_ref(port_ref, component_refs)) is not None
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
        return _center(net_symbol_groups[net_name])
    return None


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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        component_refs = tuple(component_groups)
        resolved_to_source = _resolved_to_source_ids(proposed, module)
        net_symbols: dict[str, list[tuple[str, Position]]] = {}
        for symbol_id, position in resolved_positions.items():
            if not symbol_id.startswith("sym:"):
                continue
            net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
            if separator and suffix.isdigit():
                net_symbols.setdefault(net_name, []).append((symbol_id, position))

        candidates: list[tuple[str, str, Point, Position, float]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, component_position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = _terminal_nets(component_ref, component, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if _two_terminal_axis(component, component_position, terminals) != "x":
                continue
            if any(_is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue

            side_data: list[tuple[dict[str, Any], set[str]]] = []
            for _, net in terminal_nets.values():
                neighbours = {
                    neighbour_ref
                    for port_ref in net.get("ports", [])
                    if isinstance(port_ref, str)
                    if (neighbour_ref := _port_component_ref(port_ref, component_refs)) is not None
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

            component_anchor = _single_neighbour_pin_anchor(
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
            leaf_anchor = Point(leaf_position.x, leaf_position.y)
            if _opposing_anchor_axis(component_anchor, leaf_anchor, terminal_center) != "x":
                continue
            if abs(component_anchor.y - terminal_center.y) > maximum_adjustment:
                continue
            candidates.append(
                (
                    resolved_to_source[resolved_id],
                    resolved_to_source[leaf_id],
                    terminal_center,
                    leaf_position,
                    component_anchor.y,
                )
            )

        updated = dict(module.positions)
        for candidate in sorted(candidates, key=lambda item: (item[2].x, item[4], item[0])):
            series_source_id, leaf_source_id, terminal_center, leaf_position, target_y = candidate
            delta_y = target_y - terminal_center.y
            if abs(delta_y) > maximum_adjustment:
                continue
            updated[series_source_id] = replace(
                updated[series_source_id],
                y=updated[series_source_id].y + delta_y,
            )
            updated[leaf_source_id] = replace(
                updated[leaf_source_id],
                y=updated[leaf_source_id].y + target_y - leaf_position.y,
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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        resolved_to_source = _resolved_to_source_ids(proposed, module)
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
            terminal_nets = _terminal_nets(component_ref, component, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if _two_terminal_axis(component, component_position, terminals) != "x":
                continue
            if any(_is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue

            first_anchor = _single_neighbour_pin_anchor(
                terminal_nets[terminals[0]][1],
                subject_ref=component_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups=net_symbol_groups,
            )
            second_anchor = _single_neighbour_pin_anchor(
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
            if _opposing_anchor_axis(first_anchor, second_anchor, terminal_center) != "x":
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


def schematic_quality_findings(
    schematic: dict[str, Any],
    *,
    maximum_series_dogleg: float = 1.0,
    maximum_local_passive_gap: float = 40.0,
    maximum_connected_component_gap: float = 400.0,
    minimum_bank_pitch: float = 90.0,
    lane_tolerance: float = 40.0,
    bank_group_span: float = 400.0,
) -> tuple[QualityFinding, ...]:
    """Measure detached components, series doglegs, and compressed banks.

    Findings are review evidence rather than hard electrical errors. The same
    ambiguity rules as alignment apply, so shared fan-in/fan-out topology is
    not mislabeled as a correctable two-endpoint dogleg.
    """

    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if not isinstance(instances, dict) or not isinstance(nets, dict):
        raise ToolchainError("schematic instances and nets must be objects")

    findings: list[QualityFinding] = []
    for module_ref, module_instance in instances.items():
        if not isinstance(module_ref, str) or not isinstance(module_instance, dict):
            continue
        raw_positions = module_instance.get("symbol_positions")
        if not isinstance(raw_positions, dict) or not raw_positions:
            continue
        positions = {
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module_ref, positions)
        net_symbol_groups: dict[str, list[Position]] = {}
        for symbol_id, position in positions.items():
            if not symbol_id.startswith("sym:"):
                continue
            net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
            if separator and suffix.isdigit():
                net_symbol_groups.setdefault(net_name, []).append(position)

        for symbol_id in sorted(
            _internal_signal_symbol_ids(
                positions,
                component_groups,
                nets,
                _module_boundary_net_names(module_instance),
            )
        ):
            findings.append(
                QualityFinding(
                    code="internal-signal-net-symbol",
                    module_ref=module_ref,
                    symbol_ids=(symbol_id,),
                    measured=1.0,
                    threshold=0.0,
                    message="an entirely local signal net is split by a label instead of a wire",
                )
            )

        component_refs = tuple(component_groups)
        for component_ref, group in sorted(component_groups.items()):
            if len(group) != 1:
                continue
            symbol_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            connection_gaps: list[float] = []
            terminals_by_net: dict[str, tuple[dict[str, Any], list[str]]] = {}
            for terminal, (net_ref, net) in _terminal_nets(component_ref, component, nets).items():
                terminals_by_net.setdefault(net_ref, (net, []))[1].append(terminal)
            for net_ref, (net, terminals) in sorted(terminals_by_net.items()):
                own_points = tuple(
                    point
                    for terminal in terminals
                    for point in pin_positions(component, position, terminal)
                )
                peers = {
                    peer_ref
                    for port_ref in net.get("ports", ())
                    if isinstance(port_ref, str)
                    if (peer_ref := _port_component_ref(port_ref, component_refs)) is not None
                    if peer_ref != component_ref
                }
                terminal_gaps: list[float] = []
                for peer_ref in peers:
                    peer_group = component_groups.get(peer_ref)
                    peer = instances.get(peer_ref)
                    if not peer_group or len(peer_group) != 1 or not isinstance(peer, dict):
                        continue
                    _, peer_position = peer_group[0]
                    prefix = peer_ref + "."
                    peer_terminals = {
                        port_ref.removeprefix(prefix)
                        for port_ref in net.get("ports", ())
                        if isinstance(port_ref, str) and port_ref.startswith(prefix)
                    }
                    peer_points = tuple(
                        point
                        for peer_terminal in peer_terminals
                        if "." not in peer_terminal
                        for point in pin_positions(peer, peer_position, peer_terminal)
                    )
                    terminal_gaps.extend(
                        hypot(own.x - other.x, own.y - other.y)
                        for own in own_points
                        for other in peer_points
                    )
                connection_gaps.extend(terminal_gaps)
                if (
                    terminal_gaps
                    and not _is_rail_net(net_ref, net)
                    and not (
                        _is_named_interface_net(net)
                        and len(net_symbol_groups.get(net_ref, [])) >= 2
                    )
                    and min(terminal_gaps) > maximum_connected_component_gap
                ):
                    findings.append(
                        QualityFinding(
                            code="detached-local-signal",
                            module_ref=module_ref,
                            symbol_ids=(symbol_id, f"net:{net_ref}"),
                            measured=min(terminal_gaps),
                            threshold=maximum_connected_component_gap,
                            message=("non-rail terminal has no nearby component peer on its net"),
                        )
                    )
            if connection_gaps and min(connection_gaps) > maximum_connected_component_gap:
                findings.append(
                    QualityFinding(
                        code="detached-connected-component",
                        module_ref=module_ref,
                        symbol_ids=(symbol_id,),
                        measured=min(connection_gaps),
                        threshold=maximum_connected_component_gap,
                        message="connected component has no nearby visual peer",
                    )
                )

        for capacitor_ref, capacitor_group in sorted(component_groups.items()):
            capacitor = instances.get(capacitor_ref)
            if (
                not isinstance(capacitor, dict)
                or capacitor.get("kind") != "Component"
                or _attribute_string(capacitor, "type") != "capacitor"
                or len(capacitor_group) != 1
            ):
                continue
            terminals = _terminal_nets(capacitor_ref, capacitor, nets)
            if len(terminals) != 2:
                continue
            return_entries = [
                (net_ref, net)
                for net_ref, net in terminals.values()
                if _is_return_net(net_ref, net)
            ]
            supply_entries = [
                (net_ref, net)
                for net_ref, net in terminals.values()
                if _is_rail_net(net_ref, net) and not _is_return_net(net_ref, net)
            ]
            if len(return_entries) != 1 or len(supply_entries) != 1:
                continue

            capacitor_bounds = _group_bounds(capacitor, capacitor_group)
            capacitor_nets = {return_entries[0][0], supply_entries[0][0]}
            owner_gaps: list[float] = []
            for owner_ref, owner_group in component_groups.items():
                if owner_ref == capacitor_ref:
                    continue
                owner = instances.get(owner_ref)
                if not isinstance(owner, dict) or owner.get("kind") != "Component":
                    continue
                if (_attribute_string(owner, "type") or "").casefold() in _NON_OWNER_TYPES:
                    continue
                owner_nets = {
                    net_ref for net_ref, _ in _terminal_nets(owner_ref, owner, nets).values()
                }
                if not capacitor_nets <= owner_nets:
                    continue
                owner_bounds = _group_bounds(owner, owner_group)
                delta_x = max(
                    owner_bounds.min_x - capacitor_bounds.max_x,
                    capacitor_bounds.min_x - owner_bounds.max_x,
                    0.0,
                )
                delta_y = max(
                    owner_bounds.min_y - capacitor_bounds.max_y,
                    capacitor_bounds.min_y - owner_bounds.max_y,
                    0.0,
                )
                owner_gaps.append(hypot(delta_x, delta_y))

            if not owner_gaps:
                continue
            owner_gap = min(owner_gaps)
            if owner_gap > maximum_local_passive_gap:
                findings.append(
                    QualityFinding(
                        code="local-passive-owner-gap",
                        module_ref=module_ref,
                        symbol_ids=(capacitor_group[0][0],),
                        measured=owner_gap,
                        threshold=maximum_local_passive_gap,
                        message="local rail passive is visually detached from its owner",
                    )
                )

        series: list[tuple[str, Point]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            symbol_id, component_position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = _terminal_nets(component_ref, component, nets)
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if _two_terminal_axis(component, component_position, terminals) != "x":
                continue
            if any(_is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue
            terminal_points = [
                pin_position(component, component_position, terminal) for terminal in terminals
            ]
            terminal_center = Point(
                x=float(median(point.x for point in terminal_points)),
                y=float(median(point.y for point in terminal_points)),
            )
            series.append((symbol_id, terminal_center))

            first_anchor = _single_neighbour_pin_anchor(
                terminal_nets[terminals[0]][1],
                subject_ref=component_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups=net_symbol_groups,
            )
            second_anchor = _single_neighbour_pin_anchor(
                terminal_nets[terminals[1]][1],
                subject_ref=component_ref,
                component_groups=component_groups,
                instances=instances,
                net_symbol_groups=net_symbol_groups,
            )
            if first_anchor is None or second_anchor is None:
                continue
            if _opposing_anchor_axis(first_anchor, second_anchor, terminal_center) != "x":
                continue
            dogleg = max(
                abs(terminal_points[0].y - first_anchor.y),
                abs(terminal_points[1].y - second_anchor.y),
            )
            if dogleg > maximum_series_dogleg:
                findings.append(
                    QualityFinding(
                        code="series-terminal-dogleg",
                        module_ref=module_ref,
                        symbol_ids=(symbol_id,),
                        measured=dogleg,
                        threshold=maximum_series_dogleg,
                        message="simple series terminals exceed the vertical dogleg allowance",
                    )
                )

        lanes: list[list[tuple[str, Point]]] = []
        for candidate in sorted(series, key=lambda item: (item[1].x, item[1].y, item[0])):
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
            ordered = sorted(lane, key=lambda item: (item[1].y, item[0]))
            for first, second in zip(ordered, ordered[1:], strict=False):
                pitch = second[1].y - first[1].y
                if pitch > bank_group_span or pitch + 1e-6 >= minimum_bank_pitch:
                    continue
                findings.append(
                    QualityFinding(
                        code="series-bank-pitch",
                        module_ref=module_ref,
                        symbol_ids=(first[0], second[0]),
                        measured=pitch,
                        threshold=minimum_bank_pitch,
                        message="repeated series members are too close for annotation clearance",
                    )
                )

    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                finding.module_ref,
                finding.code,
                finding.symbol_ids,
            ),
        )
    )


def compact_terminal_connector_gaps(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    maximum_gap: float = 450.0,
    target_gap: float = 400.0,
    support_radius: float = 750.0,
    attached_symbol_radius: float = 800.0,
) -> LayoutPlan:
    """Pull remote terminal connectors toward an adjacent horizontal series bank.

    A connector at the end of one or more series branches should read as the
    endpoint of those branches, not as an apparently unrelated island. This
    pass finds connector components from evaluated type metadata, identifies
    directly connected horizontal two-terminal branches, and closes only
    excessive terminal gaps. Nearby one-rail support branches and local net
    symbols on the connector's own nets move with it so the endpoint cluster
    remains coherent.

    Selection is entirely semantic and geometric: there are no board names,
    reference designators, package names, or component-specific exceptions.
    """

    if target_gap < 0 or maximum_gap <= target_gap:
        raise ValueError("terminal connector gap limits must satisfy 0 <= target < maximum")

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
            symbol_id: _viewer_position(raw)
            for symbol_id, raw in raw_positions.items()
            if isinstance(symbol_id, str) and isinstance(raw, dict)
        }
        component_groups = _component_groups(module.instance_ref, resolved_positions)
        resolved_to_source = _resolved_to_source_ids(proposed, module)

        component_terminals: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {}
        horizontal_series: dict[str, tuple[Position, frozenset[str]]] = {}
        connectors: list[tuple[str, str, Position, frozenset[str]]] = []
        for component_ref, group in component_groups.items():
            if len(group) != 1:
                continue
            resolved_id, position = group[0]
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            terminal_nets = _terminal_nets(component_ref, component, nets)
            component_terminals[component_ref] = terminal_nets
            net_refs = frozenset(net_ref for net_ref, _ in terminal_nets.values())
            if _attribute_string(component, "type") == "connector":
                connectors.append((component_ref, resolved_id, position, net_refs))
                continue
            if len(terminal_nets) != 2:
                continue
            terminals = tuple(sorted(terminal_nets))
            if _two_terminal_axis(component, position, terminals) != "x":
                continue
            if any(_is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()):
                continue
            horizontal_series[component_ref] = (position, net_refs)

        updated = dict(module.positions)
        moved_resolved_positions = dict(resolved_positions)
        claimed_components: set[str] = set()
        claimed_symbols: set[str] = set()
        for connector_ref, connector_id, connector_position, connector_nets in sorted(connectors):
            adjacent = [
                position
                for position, net_refs in horizontal_series.values()
                if connector_nets & net_refs
            ]
            if not adjacent:
                continue
            bank_x = float(median(position.x for position in adjacent))
            if (
                min(position.x for position in adjacent)
                < connector_position.x
                < max(position.x for position in adjacent)
            ):
                continue
            direction = 1.0 if bank_x > connector_position.x else -1.0
            gap = abs(bank_x - connector_position.x)
            if gap <= maximum_gap:
                continue
            target_x = bank_x - direction * target_gap
            delta_x = target_x - connector_position.x

            connector_source_id = resolved_to_source[connector_id]
            updated[connector_source_id] = replace(
                updated[connector_source_id], x=updated[connector_source_id].x + delta_x
            )
            moved_resolved_positions[connector_id] = replace(connector_position, x=target_x)
            claimed_components.add(connector_ref)

            for support_ref, terminal_nets in component_terminals.items():
                if support_ref in claimed_components or len(terminal_nets) != 2:
                    continue
                support_group = component_groups[support_ref]
                support_id, support_position = support_group[0]
                distance = abs(support_position.x - connector_position.x) + abs(
                    support_position.y - connector_position.y
                )
                if distance > support_radius:
                    continue
                support_net_refs = {net_ref for net_ref, _ in terminal_nets.values()}
                rail_count = sum(
                    _is_rail_net(net_ref, net) for net_ref, net in terminal_nets.values()
                )
                if rail_count != 1 or not connector_nets & support_net_refs:
                    continue
                support_source_id = resolved_to_source[support_id]
                updated[support_source_id] = replace(
                    updated[support_source_id],
                    x=updated[support_source_id].x + delta_x,
                )
                moved_resolved_positions[support_id] = replace(
                    support_position, x=support_position.x + delta_x
                )
                claimed_components.add(support_ref)

            connector_net_names = {
                str(net.get("name", net_ref))
                for net_ref, net in component_terminals[connector_ref].values()
            }
            for symbol_id, symbol_position in tuple(moved_resolved_positions.items()):
                if not symbol_id.startswith("sym:") or symbol_id in claimed_symbols:
                    continue
                net_name, separator, suffix = symbol_id.removeprefix("sym:").rpartition("#")
                if not separator or not suffix.isdigit() or net_name not in connector_net_names:
                    continue
                distance = abs(symbol_position.x - connector_position.x) + abs(
                    symbol_position.y - connector_position.y
                )
                if distance > attached_symbol_radius:
                    continue
                symbol_source_id = resolved_to_source[symbol_id]
                updated[symbol_source_id] = replace(
                    updated[symbol_source_id], x=updated[symbol_source_id].x + delta_x
                )
                moved_resolved_positions[symbol_id] = replace(
                    symbol_position, x=symbol_position.x + delta_x
                )
                claimed_symbols.add(symbol_id)

        modules.append(replace(module, positions=updated))
    return replace(plan, modules=tuple(modules))
