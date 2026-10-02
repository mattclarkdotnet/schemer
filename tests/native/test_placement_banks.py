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
from schemer.native.endpoints import component_net_endpoints
from schemer.native.model import NetSymbolTarget
from schemer.native.placement.banks import place_owned_shunt_banks
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("independent", [False, True])
def test_authored_unowned_shunt_bank_shares_return_without_acquiring_an_owner(independent):
    from copy import deepcopy

    from schemer.native.annotations.rails import localize_rail_symbols
    from schemer.native.placement.banks import place_shared_rail_banks

    schematic, editor = _support_fixture(None)
    for ref in ("root.OWNER", "root.BIAS"):
        schematic["instances"][ref]["attributes"] = {"schematic_properties": {"Json": {
            "role": "shunt", "group": "supply-bank"}}}
    if independent:
        schematic["instances"]["root"].update(kind="Module", attributes={
            "schematic_properties": {"Json": {"representation": "independent-blocks"}}})
    schematic["nets"] = {
        "gnd": {"name": "RETURN", "kind": "Ground", "ports": ["root.OWNER.1", "root.BIAS.2"]},
        "a": {"name": "SUPPLY_A", "kind": "Power", "ports": ["root.OWNER.2"]},
        "b": {"name": "SUPPLY_B", "kind": "Power", "ports": ["root.BIAS.1"]},
    }
    original = deepcopy(schematic)
    groups, _ = component_layout_groups(schematic)
    assert groups["root.OWNER"] == groups["root.BIAS"]
    place_shared_rail_banks(schematic, editor)
    assert schematic == original
    endpoints = component_net_endpoints(schematic, editor)
    a, b = endpoints["RETURN"]
    assert a.position.y == b.position.y and a.side == b.side == "bottom"
    # Bank membership keeps one return even when caption width exceeds the
    # usual proximity threshold. Other blocks never join it by net name alone.
    targets = localize_rail_symbols([
        NetSymbolTarget("RETURN", "RETURN", Vector2(0, 0), 0, True, True)],
        endpoints, cluster_mm=0.01)
    assert len(targets) == 1 and len(targets[0].members) == 2
    assert a in targets[0].members and b in targets[0].members


@pytest.mark.parametrize("owner_rotation", [0, 180])
@pytest.mark.parametrize("seed_x", [-100, 100])
def test_single_owned_rail_shunt_is_placed_at_owner_face(owner_rotation, seed_x):
    from schemer.kicad.items import place_symbol

    schematic, editor = _support_fixture("shunt")
    schematic["nets"]["gnd"]["kind"] = "Ground"
    owner, shunt = editor.get_symbols()
    place_symbol(owner, Vector2.from_xy_mm(0, 0), owner_rotation)
    place_symbol(shunt, Vector2.from_xy_mm(seed_x, 40), 90)
    editor.update_items([owner, shunt])
    anchor = placed_pin_positions(editor.document, editor.document.symbols[0])["2"]
    face = placed_pin_sides(editor.document, editor.document.symbols[0])["2"]
    original = editor.document.symbols[1]
    original_pin = placed_pin_positions(editor.document, original)["1"]

    place_owned_shunt_banks(schematic, editor)

    part = editor.document.symbols[1]
    pin = placed_pin_positions(editor.document, part)["1"]
    if (original_pin.x < anchor.x) == (face == "left"):
        assert pin == original_pin and part.rotation == original.rotation
        return
    assert pin.y == anchor.y
    assert pin.x - anchor.x == (-7_620_000 if face == "left" else 7_620_000)
    assert placed_pin_sides(editor.document, part)["1"] == (
        "right" if face == "left" else "left")


