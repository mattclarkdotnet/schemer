from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
)
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.placement.inline import compact_inline_connections
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("role", ["series-termination", ""])
def test_clear_inline_wire_shortening_does_not_require_a_role(role):
    schematic, editor = _support_fixture(role)
    for item in editor.get_symbols():
        item.transform.orientation = 0
        if item.reference == "R2":
            item.position = Vector2.from_xy_mm(80, 30.48)
        editor.update_items(item)
    compact_inline_connections(schematic, editor)
    part = next(item for item in editor.get_symbols() if item.reference == "R2")
    assert part.position == Vector2.from_xy_mm(30.48, 30.48)


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_straight_attachment_compaction_preserves_all_four_pin_axes(angle):
    schematic, editor = _support_fixture(None)
    symbols = editor.get_symbols()
    for symbol in symbols:
        symbol.transform.orientation = angle
    editor.update_items(symbols)
    owner = next(s for s in editor.document.symbols if s.reference == "R1")
    child = next(s for s in editor.document.symbols if s.reference == "R2")
    a = placed_pin_positions(editor.document, owner)["2"]
    b = placed_pin_positions(editor.document, child)["1"]
    side = placed_pin_sides(editor.document, owner)["2"]
    dx, dy = {"left": (-1, 0), "right": (1, 0),
              "top": (0, -1), "bottom": (0, 1)}[side]
    part = next(s for s in symbols if s.reference == "R2")
    part.position = Vector2(part.position.x + a.x + dx * 60_000_000 - b.x,
                            part.position.y + a.y + dy * 60_000_000 - b.y)
    editor.update_items(part)

    compact_inline_connections(schematic, editor)

    child = next(s for s in editor.document.symbols if s.reference == "R2")
    b = placed_pin_positions(editor.document, child)["1"]
    assert b == Vector2(a.x + dx * 5_080_000, a.y + dy * 5_080_000)
    assert child.rotation == angle


def test_adjacent_passive_does_not_block_compaction_with_speculative_caption_space():
    schematic, editor = _support_fixture("series-termination")
    text = editor.get_as_string()
    raw = next(s for s in editor.document.symbols if s.reference == "R2")
    expr = raw.expression
    copy = text[expr.start:expr.end].replace("aaaaaaaa", "bbbbbbbb")
    copy = copy.replace('"BIAS"', '"NEIGHBOUR"').replace('"R2"', '"R3"')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + copy + ")")
    for item in editor.get_symbols():
        item.transform.orientation = 0
        if item.reference in {"R2", "R3"}:
            item.position = Vector2.from_xy_mm(80, 30.48 if item.reference == "R2" else 34.29)
        editor.update_items(item)

    compact_inline_connections(schematic, editor)

    part = next(item for item in editor.get_symbols() if item.reference == "R2")
    assert part.position == Vector2.from_xy_mm(30.48, 30.48)


@pytest.mark.parametrize("obstacle_x,expected_x", [(55, 30.48), (30.48, 80)])
def test_compaction_checks_destination_not_the_path_of_a_drag(obstacle_x, expected_x):
    schematic, editor = _support_fixture(None)
    text = editor.get_as_string()
    raw = next(s for s in editor.document.symbols if s.reference == "R2")
    copy = text[raw.expression.start:raw.expression.end].replace("aaaaaaaa", "bbbbbbbb")
    copy = copy.replace('"BIAS"', '"OBSTACLE"').replace('"R2"', '"R3"')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + copy + ")")
    for item in editor.get_symbols():
        item.transform.orientation = 0
        if item.reference in {"R2", "R3"}:
            item.position = Vector2.from_xy_mm(80 if item.reference == "R2" else obstacle_x, 30.48)
        editor.update_items(item)

    compact_inline_connections(schematic, editor)

    part = next(item for item in editor.get_symbols() if item.reference == "R2")
    assert part.position == Vector2.from_xy_mm(expected_x, 30.48)
