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
from schemer.native.endpoints import component_net_endpoints
from schemer.native.model import NetSymbolTarget
from schemer.native.placement.branches import (
    align_perpendicular_branches,
    align_same_face_terminal_exits,
    place_top_pin_bypasses,
)
from schemer.native.routing_model import PlacedEndpoint
from tests.support.schematic import _support_fixture


def test_folded_symbol_leaves_outward_space_for_facing_terminal():
    schematic, editor = _support_fixture(None)
    text = editor.get_as_string()
    node = editor.document.root.first_list("lib_symbols").first_list("symbol")
    definition = text[node.start:node.end]
    folded = definition.replace('"Device:R"', '"Device:Folded"')
    folded = folded.replace('(at -2.54 0 0)', '(at -1.27 -2.54 90)')
    folded = folded.replace('(at 2.54 0 180)', '(at 1.27 -2.54 90)')
    text = text.replace(definition, definition + folded)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Device:Folded")', 1)
    text = text.replace('(at 20.32 30.48 90)', '(at 20.32 30.48 0)')
    text = text.replace('(at 20.32 20.32 0)', '(at 40.64 30.48 270)')
    editor = FileSchematic.from_text(text)

    align_same_face_terminal_exits(schematic, editor)

    source, sink = editor.document.symbols
    a = placed_pin_positions(editor.document, source)["2"]
    b = placed_pin_positions(editor.document, sink)["1"]
    assert placed_pin_sides(editor.document, source)["2"] == "bottom"
    assert placed_pin_sides(editor.document, sink)["1"] == "top"
    assert b.y - a.y == 5_080_000


@pytest.mark.parametrize("direction", [-1, 1])
def test_top_bypasses_fan_out_without_crossing_neighbour_supply_exit(direction):
    from copy import deepcopy

    from schemer.native.annotations.rails import localize_rail_symbols

    schematic, editor = _support_fixture("bypass")
    definition = f'''(symbol "Test:IC" (symbol "IC_1_1"
      (rectangle (start -5.08 0) (end 5.08 -10.16)
        (stroke (width 0.254) (type default)) (fill (type none)))
      (pin power_in line (at {direction * 2.54} 2.54 270)
        (length 2.54) (name "SIG") (number "2"))
      (pin power_in line (at {-direction * 2.54} 2.54 270)
        (length 2.54) (name "OTHER") (number "1"))))'''
    text = editor.get_as_string().replace("(lib_symbols", "(lib_symbols " + definition, 1)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Test:IC")', 1)
    editor = FileSchematic.from_text(text)
    owner = next(s for s in editor.get_symbols() if s.reference == "R1")
    owner.transform.orientation = 0
    editor.update_items(owner)
    raw = editor.document.symbols[1].expression
    text = editor.get_as_string()
    duplicate = text[raw.start:raw.end].replace("aaaaaaaa", "bbbbbbbb")
    duplicate = duplicate.replace('"BIAS"', '"BIAS2"').replace('"R2"', '"R3"')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + duplicate + ")")
    schematic["instances"]["root.OWNER"]["attributes"]["__symbol_value"] = {"String": definition}
    second = deepcopy(schematic["instances"]["root.BIAS"])
    second["reference_designator"] = "R3"
    second["attributes"]["schematic_properties"]["Json"]["pin"] = "OTHER"
    schematic["instances"]["root.BIAS2"] = second
    schematic["nets"]["gnd"]["ports"] = ["root.BIAS.2", "root.BIAS2.2"]
    schematic["nets"]["other"] = {"name": "OTHER", "ports": ["root.OWNER.OTHER", "root.BIAS2.1"]}
    place_top_pin_bypasses(schematic, editor)
    pins = {s.reference: placed_pin_positions(editor.document, s) for s in editor.document.symbols}
    for ref, number, outward in (("R2", "2", direction), ("R3", "1", -direction)):
        pin, attached = pins["R1"][number], pins[ref]["1"]
        assert attached == Vector2(pin.x + outward * 5_080_000, pin.y - 5_080_000)
        assert outward * (pins[ref]["2"].x - attached.x) > 0
    endpoints = component_net_endpoints(schematic, editor)
    templates = [NetSymbolTarget(net, net, Vector2(0, 0), 0, True, False)
                 for net in ("SIG", "OTHER")]
    targets = localize_rail_symbols(templates, endpoints)
    assert len(targets) == 2  # One supply glyph per branch, not duplicated pairs.
    assert all(len(target.members) == 2 for target in targets)


