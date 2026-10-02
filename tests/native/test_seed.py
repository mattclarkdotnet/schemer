from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_sides,
)
from schemer.native.seed import drawing_translation, ordered_unit_position


@pytest.mark.parametrize("auxiliary,bank_size,mixed,expected", [
    ("right", 2, False, 270), ("left", 2, False, 90), (None, 2, False, 90),
    ("both", 2, False, 0), ("right", 1, False, 0), ("right", 2, True, 0),
])
@pytest.mark.parametrize("seed_rotation", [0, 90, 180, 270])
def test_power_unit_orientation_uses_pin_faces_not_identity(
    auxiliary, bank_size, mixed, expected, seed_rotation,
):
    from dataclasses import replace

    from schemer.native.seed import power_unit_rotation

    pins = []
    for face, y, angle in (("top", 5.08, 270), ("bottom", -5.08, 90)):
        for i in range(bank_size):
            kind = "input" if mixed and i == 0 else "power_in"
            pins.append(f'(pin {kind} line (at {i * 2.54} {y} {angle}) (length 2.54) '
                        f'(name "SUPPLY") (number "{face}{i}"))')
    for face, x, angle in (("left", -7.62, 0), ("right", 7.62, 180)):
        if auxiliary in {face, "both"}:
            pins.append(f'(pin power_in line (at {x} 0 {angle}) (length 2.54) '
                        f'(name "AUX") (number "{face}"))')
    # A bank of no-connects must not outweigh the usable supply terminals.
    pins.extend(f'(pin no_connect line (at -7.62 {i} 0) (length 2.54) '
                f'(name "NC") (number "nc{i}"))' for i in range(10))
    editor = FileSchematic.from_text(f'''(kicad_sch (version 20260306) (generator "test")
      (lib_symbols (symbol "Test:Supply" (symbol "Supply_2_1" {" ".join(pins)})))
      (symbol (lib_id "Test:Supply") (at 20 20 {seed_rotation}) (unit 2)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Reference" "X93" (at 20 10 0) (effects (font (size 1.27 1.27))))
        (property "Value" "UnrelatedPart" (at 20 12 0) (effects (font (size 1.27 1.27))))))''')
    raw, = editor.document.symbols
    rotation = power_unit_rotation(editor, raw)
    assert rotation == expected
    if expected:
        sides = placed_pin_sides(editor.document, replace(raw, rotation=rotation))
        assert {sides[f"top{i}"] for i in range(bank_size)} <= {"left", "right"}
        assert {sides[f"bottom{i}"] for i in range(bank_size)} <= {"left", "right"}
        if auxiliary:
            assert sides[auxiliary] == "bottom"


def test_one_physical_components_units_form_one_ordered_stack() -> None:
    positions = [ordered_unit_position(120.0, 80.0, index) for index in range(7)]

    assert {x for x, _ in positions} == {120.0}
    assert [round(y, 1) for _, y in positions] == [
        80.0,
        232.4,
        384.8,
        537.2,
        689.6,
        842.0,
        994.4,
    ]


def test_drawing_origin_is_derived_only_from_visible_targets() -> None:
    translation = drawing_translation([(100, -400, 0), (300, 200, 0)])

    assert translation == (100, 600)
