from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.items import (
    LocalLabel,
    SchematicLine,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.native.validation import clear_final_label_wires
from tests.support.schematic import SCHEMATIC, _support_fixture


def test_final_geometry_check_rejects_label_body_and_flag_overlaps():
    from schemer.core.diagnostics import capture_layout_issues
    from schemer.kicad.items import GlobalLabel
    from schemer.native.validation import validate_fixed_geometry

    _, editor = _support_fixture(None)
    position = editor.get_symbols()[0].position
    editor.create_items([GlobalLabel(id="", position=position, text=Text(
        name, position, TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                                      0, "left", "center"))) for name in ("ONE", "TWO")])
    with capture_layout_issues(True) as issues:
        validate_fixed_geometry(editor, {item.id: "circuit" for item in editor.get_items()})
    assert {i["code"] for i in issues} == {"label-body-overlap", "label-label-overlap"}


@pytest.mark.parametrize("same_group", [True, False])
def test_final_label_clearance_moves_text_before_extending_its_stub(same_group):
    editor = FileSchematic.from_text(SCHEMATIC)
    editor.remove_items(editor.get_items())
    position = Vector2.from_xy_mm(10, 10)
    items = editor.create_items([
        SchematicLine(id="", start=Vector2.from_xy_mm(10, 5), end=position),
        SchematicLine(id="", start=Vector2.from_xy_mm(6, 8.3),
                      end=Vector2.from_xy_mm(9, 8.3)),
        LocalLabel(id="", position=position, text=Text(
            "DATA", position, TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                                              0, "right", "bottom"))),
    ])
    groups = {item.id: "first" for item in items}
    if not same_group:
        groups[items[1].id] = "second"

    clear_final_label_wires(editor, groups)

    wire, foreign = editor.get_lines()
    label = editor.get_labels()[0]
    assert wire == items[0]
    assert label.position == position
    assert label.text.attributes.horizontal_alignment == ("left" if same_group else "right")
    assert foreign == items[1]


def test_final_label_clearance_does_not_move_a_junction():
    editor = FileSchematic.from_text(SCHEMATIC)
    editor.remove_items(editor.get_items())
    position = Vector2.from_xy_mm(10, 10)
    editor.create_items([
        SchematicLine(id="", start=Vector2.from_xy_mm(10, 5), end=position),
        SchematicLine(id="", start=position, end=Vector2.from_xy_mm(10, 15)),
        SchematicLine(id="", start=Vector2.from_xy_mm(6, 8.3),
                      end=Vector2.from_xy_mm(9, 8.3)),
        LocalLabel(id="", position=position, text=Text(
            "DATA", position, TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                                              0, "right", "bottom"))),
    ])
    wires = editor.get_lines()
    clear_final_label_wires(editor)
    assert editor.get_lines() == wires
    assert editor.get_labels()[0].text.attributes.horizontal_alignment == "left"
