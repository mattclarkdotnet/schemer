from __future__ import annotations

import pytest

from schemer.kicad.geometry.envelopes import (
    Envelope,
)
from schemer.kicad.items import (
    SchematicLine,
    Vector2,
)
from schemer.native.annotations.rails import (
    localize_rail_symbols,
    merge_touching_rail_symbols,
    rail_approach_side,
    separate_same_face_rail_corridors,
)
from schemer.native.model import NetSymbolTarget
from schemer.native.routing import (
    route_group,
)
from schemer.native.routing_model import PlacedEndpoint
from tests.support.routing import _line_mm


@pytest.mark.parametrize("ground,sides", [(True, ("top", "left", "bottom", "right")),
                                        (False, ("bottom", "right", "top", "left"))])
def test_rail_routes_approach_the_anchor_away_from_the_graphic(ground, sides):
    for rotation, side in zip((0, 90, 180, 270), sides, strict=True):
        target = NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), rotation, True, ground)
        assert rail_approach_side(target) == side


@pytest.mark.parametrize("ground,side", [(True, "top"), (False, "bottom")])
def test_rail_on_reversed_branch_points_outward_without_wrapping(ground, side):
    target = NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, ground)
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), side, "part", "block")
    rail, = localize_rail_symbols([target], {"RAIL": [pin]})
    assert rail.position.x == pin.position.x
    assert (rail.position.y < pin.position.y) == (side == "top")
    assert rail.rotation == 180
    assert rail_approach_side(rail) == ("bottom" if side == "top" else "top")


def test_distant_bypass_return_does_not_wrap_back_to_its_owner_ground():
    target = NetSymbolTarget("RET", "RET", Vector2(0, 0), 0, True, True)
    core = PlacedEndpoint(Vector2.from_xy_mm(40, 40), "bottom", "core", "block",
                           rail_class="supply")
    caps = [PlacedEndpoint(Vector2.from_xy_mm(x, 10), "bottom", f"cap{x}", "block",
                            rail_class="supply", bank=("core", "supply")) for x in (20, 0)]
    rails = localize_rail_symbols([target], {"RET": [core, *caps]})
    assert len(rails) == 2
    assert any(t.members == (core,) for t in rails)
    assert any(len(t.members) == len(caps) and all(p in t.members for p in caps) for t in rails)


@pytest.mark.parametrize("ground", [False, True])
def test_supply_and_signal_ties_keep_separate_same_net_wiresets(ground):
    pins = [PlacedEndpoint(Vector2.from_xy_mm(10, y), "left", "device", "block",
                            rail_class=kind)
            for y, kind in [(10, "supply"), (12.54, "signal-tie"), (15.08, "signal-tie")]]
    bypass = PlacedEndpoint(Vector2.from_xy_mm(5, 10), "right", "cap", "block",
                             rail_class="supply")
    template = NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, ground)
    rails = localize_rail_symbols([template], {"RAIL": [*pins, bypass]})
    assert len(rails) == 2
    supply = next(t for t in rails if pins[0] in t.members)
    ties = next(t for t in rails if pins[1] in t.members)
    assert len(supply.members) == 2 and bypass in supply.members
    assert len(ties.members) == 2 and pins[2] in ties.members
    # Later glyph clearance must not undo the partition.
    from dataclasses import replace
    rails = [replace(t, position=Vector2(0, 0)) for t in rails]
    bounds = {ground: Envelope(-1_270_000, -2_540_000, 1_270_000, 0)}
    assert merge_touching_rail_symbols(rails, bounds) == rails


def test_same_net_glyphs_share_a_termination_when_clearance_brings_them_together():
    pins = [PlacedEndpoint(Vector2.from_xy_mm(x, 10), "right", group="block")
            for x in (10, 30)]
    rails = [NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(40, y),
                              0, True, False, group="block", members=(pin,))
             for y, pin in zip((10, 11.27), pins)]
    bounds = {False: Envelope(-1_270_000, -2_540_000, 1_270_000, 0)}
    merged = merge_touching_rail_symbols(rails, bounds)
    assert len(merged) == 1
    assert merged[0].members == tuple(pins)
    assert merged[0].position.y == 10_000_000
    rails[1] = NetSymbolTarget("OTHER", "OTHER", rails[1].position, 0, True, False,
                               group="block", members=(pins[1],))
    assert merge_touching_rail_symbols(rails, bounds) == rails


