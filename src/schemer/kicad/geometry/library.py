from __future__ import annotations

import re
from dataclasses import dataclass
from math import cos, radians, sin

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.document import KiCadSchematicDocument
from schemer.kicad.items import Vector2
from schemer.kicad.records import KiCadSymbol
from schemer.kicad.syntax import ListExpr, sexpr_at, sexpr_atom, sexpr_number


@dataclass(frozen=True)
class LibraryPin:
    number: str
    name: str
    offset_mm: tuple[float, float]
    orientation: float
    electrical_type: str = "passive"
    length_mm: float = 0.0


def symbol_library_pins(
    document: KiCadSchematicDocument,
    symbol: KiCadSymbol,
) -> dict[str, LibraryPin]:
    """Return the pins painted by one placed symbol unit, keyed by number."""

    definition = library_definition(document, symbol)

    instance_numbers = {
        sexpr_atom(pin, 1, f"symbol {symbol.reference} pin").value
        for pin in symbol.expression.lists("pin")
    }
    pins: dict[str, LibraryPin] = {}
    for pin in pins_for_unit(definition, symbol.unit):
        number_expression = pin.first_list("number")
        name_expression = pin.first_list("name")
        if number_expression is None:
            continue
        number = sexpr_atom(number_expression, 1, "pin number").value
        if instance_numbers and number not in instance_numbers:
            continue
        name = sexpr_atom(name_expression, 1, "pin name").value if name_expression else ""
        x, y, orientation = sexpr_at(pin, f"pin {number}")
        length = pin.first_list("length")
        candidate = LibraryPin(number, name, (x, y), orientation,
                               sexpr_atom(pin, 1, "pin electrical type").value,
                               0.0 if length is None else sexpr_number(
                                   sexpr_atom(length, 1, "pin length"), "pin length"))
        previous = pins.get(number)
        if previous is not None and previous != candidate:
            raise KiCadSchematicError(
                f"symbol {symbol.reference} unit {symbol.unit} has ambiguous pin {number}"
            )
        pins[number] = candidate

    missing = instance_numbers - set(pins)
    if missing:
        raise KiCadSchematicError(
            f"symbol {symbol.reference} unit {symbol.unit} lacks library pins: "
            + ", ".join(sorted(missing))
        )
    return pins


def placed_symbol_body_positions(
    document: KiCadSchematicDocument,
    symbol: KiCadSymbol,
    *, fallback_to_pins: bool = True,
) -> tuple[Vector2, ...]:
    """Return painted body points in sheet coordinates.

    Pin anchors alone do not describe a symbol's height: the two pins of a
    horizontal gate share one Y coordinate while its triangle extends well
    above and below them.  Field placement and collision envelopes therefore
    use the embedded library graphics and fall back to pins only for symbols
    with no painted primitives. Routing must disable that fallback: pin bounds
    are a placement envelope, not a solid body that blocks their own exits.
    """

    definition = library_definition(document, symbol)
    points: list[tuple[float, float]] = []
    for expression in _expressions_for_unit(definition, symbol.unit):
        for child in expression.children[1:]:
            if not isinstance(child, ListExpr):
                continue
            if child.tag == "rectangle":
                for tag in ("start", "end"):
                    point = child.first_list(tag)
                    if point is not None:
                        points.append(_xy(point, f"{symbol.reference} rectangle {tag}"))
            elif child.tag == "polyline":
                pts = child.first_list("pts")
                if pts is not None:
                    points.extend(
                        _xy(point, f"{symbol.reference} polyline")
                        for point in pts.lists("xy")
                    )
            elif child.tag == "arc":
                for tag in ("start", "mid", "end"):
                    point = child.first_list(tag)
                    if point is not None:
                        points.append(_xy(point, f"{symbol.reference} arc {tag}"))
            elif child.tag == "circle":
                center = child.first_list("center")
                radius = child.first_list("radius")
                if center is not None and radius is not None:
                    center_x, center_y = _xy(center, f"{symbol.reference} circle")
                    radius_value = sexpr_number(
                        sexpr_atom(radius, 1, f"{symbol.reference} circle radius"),
                        f"{symbol.reference} circle radius",
                    )
                    points.extend(
                        (
                            (center_x - radius_value, center_y - radius_value),
                            (center_x + radius_value, center_y + radius_value),
                        )
                    )

    if not points:
        return tuple(placed_pin_positions(document, symbol).values()) if fallback_to_pins else ()

    return tuple(_placed_point(symbol, x, y) for x, y in points)


