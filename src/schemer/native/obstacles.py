from __future__ import annotations

from collections import defaultdict

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.annotations import pin_name_envelopes
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    subtract_envelope,
)
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
    symbol_library_pins,
)
from schemer.kicad.geometry.text import (
    caption_bank_axis,
    text_envelope,
)


def component_body_obstacles(
    editor: FileSchematic, groups: dict[str, str],
) -> dict[str, list[Envelope]]:
    """Measure only fixed bodies; captions must yield to electrical geometry."""

    result: dict[str, list[Envelope]] = defaultdict(list)
    for symbol in editor.document.symbols:
        group = groups.get(symbol.uuid)
        if group is None:
            continue
        if symbol.path is not None:
            result[group].extend(pin_name_envelopes(editor, symbol))
        points = placed_symbol_body_positions(editor.document, symbol, fallback_to_pins=False)
        if points:
            box = envelope_from_points(points)
            # Library primitive coordinates describe stroke centrelines, not
            # painted edges. Reserve ink plus wire clearance, including near
            # misses at the ends of open graphics such as plates and contacts.
            # Zero-length rail anchors sit on the graphic boundary: preserve
            # that access edge. Their own rotated glyph is already reserved.
            pad = 250_000 if symbol.path is not None else 0
            box = Envelope(box.min_x - pad, box.min_y - pad,
                            box.max_x + pad, box.max_y + pad)
            pieces = [box]
            positions = placed_pin_positions(editor.document, symbol)
            sides = placed_pin_sides(editor.document, symbol)
            for number, pin in symbol_library_pins(editor.document, symbol).items():
                if pin.length_mm != 0:
                    continue
                # Some native graphics contain their own electrical anchor.
                # Open only that pin's outward ray, not its entire body.
                # Foreign nets still see the electrical pin-contact obstacle.
                p, side = positions[number], sides[number]
                if not (box.min_x <= p.x <= box.max_x and box.min_y <= p.y <= box.max_y):
                    continue
                if p.x in {box.min_x, box.max_x} or p.y in {box.min_y, box.max_y}:
                    # A zero-length boundary anchor can be approached along
                    # its boundary (e.g. the empty upper edge of a triangular
                    # rail envelope). Reserve its interior, not that edge.
                    pieces = [Envelope(
                        b.min_x + (1 if b.min_x == p.x else 0),
                        b.min_y + (1 if b.min_y == p.y else 0),
                        b.max_x - (1 if b.max_x == p.x else 0),
                        b.max_y - (1 if b.max_y == p.y else 0),
                    ) for b in pieces]
                    pieces = [b for b in pieces if b.min_x <= b.max_x and b.min_y <= b.max_y]
                    continue
                cut = Envelope(box.min_x if side == "left" else p.x,
                                box.min_y if side == "top" else p.y,
                                box.max_x if side == "right" else p.x,
                                box.max_y if side == "bottom" else p.y)
                pieces = [piece for current in pieces for piece in subtract_envelope(current, cut)]
            result[group].extend(pieces)
    return result


def component_annotation_obstacles(
    editor: FileSchematic, groups: dict[str, str],
) -> dict[str, list[Envelope]]:
    """Give movable labels the actual local bodies and component captions."""

    result = component_body_obstacles(editor, groups)
    for symbol in editor.get_symbols():
        group = groups.get(symbol.id)
        if group is None:
            continue
        for field in symbol.fields:
            if field.visible and field.name in {"Reference", "Value"}:
                result[group].append(envelope_from_points(
                    text_envelope(field.text, symbol.transform.orientation),
                ))
    return result


def label_layout_obstacles(
    editor: FileSchematic, groups: dict[str, str],
) -> dict[str, list[Envelope]]:
    """Keep row-constrained bank captions local when placing movable flags."""
    result = component_body_obstacles(editor, groups)
    for symbol in editor.get_symbols():
        group = groups.get(symbol.id)
        fields = [f for f in symbol.fields if f.visible and f.name in {"Reference", "Value"}]
        if group is None or caption_bank_axis(symbol) is None:
            continue
        for field in fields:
            box = envelope_from_points(text_envelope(field.text, symbol.transform.orientation))
            result.setdefault(group, []).append(Envelope(
                box.min_x - 250_000, box.min_y - 250_000,
                box.max_x + 250_000, box.max_y + 250_000))
    return result
