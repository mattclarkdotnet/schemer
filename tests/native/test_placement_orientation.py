from __future__ import annotations

import pytest

from schemer.analysis.circuits import component_layout_groups
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
)
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.annotations.fields import position_component_fields
from schemer.native.endpoints import component_net_endpoints
from schemer.native.model import NetSymbolTarget
from schemer.native.obstacles import component_body_obstacles
from schemer.native.routing_model import PlacedEndpoint
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("kind,side", [("Power", "top"), ("Ground", "bottom")])
@pytest.mark.parametrize("seed_rotation", [0, 90, 180, 270])
def test_one_terminal_rail_access_keeps_straight_exit_and_opposite_captions(
    kind, side, seed_rotation,
):
    from schemer.kicad.geometry.envelopes import Envelope
    from schemer.native.annotations.corridors import clear_signal_stub_corridors
    from schemer.native.annotations.rails import localize_rail_symbols
    from schemer.native.placement.orientation import orient_single_terminal_rails

    editor = FileSchematic.from_text(f'''(kicad_sch (version 20260306) (generator "test")
      (lib_symbols (symbol "Test:Access" (symbol "Access_1_1"
        (circle (center 0 0) (radius 0.508) (stroke (width 0) (type default)) (fill (type none)))
        (pin passive line (at 0 0 90) (length 0) (name "P") (number "1")))))
      (symbol (lib_id "Test:Access") (at 20 20 {seed_rotation}) (unit 1)
        (uuid "11111111-1111-1111-1111-111111111111")
        (property "Path" "ACCESS" (at 20 20 0) (hide yes))
        (property "Reference" "X93" (at 20 10 0) (effects (font (size 1.27 1.27))))
        (property "Value" "Access" (at 20 12 0) (effects (font (size 1.27 1.27))))))''')
    schematic = {"root_ref": "root", "instances": {
        "root": {"children": {}},
        "root.ACCESS": {"reference_designator": "X93"}}, "nets": {
            "rail": {"name": "RAIL", "kind": kind, "ports": ["root.ACCESS.1"]}}}
    orient_single_terminal_rails(schematic, editor)
    editor.update_items(position_component_fields(editor, list(editor.get_symbols())))
    raw, = editor.document.symbols
    assert placed_pin_sides(editor.document, raw)["1"] == side
    symbol, = editor.get_symbols()
    for field in symbol.fields:
        if field.visible and field.name in {"Reference", "Value"}:
            assert (field.text.position.y > symbol.position.y) == (side == "top")
            assert (field.text.attributes.angle + symbol.transform.orientation) % 180 == 0
    endpoints = component_net_endpoints(schematic, editor)
    ground = kind == "Ground"
    template = NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, ground)
    targets = localize_rail_symbols([template], endpoints)
    glyph = Envelope(-1_270_000, 0 if ground else -2_540_000,
                      1_270_000, 2_540_000 if ground else 0)
    groups, _ = component_layout_groups(schematic)
    obstacles = component_body_obstacles(editor, {symbol.id: groups["root.ACCESS"]})
    assert clear_signal_stub_corridors(targets, endpoints, {ground: glyph}, obstacles) == targets
    assert targets[0].position.x == symbol.position.x


@pytest.mark.parametrize("crossing", [False, True])
def test_shunt_flips_about_signal_pin_only_when_it_removes_crossings(monkeypatch, crossing):
    from schemer.native.placement.orientation import orient_shunts_away_from_signal_routes

    schematic, editor = _support_fixture("shunt")
    schematic["nets"]["gnd"]["kind"] = "Ground"
    symbol = editor.get_symbols()[1]
    symbol.transform.orientation = 90
    editor.update_items(symbol)
    original = editor.document.symbols[1]
    pins = placed_pin_positions(editor.document, original)
    y = (pins["1"].y + pins["2"].y) // 2 if crossing else pins["1"].y - 20_000_000
    endpoints = {
        "SIG": [PlacedEndpoint(pins["1"], "top", "root.BIAS", "block")],
        "GND": [PlacedEndpoint(pins["2"], "bottom", "root.BIAS", "block")],
        "OTHER": [PlacedEndpoint(Vector2(pins["1"].x + dx, y), side, group="block")
                  for dx, side in ((-10_000_000, "right"), (10_000_000, "left"))],
    }
    monkeypatch.setattr(
        "schemer.native.placement.orientation.component_net_endpoints", lambda *args: endpoints
    )
    orient_shunts_away_from_signal_routes(schematic, editor)
    result = editor.document.symbols[1]
    after = placed_pin_positions(editor.document, result)
    assert after["1"] == pins["1"]
    assert after["2"].y == (2 * pins["1"].y - pins["2"].y if crossing else pins["2"].y)
    assert result.rotation == (original.rotation + (180 if crossing else 0)) % 360
