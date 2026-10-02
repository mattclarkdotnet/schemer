from __future__ import annotations

from dataclasses import replace
from statistics import median

from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.placement.circuits.model import Component, SeriesLink
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_bounds,
)
from schemer.symbols.model import PlacedBounds, Point


def median_point(points: tuple[Point, ...]) -> Point:
    return Point(
        float(median(point.x for point in points)),
        float(median(point.y for point in points)),
    )


def central_orientation(
    central: Component,
    connectors: tuple[Component, ...],
    links: tuple[SeriesLink, ...],
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
            centres.append(median_point(points))
        horizontal = abs(centres[1].x - centres[0].x)
        vertical = abs(centres[1].y - centres[0].y)
        candidates.append((horizontal - vertical, -rotation, position))
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def connector_net_point(
    connector: Component,
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
    return median_point(points)


def connector_orientation(
    connector: Component,
    links: tuple[SeriesLink, ...],
    *,
    side: str,
) -> Position:
    candidates: list[tuple[float, float, Position]] = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        bounds = placed_symbol_bounds(connector.instance, position)
        pin_x = float(
            median(
                connector_net_point(connector, position, link.connector_net).x for link in links
            )
        )
        outward = pin_x - bounds.center_x if side == "left" else bounds.center_x - pin_x
        candidates.append((outward, -rotation, position))
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def translate_pin_to(position: Position, point: Point, target: Point) -> Position:
    return replace(
        position,
        x=position.x + target.x - point.x,
        y=position.y + target.y - point.y,
    )


def oriented_terminal_vector(
    component: Component,
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
        attached = median_point(pin_positions(component.instance, position, attached_terminal))
        remote = median_point(pin_positions(component.instance, position, remote_terminal))
        along = remote.x - attached.x if axis == "x" else remote.y - attached.y
        across = remote.y - attached.y if axis == "x" else remote.x - attached.x
        candidates.append((abs(along) - abs(across), direction * along, -rotation, position))
    return max(candidates, key=lambda candidate: candidate[:3])[3]


def active_orientation_toward(
    component: Component,
    terminal: str,
    *,
    outward_direction: float,
) -> Position:
    """Orient an active device so one terminal faces its owner horizontally."""

    candidates: list[tuple[float, float, Position]] = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        bounds = placed_symbol_bounds(component.instance, position)
        pin = median_point(pin_positions(component.instance, position, terminal))
        inward_score = bounds.center_x - pin.x if outward_direction > 0 else pin.x - bounds.center_x
        candidates.append((inward_score, -rotation, position))
    return max(candidates, key=lambda candidate: candidate[:2])[2]


def passive_orientation(
    passive: Component,
    first_terminal: str,
    second_terminal: str,
) -> Position:
    candidates: list[tuple[float, float, Position]] = []
    for rotation in (90.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        first = median_point(pin_positions(passive.instance, position, first_terminal))
        second = median_point(pin_positions(passive.instance, position, second_terminal))
        candidates.append((second.x - first.x, -rotation, position))
    return max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[2]


def pin_side_near_bounds(point: Point, bounds: PlacedBounds, *, prefer: str | None = None) -> str:
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


def fan_in_continuation_coordinate(coordinates: tuple[float, ...]) -> float:
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


def orientation_for_terminal_sides(
    component: Component,
    desired_sides: dict[str, str],
) -> Position:
    """Choose the cardinal orientation best matching evidenced pin roles."""

    candidates: list[tuple[int, float, Position]] = []
    for rotation in (0.0, 90.0, 180.0, 270.0):
        position = Position(0.0, 0.0, rotation)
        bounds = placed_symbol_bounds(component.instance, position)
        matches = sum(
            pin_side_near_bounds(
                median_point(pin_positions(component.instance, position, terminal)),
                bounds,
                prefer=side,
            )
            == side
            for terminal, side in desired_sides.items()
        )
        candidates.append((matches, -rotation, position))
    return max(candidates, key=lambda candidate: candidate[:2])[2]