def test_horizontal_return_bank_does_not_add_an_unnecessary_vertical_stub():
    target = NetSymbolTarget("GND", "GND", Vector2(0, 0), 0, True, True)
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(20, y), "right") for y in (10, 12.54)]
    ground = localize_rail_symbols([target], {"GND": endpoints})[0]
    assert ground.position == Vector2.from_xy_mm(22.54, 12.54)


@pytest.mark.parametrize("ground", [False, True])
def test_rail_marker_branches_off_a_continuing_vertical_connection(ground):
    target = NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, ground)
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(20, 10), "bottom"),
                 PlacedEndpoint(Vector2.from_xy_mm(20, 30), "top")]
    rail = localize_rail_symbols([target], {"RAIL": endpoints})[0]
    assert rail.position.x == 25_080_000
    assert 10_000_000 < rail.position.y < 30_000_000
    assert rail.rotation == 0


@pytest.mark.parametrize("obstacle_side", [-1, 1])
def test_continuing_rail_branch_uses_the_clear_side(obstacle_side):
    target = NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, False)
    endpoints = {
        "RAIL": [PlacedEndpoint(Vector2.from_xy_mm(20, 10), "bottom"),
                 PlacedEndpoint(Vector2.from_xy_mm(20, 30), "top")],
        "OTHER": [PlacedEndpoint(Vector2.from_xy_mm(20 + obstacle_side * 5.08, 25), "top")],
    }
    rail = localize_rail_symbols([target], endpoints)[0]
    assert rail.position.x == round((20 - obstacle_side * 5.08) * 1_000_000)
    assert rail.position.y == 20_000_000


def test_same_face_rail_cluster_does_not_span_an_intervening_signal_exit():
    template = NetSymbolTarget("VDD", "VDD", Vector2(0, 0), 0, True, False)
    pins = [PlacedEndpoint(Vector2.from_xy_mm(10, y), "right", "device", "block",
                            rail_class="signal-tie") for y in (10, 20)]
    signal = PlacedEndpoint(Vector2.from_xy_mm(10, 15), "right", "device", "block")
    rails = localize_rail_symbols([template], {"VDD": pins, "DATA": [signal]})
    assert len(rails) == 2


def test_authored_support_bank_can_share_rail_beyond_pairwise_cluster_distance():
    template = NetSymbolTarget("VDD", "VDD", Vector2(0, 0), 0, True, False)
    core = PlacedEndpoint(Vector2.from_xy_mm(40, 10), "left", "core", "block",
                            rail_class="supply")
    caps = [PlacedEndpoint(Vector2.from_xy_mm(x, 10), "top", f"cap{x}", "block",
                            rail_class="supply", bank=("core", "input")) for x in (20, 0)]
    rails = localize_rail_symbols([template], {"VDD": [core, *caps]})
    assert len(rails) == 1
    assert len(rails[0].members) == 3


def test_nearby_ground_endpoints_share_one_south_facing_local_symbol() -> None:
    template = NetSymbolTarget(
        "GND",
        "GND",
        Vector2.from_xy_mm(100, 100),
        90,
        True,
        True,
    )
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 10), "bottom", "root.U1"),
        PlacedEndpoint(Vector2.from_xy_mm(20, 12), "bottom", "root.C1"),
    ]

    adjusted = localize_rail_symbols([template], {"GND": endpoints})

    assert len(adjusted) == 1
    assert adjusted[0].position == Vector2.from_xy_mm(15, 14.54)
    assert adjusted[0].rotation == 0


@pytest.mark.parametrize("ground,direction", [(True, 1), (False, -1)])
def test_nearby_rail_pins_do_not_merge_through_a_different_net(ground, direction):
    template = NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, ground)
    endpoints = {
        "RAIL": [
            PlacedEndpoint(Vector2.from_xy_mm(20, 10 * direction), "left", "root.T1"),
            PlacedEndpoint(Vector2.from_xy_mm(10, 30 * direction),
                            "bottom" if ground else "top", "root.R1"),
        ],
        "SIGNAL": [
            PlacedEndpoint(Vector2.from_xy_mm(10, 20 * direction),
                            "top" if ground else "bottom", "root.R1"),
        ],
    }

    targets = localize_rail_symbols([template], endpoints)

    assert len(targets) == 2
    assert {target.owner for target in targets} == {"root.T1", "root.R1"}