@pytest.mark.parametrize("owner_rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("signal_bank", [False, True])
def test_owned_parallel_bank_moves_from_remote_seed_to_its_declared_pin(
    owner_rotation, signal_bank,
):
    from copy import deepcopy

    schematic, editor = _support_fixture("shunt" if signal_bank else "bypass")
    text = editor.get_as_string()
    node = editor.document.symbols[1].expression
    extra = text[node.start:node.end].replace("aaaaaaaa", "bbbbbbbb")
    extra = extra.replace('"R2"', '"R3"').replace('"BIAS"', '"BIAS2"')
    extra = extra.replace('(at 20.32 20.32 0)', '(at 200 200 0)')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + extra + ")")
    schematic["instances"]["root.BIAS2"] = deepcopy(schematic["instances"]["root.BIAS"])
    schematic["instances"]["root.BIAS2"]["reference_designator"] = "R3"
    if not signal_bank:
        schematic["nets"]["sig"]["kind"] = "Power"
    schematic["nets"]["sig"]["ports"].append("root.BIAS2.1")
    schematic["nets"]["gnd"]["ports"].append("root.BIAS2.2")
    owner = editor.get_symbols()[0]
    owner.transform.orientation = owner_rotation
    editor.update_items(owner)

    place_owned_shunt_banks(schematic, editor)

    first, second = editor.document.symbols[1:]
    a = placed_pin_positions(editor.document, first)["1"]
    b = placed_pin_positions(editor.document, second)["1"]
    owner = editor.document.symbols[0]
    anchor = placed_pin_positions(editor.document, owner)["2"]
    side = placed_pin_sides(editor.document, owner)["2"]
    if signal_bank and side in {"left", "right"}:
        assert a.x == b.x == anchor.x + (-7_620_000 if side == "left" else 7_620_000)
        assert b.y - a.y == 7_620_000
        assert min(a.y, b.y) > anchor.y
        assert placed_pin_sides(editor.document, first)["1"] == (
            "right" if side == "left" else "left")
        return
    assert a.y == b.y
    assert a.x != b.x
    assert b.x < 100_000_000 and b.y < 100_000_000
    assert placed_pin_sides(editor.document, first)["1"] == "top"
    assert placed_pin_sides(editor.document, second)["1"] == "top"
    bank_points = [p for s in (first, second)
                   for p in placed_pin_positions(editor.document, s).values()]
    if side == "top":
        assert max(p.y for p in bank_points) <= anchor.y - 5_080_000
    elif side == "bottom":
        assert min(p.y for p in bank_points) >= anchor.y + 5_080_000


@pytest.mark.parametrize("obstruction", ["none", "between", "all"])
def test_parallel_signal_stack_reserves_its_whole_corridor(obstruction):
    from schemer.kicad.geometry.envelopes import (
        Envelope,
        envelope_from_points,
        envelopes_do_not_overlap,
    )
    from schemer.native.placement.banks import _stack_parallel_signal_bank

    _, editor = _support_fixture("shunt")
    parts = editor.get_symbols()
    before = editor.get_as_string()
    obstacles = {
        "none": [],
        "between": [Envelope(-13_000_000, 8_000_000, -8_000_000, 9_000_000)],
        "all": [Envelope(-1_000_000_000, -1_000_000_000, 1_000_000_000, 1_000_000_000)],
    }[obstruction]
    placed = _stack_parallel_signal_bank(
        editor, [(s, "1") for s in parts], Vector2(0, 0), "left", obstacles)
    if obstruction == "all":
        assert not placed and editor.get_as_string() == before
        return
    assert placed
    pins = [placed_pin_positions(editor.document, s) for s in editor.document.symbols]
    assert pins[0]["1"].x == pins[1]["1"].x
    assert pins[1]["1"].y - pins[0]["1"].y == 7_620_000
    corridor = envelope_from_points([p for endpoints in pins for p in endpoints.values()])
    assert all(envelopes_do_not_overlap(corridor, box, 2_540_000) for box in obstacles)
    assert pins[0]["1"].x == (-7_620_000 if obstruction == "none" else -17_780_000)
