from __future__ import annotations

import re
from math import cos, radians, sin
from typing import Any

from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.symbols.library import (
    balanced_blocks,
    pin_identifiers,
    symbol_body_local_bounds,
    symbol_local_bounds,
    symbol_pin_offset_groups,
    symbol_pin_offsets,
)
from schemer.symbols.model import (
    NUMBER_PATTERN,
    VIEWER_UNITS_PER_MM,
    PlacedBounds,
    Point,
    SymbolBounds,
)


def pin_outward_side(instance: dict[str, Any], position: Position, terminal: str) -> str:
    """Read a terminal's stroke direction, including pins at bounding-box corners."""
    if position.mirror is not None:
        raise ToolchainError("pin-exit hints do not yet support mirrored symbols")
    symbol = attribute_string(instance, "__symbol_value") or ""
    sides = set()
    for block in balanced_blocks(symbol, "pin"):
        names = pin_identifiers(instance, block)
        if terminal not in names:
            continue
        at = re.search(rf"\(at\s+{NUMBER_PATTERN}\s+{NUMBER_PATTERN}\s+({NUMBER_PATTERN})\)", block)
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


def placed_symbol_origin(
    instance: dict[str, Any],
    position: Position,
) -> tuple[float, float]:
    """Recover the KiCad origin from the viewer's rotated top-left anchor.

    Persisted x/y coordinates identify the top-left of the symbol *after*
    rotation.  Using the unrotated bounds happens to work at 0 degrees, but
    shifts every non-square symbol at 90/270 degrees.  That mismatch makes
    otherwise collinear wire endpoints appear one symbol half-span apart.
    """

    bounds = symbol_local_bounds(instance)
    corners = (
        (bounds.min_x, bounds.min_y),
        (bounds.min_x, bounds.max_y),
        (bounds.max_x, bounds.min_y),
        (bounds.max_x, bounds.max_y),
    )
    transformed = [rotated_offset(corner, position.rotation) for corner in corners]
    return (
        position.x - min(point[0] for point in transformed) * VIEWER_UNITS_PER_MM,
        position.y - min(point[1] for point in transformed) * VIEWER_UNITS_PER_MM,
    )


def placed_symbol_bounds(instance: dict[str, Any], position: Position) -> PlacedBounds:
    """Resolve one stored symbol anchor into its visible geometry bounds."""

    if position.mirror is not None:
        raise ToolchainError("placed bounds do not yet support mirrored symbols")
    return _placed_bounds(
        symbol_local_bounds(instance), placed_symbol_origin(instance, position), position.rotation,
    )


def placed_symbol_body_bounds(instance: dict[str, Any], position: Position) -> PlacedBounds:
    """Resolve painted body geometry using the viewer's complete-symbol anchor."""

    if position.mirror is not None:
        raise ToolchainError("placed bounds do not yet support mirrored symbols")
    return _placed_bounds(
        symbol_body_local_bounds(instance),
        placed_symbol_origin(instance, position),
        position.rotation,
    )


def _placed_bounds(
    bounds: SymbolBounds, origin: tuple[float, float], rotation: float,
) -> PlacedBounds:
    origin_x, origin_y = origin
    corners = (
        (bounds.min_x, bounds.min_y),
        (bounds.min_x, bounds.max_y),
        (bounds.max_x, bounds.min_y),
        (bounds.max_x, bounds.max_y),
    )
    transformed = [
        (
            origin_x + rotated_offset(corner, rotation)[0] * VIEWER_UNITS_PER_MM,
            origin_y + rotated_offset(corner, rotation)[1] * VIEWER_UNITS_PER_MM,
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
    origin_x, origin_y = placed_symbol_origin(instance, position)
    offset_x, offset_y = rotated_offset(offsets[pin_name], position.rotation)
    return Point(
        origin_x + offset_x * VIEWER_UNITS_PER_MM,
        origin_y + offset_y * VIEWER_UNITS_PER_MM,
    )


def pin_positions(instance: dict[str, Any], position: Position, pin_name: str) -> tuple[Point, ...]:
    """Resolve every physical occurrence of one logical terminal."""

    if position.mirror is not None:
        raise ToolchainError("pin geometry validation does not yet support mirrored symbols")
    grouped = symbol_pin_offset_groups(instance)
    offsets = grouped.get(pin_name)
    if not offsets:
        raise ToolchainError(f"evaluated symbol has no pin geometry for {pin_name!r}")
    origin_x, origin_y = placed_symbol_origin(instance, position)
    result: list[Point] = []
    for offset in offsets:
        offset_x, offset_y = rotated_offset(offset, position.rotation)
        result.append(
            Point(
                origin_x + offset_x * VIEWER_UNITS_PER_MM,
                origin_y + offset_y * VIEWER_UNITS_PER_MM,
            )
        )
    return tuple(result)
