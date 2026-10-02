from __future__ import annotations

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.annotations import pin_number_envelopes
from tests.support.schematic import SCHEMATIC


def test_pin_number_clearance_uses_font_height_and_respects_hidden_numbers():
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.document.symbols[0]
    boxes = pin_number_envelopes(editor, symbol)
    # Rotated numbers paint almost 2 mm to the left of a vertical pin,
    # outside the old fixed 1 mm corridor.
    assert boxes and min(box.min_x for box in boxes) < 20_320_000 - 1_900_000
    hidden = FileSchematic.from_text(SCHEMATIC.replace(
        '(symbol "Device:R"', '(symbol "Device:R" (pin_numbers (hide yes))',
    ))
    assert pin_number_envelopes(hidden, hidden.document.symbols[0]) == []