def placed_pin_positions(
    document: KiCadSchematicDocument,
    symbol: KiCadSymbol,
) -> dict[str, Vector2]:
    """Resolve one placed unit's electrical pin anchors in sheet coordinates."""

    return {number: _placed_point(symbol, *pin.offset_mm)
            for number, pin in symbol_library_pins(document, symbol).items()}


def placed_pin_sides(
    document: KiCadSchematicDocument,
    symbol: KiCadSymbol,
) -> dict[str, str]:
    """Return each placed pin's outward sheet direction."""

    result = {}
    for number, pin in symbol_library_pins(document, symbol).items():
        angle = radians(pin.orientation)
        dx, dy = _placed_offset(symbol, -cos(angle), -sin(angle))
        if abs(dx) >= abs(dy):
            result[number] = "right" if dx > 0 else "left"
        else:
            result[number] = "bottom" if dy > 0 else "top"
    return result


def placed_pin_segments(
    document: KiCadSchematicDocument,
    symbol: KiCadSymbol,
) -> dict[str, tuple[Vector2, Vector2]]:
    """Resolve the complete pin strokes, not only their electrical endpoints."""

    positions = placed_pin_positions(document, symbol)
    result = {}
    for number, pin in symbol_library_pins(document, symbol).items():
        angle = radians(pin.orientation)
        x = pin.offset_mm[0] + pin.length_mm * cos(angle)
        y = pin.offset_mm[1] + pin.length_mm * sin(angle)
        inner = _placed_point(symbol, x, y)
        result[number] = (positions[number], inner)
    return result


def pins_for_unit(definition: ListExpr, unit: int) -> tuple[ListExpr, ...]:
    return tuple(pin for expression in _expressions_for_unit(definition, unit)
                 for pin in expression.lists("pin"))


def library_definition(
    document: KiCadSchematicDocument,
    symbol: KiCadSymbol,
) -> ListExpr:
    library = document.root.first_list("lib_symbols")
    if library is None:
        raise KiCadSchematicError("schematic has no embedded symbol library")
    definition = next(
        (
            child
            for child in library.children[1:]
            if isinstance(child, ListExpr)
            and child.tag == "symbol"
            and sexpr_atom(child, 1, "library symbol name").value == symbol.library_id
        ),
        None,
    )
    if definition is None:
        raise KiCadSchematicError(
            f"embedded symbol library has no definition for {symbol.library_id!r}"
        )
    return definition


def _expressions_for_unit(definition: ListExpr, unit: int) -> tuple[ListExpr, ...]:
    result = [definition]
    for child in definition.lists("symbol"):
        name = sexpr_atom(child, 1, "library unit name").value
        match = re.search(r"_(\d+)_(\d+)$", name)
        if match is not None and int(match.group(1)) in {0, unit}:
            result.append(child)
    return tuple(result)


def _xy(expression: ListExpr, context: str) -> tuple[float, float]:
    return (
        sexpr_number(sexpr_atom(expression, 1, context), context),
        sexpr_number(sexpr_atom(expression, 2, context), context),
    )


def _placed_point(symbol: KiCadSymbol, x: float, y: float) -> Vector2:
    dx, dy = _placed_offset(symbol, x, y)
    return Vector2.from_xy_mm(symbol.position[0] + dx, symbol.position[1] + dy)


def _placed_offset(symbol: KiCadSymbol, x: float, y: float) -> tuple[float, float]:
    """Apply library-space mirrors, then rotate from y-up to y-down coordinates."""

    if symbol.mirror_x:
        y = -y
    if symbol.mirror_y:
        x = -x
    angle = radians(symbol.rotation)
    return (
        x * cos(angle) - y * sin(angle),
        -x * sin(angle) - y * cos(angle),
    )
