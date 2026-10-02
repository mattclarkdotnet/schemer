from __future__ import annotations

import re
from math import cos, radians, sin
from typing import Any

from schemer.core.attributes import attribute_string
from schemer.core.errors import ToolchainError
from schemer.symbols.model import NUMBER_PATTERN, VIEWER_BOUNDS_EXPANSION_MM, SymbolBounds


def balanced_blocks(source: str, head: str) -> list[str]:
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


def pin_identifiers(instance: dict[str, Any], block: str) -> tuple[str, ...]:
    """Resolve authored terminal aliases through physical pad identity."""

    number = re.search(r'\(number\s+"([^"]*)"', block)
    physical = number.group(1) if number else ""
    aliases = instance.get("_pin_numbers", {})
    identifiers = []
    for field in ("name", "number"):
        match = re.search(rf'\({field}\s+"([^"]*)"', block)
        if match and (name := match.group(1)):
            if name not in aliases or physical in aliases[name]:
                identifiers.append(name)
    identifiers.extend(alias for alias, pads in aliases.items() if physical in pads)
    return tuple(dict.fromkeys(identifiers))


def symbol_pin_offsets(instance: dict[str, Any]) -> dict[str, tuple[float, float]]:
    """Read named and numbered pin offsets from an evaluated KiCad symbol."""
    return {name: offsets[-1] for name, offsets in symbol_pin_offset_groups(instance).items()}


def symbol_pin_offset_groups(
    instance: dict[str, Any],
) -> dict[str, tuple[tuple[float, float], ...]]:
    """Read every physical pin offset grouped by logical KiCad pin name.

    A logical terminal may be painted more than once, such as two ground pins
    on one IC face. Unlike ``symbol_pin_offsets``, this representation does not
    discard duplicate names.
    """

    symbol = attribute_string(instance, "__symbol_value")
    if symbol is None:
        return {}
    grouped: dict[str, list[tuple[float, float]]] = {}
    for block in balanced_blocks(symbol, "pin"):
        at_match = re.search(
            rf"\(at\s+({NUMBER_PATTERN})\s+({NUMBER_PATTERN})(?:\s+{NUMBER_PATTERN})?\)",
            block,
        )
        if at_match is None:
            continue
        offset = (float(at_match.group(1)), float(at_match.group(2)))
        for name in pin_identifiers(instance, block):
            grouped.setdefault(name, []).append(offset)
    return {name: tuple(offsets) for name, offsets in grouped.items()}


def symbol_pin_electrical_types(instance: dict[str, Any]) -> dict[str, str]:
    """Read KiCad electrical pin types by both pin name and pin number."""

    symbol = attribute_string(instance, "__symbol_value")
    if symbol is None:
        return {}
    result: dict[str, str] = {}
    for block in balanced_blocks(symbol, "pin"):
        type_match = re.match(r"\(pin\s+([a-zA-Z0-9_]+)\s+", block)
        if type_match is None:
            continue
        electrical_type = type_match.group(1)
        for name in pin_identifiers(instance, block):
            result[name] = electrical_type
    return result


def symbol_pin_number_groups(instance: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Retain every physical pin belonging to a logical terminal."""

    symbol = attribute_string(instance, "__symbol_value")
    result: dict[str, tuple[str, ...]] = {}
    for block in balanced_blocks(symbol or "", "pin"):
        number = re.search(r'\(number\s+"([^"]*)"', block)
        if number and number.group(1):
            for name in pin_identifiers(instance, block):
                previous = result.get(name, ())
                if number.group(1) not in previous:
                    result[name] = (*previous, number.group(1))
    return result


def symbol_pin_numbers(instance: dict[str, Any]) -> dict[str, str]:
    """Map each non-empty KiCad pin name to its physical pin number."""

    symbol = attribute_string(instance, "__symbol_value")
    if symbol is None:
        return {}
    result: dict[str, str] = {}
    for block in balanced_blocks(symbol, "pin"):
        name_match = re.search(r'\(name\s+"([^"]*)"', block)
        number_match = re.search(r'\(number\s+"([^"]*)"', block)
        if (
            name_match is not None
            and name_match.group(1)
            and number_match is not None
            and number_match.group(1)
        ):
            result[name_match.group(1)] = number_match.group(1)
    result.update({name: pads[-1] for name, pads in instance.get("_pin_numbers", {}).items()})
    return result


def _symbol_local_bounds(
    instance: dict[str, Any],
    *,
    include_pins: bool,
) -> SymbolBounds:
    """Calculate local primitive bounds, optionally including pin strokes."""

    symbol = attribute_string(instance, "__symbol_value")
    if symbol is None:
        raise ToolchainError("evaluated component has no symbol geometry")
    points: list[tuple[float, float]] = []

    def add_fields(block: str, fields: tuple[str, ...]) -> None:
        for field in fields:
            for match in re.finditer(
                rf"\({field}\s+({NUMBER_PATTERN})\s+({NUMBER_PATTERN})\)", block
            ):
                points.append((float(match.group(1)), float(match.group(2))))

    for block in balanced_blocks(symbol, "rectangle"):
        add_fields(block, ("start", "end"))
    for tag in ("polyline", "bezier"):
        for block in balanced_blocks(symbol, tag):
            add_fields(block, ("xy",))
    for block in balanced_blocks(symbol, "circle"):
        center = re.search(rf"\(center\s+({NUMBER_PATTERN})\s+({NUMBER_PATTERN})\)", block)
        radius = re.search(rf"\(radius\s+({NUMBER_PATTERN})\)", block)
        if center is not None and radius is not None:
            x = float(center.group(1))
            y = float(center.group(2))
            value = float(radius.group(1))
            points.extend(((x - value, y - value), (x + value, y + value)))
    for block in balanced_blocks(symbol, "arc"):
        add_fields(block, ("start", "mid", "end"))
    if include_pins:
        for block in balanced_blocks(symbol, "pin"):
            at = re.search(
                rf"\(at\s+({NUMBER_PATTERN})\s+({NUMBER_PATTERN})(?:\s+({NUMBER_PATTERN}))?\)",
                block,
            )
            if at is None:
                continue
            x = float(at.group(1))
            y = float(at.group(2))
            angle = radians(float(at.group(3) or 0.0))
            length_match = re.search(rf"\(length\s+({NUMBER_PATTERN})\)", block)
            length = float(length_match.group(1)) if length_match is not None else 0.0
            points.extend(((x, y), (x + length * cos(angle), y + length * sin(angle))))

    if not points:
        raise ToolchainError("evaluated symbol has no bounded primitives")
    expansion = VIEWER_BOUNDS_EXPANSION_MM
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
