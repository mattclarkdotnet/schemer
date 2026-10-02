from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.text import (
    label_envelope,
    text_envelope,
)
from schemer.kicad.items import (
    LocalLabel,
    Text,
    TextAttributes,
    Vector2,
)
from tests.support.schematic import SCHEMATIC


def test_wire_label_vertical_alignment_round_trips_and_bounds_stay_above_wire():
    editor = FileSchematic.from_text(SCHEMATIC)
    label = LocalLabel(id="", position=Vector2.from_xy_mm(15, 20), text=Text(
        "DATA", Vector2.from_xy_mm(15, 20),
        TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 0, "left", "bottom"),
    ))
    created = editor.create_items(label)[0]
    saved = next(item for item in editor.get_labels() if item.id == created.id)
    assert saved.text.attributes.vertical_alignment == "bottom"
    low, high = text_envelope(saved.text)
    assert low.y == 18_730_000
    assert high.y == saved.position.y
    saved.text.attributes.horizontal_alignment = "right"
    editor.update_items(saved)
    saved = next(item for item in editor.get_labels() if item.id == created.id)
    assert saved.text.attributes.horizontal_alignment == "right"
    assert saved.text.attributes.vertical_alignment == "bottom"
    saved.text.attributes.vertical_alignment = "top"
    editor.update_items(saved)
    saved = next(item for item in editor.get_labels() if item.id == created.id)
    assert text_envelope(saved.text)[0].y == saved.position.y


def test_rotated_label_bounds_rotate_justification_about_its_attachment():
    text = Text("DATA", Vector2.from_xy_mm(10, 10),
                TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 90, "right"))
    low, high = text_envelope(text)
    assert low == Vector2.from_xy_mm(9.365, 10)
    assert high == Vector2.from_xy_mm(10.635, 15.08)


@pytest.mark.parametrize("angle", [0, 90])
def test_native_label_bounds_include_wire_inset_and_stroke(angle):
    text = Text("DATA", Vector2(0, 0),
                TextAttributes(Vector2.from_xy_mm(1.27, 1.27), angle, "left", "bottom"))
    low, high = label_envelope(text)
    # Native stroke plots extend about 1.86 mm from the electrical anchor,
    # rather than only the nominal 1.27 mm text height.
    assert (low.y if angle == 0 else low.x) < -1_860_000
    assert (high.y if angle == 0 else high.x) < 0


@pytest.mark.parametrize("angle", [0, 90])
@pytest.mark.parametrize("alignment", ["left", "right"])
def test_boxed_label_envelope_reserves_flag_and_tip(angle, alignment):
    text = Text("DATA", Vector2(0, 0),
                TextAttributes(Vector2.from_xy_mm(1.27, 1.27), angle, alignment, "center"))
    low, high = label_envelope(text)
    assert low.x <= 0 <= high.x and low.y <= 0 <= high.y
    assert (high.y-low.y if angle == 0 else high.x-low.x) > 2_540_000