@pytest.mark.parametrize("direction", [-1, 1])
def test_side_pin_bypass_clears_a_lower_same_net_logic_tie(direction):
    schematic, editor = _support_fixture("bypass")
    definition = f'''(symbol "Test:IC" (symbol "IC_1_1"
      (pin power_in line (at {direction * 2.54} 0 {180 if direction == 1 else 0})
        (length 1.27) (name "VDD") (number "2"))
      (pin input line (at {direction * 2.54} -2.54 {180 if direction == 1 else 0})
        (length 1.27) (name "EN") (number "1"))))'''
    text = editor.get_as_string().replace("(lib_symbols", "(lib_symbols " + definition, 1)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Test:IC")', 1)
    editor = FileSchematic.from_text(text)
    owner = next(s for s in editor.get_symbols() if s.reference == "R1")
    owner.transform.orientation = 0
    editor.update_items(owner)
    schematic["nets"]["sig"]["ports"].append("root.OWNER.1")
    schematic["nets"]["gnd"]["ports"].remove("root.OWNER.1")
    place_top_pin_bypasses(schematic, editor)
    owner, cap = [next(s for s in editor.document.symbols if s.reference == ref)
                  for ref in ("R1", "R2")]
    pin = placed_pin_positions(editor.document, owner)["2"]
    cap_pin = placed_pin_positions(editor.document, cap)["1"]
    assert cap_pin == Vector2(pin.x + direction * 5_080_000, pin.y - 5_080_000)


@pytest.mark.parametrize("role,owned,preserved", [
    (None, True, False), ("series", True, True), ("divider", True, True),
    ("shunt", False, False), ("shunt", True, False),
])
def test_perpendicular_alignment_preserves_authored_paths(role, owned, preserved, monkeypatch):
    schematic, editor = _support_fixture(role)
    if not owned:
        props = schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]
        del props["owner"]
        del props["pin"]
    for item in editor.get_symbols():
        if item.reference == "R2":
            item.position = Vector2.from_xy_mm(50, 60)
            item.transform.orientation = 90
        else:
            item.transform.orientation = 0
        editor.update_items(item)
    endpoint_sets = {}
    for raw in editor.document.symbols:
        positions = placed_pin_positions(editor.document, raw)
        sides = placed_pin_sides(editor.document, raw)
        endpoint_sets[raw.reference] = [
            PlacedEndpoint(position, sides[number], "root." + raw.path, "")
            for number, position in positions.items()
        ]
    endpoints = endpoint_sets["R1"] + [endpoint_sets["R2"][0]]
    monkeypatch.setattr("schemer.native.placement.branches.component_net_endpoints",
                        lambda *args: {"NODE": endpoints})

    align_perpendicular_branches(schematic, editor)

    part = next(item for item in editor.get_symbols() if item.reference == "R2")
    assert (part.position == Vector2.from_xy_mm(50, 60)) == preserved


@pytest.mark.parametrize("horizontal_count", [1, 2])
@pytest.mark.parametrize("peer_x", [None, 10, 35])
@pytest.mark.parametrize("remote", [False, True])
def test_standalone_shunts_shorten_their_stems_without_reordering_nodes(
    horizontal_count, peer_x, remote, monkeypatch,
):
    schematic, editor = _support_fixture("shunt")
    props = schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]
    props.pop("owner")
    props.pop("pin")
    if peer_x is not None:
        text = editor.get_as_string()
        raw = next(s for s in editor.document.symbols if s.reference == "R2")
        copy = text[raw.expression.start:raw.expression.end].replace("aaaaaaaa", "bbbbbbbb")
        copy = copy.replace('"BIAS"', '"NEIGHBOUR"').replace('"R2"', '"R3"')
        editor = FileSchematic.from_text(text[:text.rfind(")")] + copy + ")")
        schematic["instances"]["root.NEIGHBOUR"] = {
            "reference_designator": "R3", "attributes": {
                "schematic_properties": {"Json": dict(props)},
            },
        }
    for item in editor.get_symbols():
        item.position = Vector2.from_xy_mm(
            50 if item.reference == "R2" else (peer_x or 100), 60,
        )
        item.transform.orientation = 90
        editor.update_items(item)
    part = next(s for s in editor.document.symbols if s.reference == "R2")
    sides = placed_pin_sides(editor.document, part)
    number = next(n for n, side in sides.items() if side == "top")
    point = placed_pin_positions(editor.document, part)[number]
    pins = [PlacedEndpoint(Vector2.from_xy_mm(20, 30), "left", "root.INLINE", "path")]
    if horizontal_count == 2:
        pins.append(PlacedEndpoint(Vector2.from_xy_mm(10, 30), "right", "root.FEED", "path"))
    nets = {"NODE": [*pins, PlacedEndpoint(point, "top", "root.BIAS", "path")]}
    if remote:
        nets["NODE"].append(PlacedEndpoint(Vector2.from_xy_mm(100, 100), "right",
                                            "root.REMOTE", "other-circuit"))
    if peer_x is not None:
        nets["OTHER"] = [PlacedEndpoint(Vector2.from_xy_mm(peer_x, 55), "top",
                                          "root.NEIGHBOUR", "path")]
    monkeypatch.setattr(
        "schemer.native.placement.branches.component_net_endpoints", lambda *args: nets
    )

    align_perpendicular_branches(schematic, editor)

    part = next(s for s in editor.document.symbols if s.reference == "R2")
    moved = placed_pin_positions(editor.document, part)[number]
    assert moved.y == 32_540_000
    assert moved.x == (50_000_000 if peer_x == 35 else 17_460_000)
