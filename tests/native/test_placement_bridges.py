from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.native.placement.bridges import place_pin_bridges
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_bridge_between_opposite_faces_stays_outside_owner_body(rotation):
    from schemer.kicad.geometry.envelopes import envelope_from_points, envelopes_do_not_overlap

    schematic, editor = _support_fixture("pin-bridge")
    schematic["instances"]["root.OWNER"]["attributes"]["__symbol_value"] = {"String":
        '(symbol "P" (pin (name "SIG") (number "2")) (pin (name "RET") (number "1")))'}
    schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"][
        "other_pin"] = "RET"
    owner = editor.get_symbols()[0]
    owner.transform.orientation = rotation
    editor.update_items(owner)
    place_pin_bridges(schematic, editor)
    owner, bridge = editor.document.symbols
    a = envelope_from_points(placed_symbol_body_positions(editor.document, owner))
    b = envelope_from_points(placed_symbol_body_positions(editor.document, bridge))
    assert envelopes_do_not_overlap(a, b, 2_540_000)
    pins = placed_pin_positions(editor.document, bridge)
    if rotation in (90, 270):
        assert pins["1"].x == pins["2"].x
    else:
        assert pins["1"].y == pins["2"].y


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("intervening_pin", [False, True])
def test_bridge_between_same_face_pins_spans_their_rows(rotation, intervening_pin):
    from schemer.kicad.geometry.envelopes import envelope_from_points, envelopes_do_not_overlap

    schematic, editor = _support_fixture("pin-bridge")
    definition = '''(symbol "Test:Owner"
      (symbol "Owner_0_1" (rectangle (start -2.54 -7.62) (end 2.54 7.62)
        (stroke (width 0.254) (type default)) (fill (type none))))
      (symbol "Owner_1_1"
        (pin passive line (at 5.08 5.08 180) (length 2.54)
          (name "RET" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 5.08 -5.08 180) (length 2.54)
          (name "SIG" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27)))))))'''
    if intervening_pin:
        definition = definition.replace('(symbol "Owner_1_1"', '''(symbol "Owner_1_1"
          (pin input line (at 5.08 0 180) (length 2.54)
            (name "OTHER" (effects (font (size 1.27 1.27))))
            (number "3" (effects (font (size 1.27 1.27)))))''')
    editor = FileSchematic.from_text(editor.get_as_string().replace(
        '(lib_symbols', '(lib_symbols ' + definition).replace(
            '(lib_id "Device:R")', '(lib_id "Test:Owner")', 1))
    if intervening_pin:
        editor = FileSchematic.from_text(editor.get_as_string().replace(
            '(pin "2" (uuid "11111111-1111-1111-1111-111111111102"))',
            '(pin "2" (uuid "11111111-1111-1111-1111-111111111102"))'
            '(pin "3" (uuid "11111111-1111-1111-1111-111111111103"))'))
    schematic["instances"]["root.OWNER"]["attributes"]["__symbol_value"] = {"String": definition}
    schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"][
        "other_pin"] = "RET"
    owner = editor.get_symbols()[0]
    owner.transform.orientation = rotation
    editor.update_items(owner)
    before = editor.get_as_string()
    place_pin_bridges(schematic, editor)
    if intervening_pin:
        assert editor.get_as_string() == before
        return
    owner, bridge = editor.document.symbols
    owner_pins = placed_pin_positions(editor.document, owner)
    pins = placed_pin_positions(editor.document, bridge)
    side = placed_pin_sides(editor.document, owner)["1"]
    axis = "y" if side in {"left", "right"} else "x"
    low, high = sorted(getattr(p, axis) for p in owner_pins.values())
    assert all(low <= getattr(p, axis) <= high for p in pins.values())
    assert getattr(pins["1"], axis) != getattr(pins["2"], axis)
    assert envelopes_do_not_overlap(
        envelope_from_points(placed_symbol_body_positions(editor.document, owner)),
        envelope_from_points(placed_symbol_body_positions(editor.document, bridge)), 2_540_000)
