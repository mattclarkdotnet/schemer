from __future__ import annotations

from dataclasses import replace

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.annotations import pin_name_envelopes, pin_number_envelopes
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_segments,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.kicad.geometry.text import caption_bank_axis, oriented_field_text
from schemer.kicad.items import Vector2
from tests.support.schematic import SCHEMATIC


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("mirror_x,mirror_y", [(False, False), (True, False),
                                              (False, True), (True, True)])
def test_body_and_pin_geometry_share_library_to_sheet_transform(rotation, mirror_x, mirror_y):
    # An asymmetric primitive makes both mirror axes observable.
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        '(rectangle (start -1.27 -1.27) (end 1.27 1.27)',
        '(rectangle (start -2.54 1.27) (end 1.27 3.81)'))
    raw = replace(editor.document.symbols[0], rotation=rotation,
                  mirror_x=mirror_x, mirror_y=mirror_y)

    def expected(x, y):
        x, y = (-x if mirror_y else x), (-y if mirror_x else y)
        dx, dy = {0: (x, -y), 90: (-y, -x), 180: (-x, y), 270: (y, x)}[rotation]
        return Vector2.from_xy_mm(raw.position[0] + dx, raw.position[1] + dy)

    assert placed_symbol_body_positions(editor.document, raw) == (
        expected(-2.54, 1.27), expected(1.27, 3.81))
    assert placed_pin_positions(editor.document, raw)["1"] == expected(-2.54, 0)
    outer, inner = placed_pin_segments(editor.document, raw)["1"]
    assert (outer, inner) == (expected(-2.54, 0), expected(-1.27, 0))
    direction = (outer.x - inner.x, outer.y - inner.y)
    side = {(1_270_000, 0): "right", (-1_270_000, 0): "left",
            (0, 1_270_000): "bottom", (0, -1_270_000): "top"}[direction]
    assert placed_pin_sides(editor.document, raw)["1"] == side


@pytest.mark.parametrize("hidden", ["", "hide", "(hide yes)", "(hide no)"])
@pytest.mark.parametrize("kind", ["pin_names", "pin_numbers"])
def test_native_pin_text_visibility_encodings(kind, hidden):
    source = SCHEMATIC.replace('(symbol "Device:R"',
                              f'(symbol "Device:R" ({kind} {hidden})', 1)
    editor = FileSchematic.from_text(source.replace('(name "~"', '(name "IN"'))
    measure = pin_name_envelopes if kind == "pin_names" else pin_number_envelopes
    boxes = measure(editor, editor.document.symbols[0])
    assert len(boxes) == (0 if hidden in {"hide", "(hide yes)"} else 2)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("axis,angle", [("x", 0), ("y", 90)])
def test_caption_bank_axis_uses_drawn_field_direction(rotation, axis, angle):
    symbol = FileSchematic.from_text(SCHEMATIC).get_symbols()[0]
    symbol.transform.orientation = rotation
    fields = symbol.reference_field, symbol.value_field
    for index, field in enumerate(fields):
        field.text = oriented_field_text(field.text, rotation, angle)
        field.text.position = Vector2(index if axis == "x" else 0,
                                      index if axis == "y" else 0)
    assert caption_bank_axis(symbol) == axis
    fields[1].visible = False
    assert caption_bank_axis(symbol) is None
    fields[1].visible = True
    fields[1].text.position = Vector2(10, 10)
    assert caption_bank_axis(symbol) is None
