from __future__ import annotations

import pytest

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.text import field_draw_angle
from schemer.kicad.items import (
    SchematicLine,
    Vector2,
)
from schemer.native.annotations.symbols import place_net_symbols
from schemer.native.model import NetSymbolTarget
from schemer.native.routing_model import PlacedEndpoint
from tests.support.schematic import SCHEMATIC, _support_fixture


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rail_graphic_rotation_does_not_rotate_its_caption(rotation):
    editor = FileSchematic.from_text(SCHEMATIC)
    target = NetSymbolTarget("VCC", "VCC", Vector2.from_xy_mm(20, 20),
                              rotation, True, False)
    symbols = place_net_symbols(editor, [target])[0]
    assert len(symbols) == 1
    assert symbols[0].transform.orientation == rotation
    assert field_draw_angle(symbols[0].value_field.text.attributes.angle, rotation) == 0
    assert symbols[0].value_field.text.attributes.size == Vector2.from_xy_mm(1.27, 1.27)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rail_body_is_protected_from_its_own_wire_except_at_the_anchor(rotation):
    from schemer.core.diagnostics import capture_layout_issues
    from schemer.native.routing import stub_endpoint
    from schemer.native.validation import validate_fixed_geometry

    definition = '''(symbol "power:VCC"
      (symbol "VCC_0_1"
        (polyline
          (pts (xy 0 0) (xy 0 2.54) (xy -0.762 1.27) (xy 0 2.54) (xy 0.762 1.27))
          (stroke (width 0) (type default)) (fill (type none))
        )
      )
      (symbol "VCC_1_1"
        (pin power_in line (at 0 0 90) (length 0)
          (name "VCC" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27))))
        )
      )
    )'''
    editor = FileSchematic.from_text(
        SCHEMATIC.replace('(lib_symbols', '(lib_symbols ' + definition))
    target = NetSymbolTarget("VCC", "VCC", Vector2.from_xy_mm(20, 20), rotation, True, False)
    symbols, _, _, _, endpoints, _, _ = place_net_symbols(editor, [target])
    editor.update_items(symbols)
    power = symbols[0]
    editor.remove_items_by_id([item.id for item in editor.get_items() if item.id != power.id])
    anchor = endpoints["VCC"][0]
    outside = stub_endpoint(anchor).position
    inside = Vector2(2 * anchor.position.x - outside.x, 2 * anchor.position.y - outside.y)
    for end, expected in ((outside, False), (inside, True)):
        editor.remove_items_by_id([line.id for line in editor.get_lines()])
        editor.create_items(SchematicLine(id="", start=anchor.position, end=end))
        with capture_layout_issues(True) as issues:
            validate_fixed_geometry(editor, {item.id: "block" for item in editor.get_items()})
        assert any(i["code"] == "wire-body-overlap" for i in issues) == expected


def test_exhausted_rail_pool_clones_native_graphics_not_unplanned_labels():
    editor = FileSchematic.from_text(SCHEMATIC)
    targets = [NetSymbolTarget(f"V{i}", f"V{i}", Vector2.from_xy_mm(20 * i, 20),
                               0, True, False) for i in range(3)]
    symbols, labels, *_ = place_net_symbols(editor, targets)
    assert not labels
    assert len({s.id for s in symbols}) == len({s.reference for s in symbols}) == 3
    assert {s.library_id for s in symbols} == {"power:VCC"}
    assert [s.value for s in symbols] == ["V0", "V1", "V2"]
    assert all(s.value_field.text.attributes.size == Vector2.from_xy_mm(1.27, 1.27)
               for s in symbols)
    editor.update_items(symbols)
    assert {s.value for s in editor.get_symbols() if s.zener_path is None} == {"V0", "V1", "V2"}
    physical = next(s for s in editor.get_symbols() if s.zener_path is not None)
    with pytest.raises(KiCadSchematicError, match="physical"):
        editor.clone_graphic_symbol(physical.id, "R999")


@pytest.mark.parametrize("rotation,sides", [
    (0, ("left", "right")), (90, ("bottom", "top")),
    (180, ("right", "left")), (270, ("top", "bottom")),
])
def test_boxed_port_route_approaches_tip_along_its_axis(rotation, sides):
    editor = FileSchematic.from_text(SCHEMATIC)
    for alignment, side in zip(("left", "right"), sides, strict=True):
        target = NetSymbolTarget("SIGNAL", "SIGNAL", Vector2.from_xy_mm(20, 20),
                                  rotation, False, False, text_alignment=alignment)
        anchor, = place_net_symbols(editor, [target], global_nets=frozenset({"SIGNAL"}))[3]
        assert anchor.side == side
        assert anchor.escape_length == 635_000


def test_native_local_labels_preserve_assigned_terminal_membership():
    _, editor = _support_fixture(None)
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "part", "block")
    target = NetSymbolTarget("REF", "REF", Vector2.from_xy_mm(12.54, 10),
                              0, False, False, group="block", members=(pin,))
    anchors = place_net_symbols(editor, [target])[3]
    assert anchors[0].members == (pin,)