def test_distant_supply_endpoints_receive_separate_north_facing_symbols() -> None:
    template = NetSymbolTarget(
        "VDD",
        "VDD",
        Vector2.from_xy_mm(100, 100),
        90,
        True,
        False,
    )
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 30), "top", "root.U1"),
        PlacedEndpoint(Vector2.from_xy_mm(60, 30), "top", "root.U2"),
    ]

    adjusted = localize_rail_symbols([template], {"VDD": endpoints})

    assert [item.position for item in adjusted] == [
        Vector2.from_xy_mm(10, 27.46),
        Vector2.from_xy_mm(60, 27.46),
    ]
    assert all(item.rotation == 0 for item in adjusted)


def test_bypass_supply_termination_stays_directly_above_owner_pin():
    template = NetSymbolTarget("VDD", "VDD", Vector2(0, 0), 0, True, False)
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(10, 30), "top", "root.U1"),
        PlacedEndpoint(Vector2.from_xy_mm(15.08, 24.92), "left", "root.C1"),
    ]

    targets = localize_rail_symbols([template], {"VDD": endpoints})

    assert len(targets) == 1
    assert targets[0].position == Vector2.from_xy_mm(10, 24.92)


def test_separated_channels_do_not_push_supply_symbols_off_their_pin_axes():
    targets = [
        NetSymbolTarget(net, net, Vector2.from_xy_mm(10, y), 0, True, False, group="BANK")
        for net, y in [("VDD_A", 10), ("VDD_B", 50), ("VDD_B", 90)]
    ]
    endpoints = {
        "VDD_A": [PlacedEndpoint(Vector2.from_xy_mm(10, 15), "top", group="BANK")],
        "VDD_B": [PlacedEndpoint(Vector2.from_xy_mm(10, y), "top", group="BANK")
                  for y in (55, 95)],
    }
    assert separate_same_face_rail_corridors(targets, endpoints) == targets


def test_simple_south_facing_ground_uses_only_one_normal_stub():
    template = NetSymbolTarget("GND", "GND", Vector2(0, 0), 0, True, True)
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 30), "bottom", "root.U1")

    targets = localize_rail_symbols([template], {"GND": [pin]})

    assert targets[0].position == Vector2.from_xy_mm(10, 32.54)
    wires = [line for line in route_group([pin, PlacedEndpoint(targets[0].position)])
             if isinstance(line, SchematicLine)]
    assert len(wires) == 1
    assert _line_mm(wires[0]) == ((10, 30), (10, 32.54))


def test_distinct_rails_on_one_component_face_use_separate_corridors() -> None:
    supply = NetSymbolTarget(
        "VDD",
        "VDD",
        Vector2.from_xy_mm(100, 100),
        0,
        True,
        False,
    )
    ground = NetSymbolTarget(
        "GND",
        "GND",
        Vector2.from_xy_mm(100, 100),
        0,
        True,
        True,
    )
    owner = "root.J1"
    group = "INPUT"
    endpoints = {
        "VDD": [
            PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", owner, group),
            PlacedEndpoint(Vector2.from_xy_mm(10, 12.54), "right", owner, group),
        ],
        "GND": [
            PlacedEndpoint(Vector2.from_xy_mm(10, 15.08), "right", owner, group),
            PlacedEndpoint(Vector2.from_xy_mm(10, 17.62), "right", owner, group),
        ],
    }

    localized = localize_rail_symbols([supply, ground], endpoints)
    adjusted = separate_same_face_rail_corridors(localized, endpoints)

    assert adjusted[0].position.x == Vector2.from_xy_mm(12.54, 0).x
    assert adjusted[1].position.x == Vector2.from_xy_mm(15.08, 0).x
    assert adjusted[0].owner == adjusted[1].owner == owner
    assert adjusted[0].group == adjusted[1].group == group
