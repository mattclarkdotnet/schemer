from __future__ import annotations

import pytest

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.obstacles import component_body_obstacles
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box
from tests.support.schematic import SCHEMATIC, _support_fixture


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_interior_zero_length_pin_has_only_an_outward_body_access_ray(rotation):
    from schemer.native.routing import _clear_pin_escape, pin_contact_obstacle, stub_endpoint

    definition = '''(symbol "power:VCC"
      (symbol "VCC_0_1" (circle (center 0 0) (radius 0.508)
        (stroke (width 0) (type default)) (fill (type none))))
      (symbol "VCC_1_1" (pin passive line (at 0 0 90) (length 0)
        (name "PAD") (number "1"))))'''
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        '(lib_symbols', '(lib_symbols ' + definition))
    pad = next(s for s in editor.get_symbols() if s.library_id == "power:VCC")
    pad.transform.orientation = rotation
    editor.update_items(pad)
    raw = next(s for s in editor.document.symbols if s.uuid == pad.id)
    pin = PlacedEndpoint(placed_pin_positions(editor.document, raw)["1"],
                           placed_pin_sides(editor.document, raw)["1"])
    bodies = component_body_obstacles(editor, {pad.id: "pad"})["pad"]
    outside = _clear_pin_escape(pin, bodies, [])
    assert outside == stub_endpoint(pin).position
    inside = Vector2(2 * pin.position.x - outside.x, 2 * pin.position.y - outside.y)
    assert any(segment_hits_box(pin.position, inside, b) for b in bodies)
    with pytest.raises(KiCadSchematicError, match="blocked pin"):
        _clear_pin_escape(pin, [*bodies, pin_contact_obstacle(pin)], [])


def test_zero_length_boundary_anchor_leaves_empty_envelope_edge_open():
    definition = '''(symbol "power:VCC"
      (symbol "VCC_0_1" (polyline
        (pts (xy 0 0) (xy -1.27 -1.27) (xy 1.27 -1.27) (xy 0 0))))
      (symbol "VCC_1_1" (pin power_in line (at 0 0 270) (length 0)
        (name "RETURN") (number "1"))))'''
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        '(lib_symbols', '(lib_symbols ' + definition))
    rail = next(s for s in editor.get_symbols() if s.library_id == "power:VCC")
    p = rail.position
    bodies = component_body_obstacles(editor, {rail.id: "rail"})["rail"]
    assert not any(segment_hits_box(Vector2(p.x - 2_540_000, p.y), p, b) for b in bodies)
    assert any(segment_hits_box(p, Vector2(p.x, p.y + 2_540_000), b) for b in bodies)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_dnp_caption_obstacle_covers_native_cross_without_changing_body(rotation):
    from schemer.kicad.geometry.annotations import dnp_caption_obstacle
    from schemer.kicad.geometry.envelopes import envelope_from_points
    _, editor = _support_fixture(None)
    symbol = editor.get_symbols()[0]
    symbol.transform.orientation = rotation
    editor.update_items(symbol)
    raw = next(s for s in editor.document.symbols if s.uuid == symbol.id)
    assert dnp_caption_obstacle(editor, raw) is None
    text = editor.get_as_string().replace(
        f'(uuid "{symbol.id}")', f'(dnp yes) (uuid "{symbol.id}")')
    marked = FileSchematic.from_text(text)
    raw = next(s for s in marked.document.symbols if s.uuid == symbol.id)
    body = envelope_from_points(placed_symbol_body_positions(marked.document, raw))
    box = dnp_caption_obstacle(marked, raw)
    assert box.min_x < body.min_x and box.min_y < body.min_y
    assert box.max_x > body.max_x and box.max_y > body.max_y
    assert component_body_obstacles(editor, {symbol.id: "part"}) == component_body_obstacles(
        marked, {symbol.id: "part"})


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("hidden", [False, True])
def test_pin_only_unit_names_reserve_space_for_rails_and_captions(rotation, hidden):
    from schemer.kicad.geometry.annotations import pin_name_envelopes
    _, original = _support_fixture(None)
    text = original.get_as_string().replace(
        '(symbol "Device:R"', '(symbol "Device:R" (pin_names (offset 0.127)'
        + (' (hide yes)' if hidden else '') + ')', 1).replace('(name "~"', '(name "V+"')
    editor = FileSchematic.from_text(text)
    symbol = editor.get_symbols()[0]
    symbol.transform.orientation = rotation
    editor.update_items(symbol)
    raw = next(s for s in editor.document.symbols if s.uuid == symbol.id)
    names = pin_name_envelopes(editor, raw)
    if hidden:
        assert names == []
    else:
        assert len(names) == 2
        # Bodies need not enclose names: a pin-only library power unit is
        # still a visible drawing, not an empty region for a foreign rail.
        obstacles = component_body_obstacles(editor, {symbol.id: "unit"})["unit"]
        assert all(box in obstacles for box in names)
        assert all(box.width > 0 and box.height > 0 for box in names)
