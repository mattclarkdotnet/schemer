from __future__ import annotations

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import Envelope, envelope_from_points
from schemer.kicad.geometry.library import (
    library_definition,
    pins_for_unit,
    placed_pin_positions,
    placed_pin_segments,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.kicad.records import KiCadSymbol
from schemer.kicad.syntax import ListExpr, descendant, sexpr_atom


def pin_stroke_envelopes(
    editor: FileSchematic, symbol: KiCadSymbol,
) -> dict[str, Envelope]:
    """Reserve the painted pin stroke and clearance on every side."""

    margin = 250_000
    return {
        number: Envelope(
            min(a.x, b.x) - margin, min(a.y, b.y) - margin,
            max(a.x, b.x) + margin, max(a.y, b.y) + margin,
        )
        for number, (a, b) in placed_pin_segments(editor.document, symbol).items()
    }


def _hidden(expression: ListExpr | None) -> bool:
    if expression is None:
        return False
    hide = expression.first_list("hide")
    return (any(getattr(child, "value", None) == "hide" for child in expression.children)
            or hide is not None and sexpr_atom(hide, 1, "visibility").value == "yes")


def _pin_font_size(expression: ListExpr, context: str) -> int:
    size = descendant(expression, ("effects", "font", "size"))
    return (
        1_270_000 if size is None else round(float(sexpr_atom(size, 1, context).value) * 1_000_000)
    )


def pin_number_envelopes(
    editor: FileSchematic, symbol: KiCadSymbol,
) -> list[Envelope]:
    """Reserve rendered number space using each embedded pin's font and length."""

    definition = library_definition(editor.document, symbol)
    visibility = definition.first_list("pin_numbers")
    if _hidden(visibility):
        return []
    positions = placed_pin_positions(editor.document, symbol)
    sides = placed_pin_sides(editor.document, symbol)
    result = []
    for pin in pins_for_unit(definition, symbol.unit):
        number = pin.first_list("number")
        if number is None:
            continue
        text = sexpr_atom(number, 1, "pin number").value
        if text not in positions:
            continue
        font = _pin_font_size(number, "pin number font")
        if font <= 0:
            continue
        anchor = positions[text]
        side = sides[text]
        length = pin.first_list("length")
        span = 0 if length is None else round(
            float(sexpr_atom(length, 1, "pin length").value) * 1_000_000
        )
        width = round(len(text) * 0.9 * font + 254_000)
        # KiCad numbers sit beside the stroke, not centred on it. The stroke
        # font's painted height and baseline offset exceed the nominal size.
        depth = round(1.25 * font + 500_000)
        if side in {"top", "bottom"}:
            center = anchor.y + (span // 2 if side == "top" else -span // 2)
            result.append(Envelope(
                anchor.x - depth, center - width // 2,
                anchor.x - 200_000, center + width // 2,
            ))
        else:
            center = anchor.x + (span // 2 if side == "left" else -span // 2)
            result.append(Envelope(
                center - width // 2, anchor.y - depth,
                center + width // 2, anchor.y - 200_000,
            ))
    return result


def dnp_caption_obstacle(editor: FileSchematic, symbol: KiCadSymbol) -> Envelope | None:
    """Reserve KiCad's plotted DNP cross for captions, not electrical routing."""
    dnp = symbol.expression.first_list("dnp")
    if dnp is None or sexpr_atom(dnp, 1, "DNP state").value != "yes":
        return None
    points = placed_symbol_body_positions(editor.document, symbol)
    if not points:
        return None
    body = envelope_from_points(points)
    pins = placed_pin_positions(editor.document, symbol)
    bounds = envelope_from_points((*points, *pins.values()))
    # SCH_SYMBOL::PlotDNP derives the cross from body/pin extents. Its X
    # margin is updated before the Y calculation. Include the thick mark's
    # stroke and native body-stroke uncertainty conservatively.
    x = max(body.min_x - bounds.min_x, bounds.max_x - body.max_x)
    y = max(body.min_y - bounds.min_y, bounds.max_y - body.max_y)
    x = max(0.6 * x, 0.3 * y)
    y = max(0.6 * y, 0.3 * x)
    return Envelope(body.min_x - round(x) - 500_000, body.min_y - round(y) - 500_000,
                     body.max_x + round(x) + 500_000, body.max_y + round(y) + 500_000)


def pin_name_envelopes(editor: FileSchematic, symbol: KiCadSymbol) -> list[Envelope]:
    """Reserve native pin names, including symbols made only from power pins."""
    definition = library_definition(editor.document, symbol)
    names = definition.first_list("pin_names")
    if _hidden(names):
        return []
    offset = None if names is None else names.first_list("offset")
    inset = 1_016_000 if offset is None else round(
        float(sexpr_atom(offset, 1, "pin name offset").value) * 1_000_000)
    segments = placed_pin_segments(editor.document, symbol)
    sides = placed_pin_sides(editor.document, symbol)
    result = []
    for pin in pins_for_unit(definition, symbol.unit):
        number, name = pin.first_list("number"), pin.first_list("name")
        if number is None or name is None:
            continue
        key, text = sexpr_atom(number, 1, "pin number").value, sexpr_atom(name, 1, "pin name").value
        if key not in segments or text in {"", "~"} or _hidden(pin):
            continue
        font = _pin_font_size(name, "pin name font")
        if font <= 0:
            continue
        outer, inner = segments[key]
        side, width, half = sides[key], len(text) * font, round(0.65 * font)
        if inset:
            # SCH_PIN::PlotPinTexts places names beyond the body's pin root,
            # along its inward axis, with native font and name offset.
            if side == "left":
                box = Envelope(inner.x + inset, inner.y - half,
                                inner.x + inset + width, inner.y + half)
            elif side == "right":
                box = Envelope(inner.x - inset - width, inner.y - half,
                                inner.x - inset, inner.y + half)
            elif side == "top":
                box = Envelope(inner.x - half, inner.y + inset,
                                inner.x + half, inner.y + inset + width)
            else:
                box = Envelope(inner.x - half, inner.y - inset - width,
                                inner.x + half, inner.y - inset)
        else:
            # With no inside offset, names sit beside the pin stroke.
            depth = round(1.25 * font + 500_000)
            if side in {"left", "right"}:
                middle = (outer.x + inner.x) // 2
                box = Envelope(middle - width // 2, inner.y - depth,
                                middle + width // 2, inner.y - 200_000)
            else:
                middle = (outer.y + inner.y) // 2
                box = Envelope(inner.x - depth, middle - width // 2,
                                inner.x - 200_000, middle + width // 2)
        result.append(box)
    return result
