from __future__ import annotations

import pytest

from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelopes_do_not_overlap,
    rotated_envelope,
)
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.annotations.corridors import clear_signal_stub_corridors
from schemer.native.annotations.label_fitting import (
    fit_signal_labels_on_pin_exits,
)
from schemer.native.model import NetSymbolTarget
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box


@pytest.mark.parametrize("ground, neighbour_y", [(True, 12.54), (False, 7.46)])
def test_rail_glyph_clears_neighbour_wire_even_without_a_label(ground, neighbour_y):
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "IC", "block")
    neighbour = PlacedEndpoint(Vector2.from_xy_mm(10, neighbour_y), "right", "IC", "block")
    rail = NetSymbolTarget("RAIL", "RAIL", Vector2.from_xy_mm(12.54, 10),
                            0, True, ground, group="block", members=(pin,))
    glyph = Envelope(-1_270_000, 0 if ground else -2_540_000,
                      1_270_000, 2_540_000 if ground else 0)
    moved = clear_signal_stub_corridors([rail], {"RAIL": [pin], "OTHER": [neighbour]},
                                         {ground: glyph})[0]
    assert moved.rotation == 0  # Conventional orientation wins when a clear move exists.
    assert not segment_hits_box(neighbour.position, Vector2.from_xy_mm(12.54, neighbour_y),
                                 rotated_envelope(glyph, moved.rotation).translated(moved.position))


def test_rail_clearance_preserves_target_order_and_clears_final_neighbour_routes():
    pins = {name: [PlacedEndpoint(Vector2.from_xy_mm(20, y), "left", "IC", "block")]
            for name, y in (("V1", 10), ("V2", 12.54), ("SIGNAL", 15.08))}
    targets = [NetSymbolTarget(name, name, Vector2.from_xy_mm(17.46, y), 0,
                                name != "SIGNAL", False, group="block",
                                members=tuple(pins[name]))
               for name, y in (("V2", 12.54), ("SIGNAL", 15.08), ("V1", 10))]
    glyph = Envelope(-1_270_000, -2_540_000, 1_270_000, 0)
    moved = clear_signal_stub_corridors(targets, pins, {False: glyph})
    assert [t.net_name for t in moved] == ["V2", "SIGNAL", "V1"]
    assert moved[1] == targets[1]
    # Rotation can clear a neighbour without requiring a staggered X lane.
    box = rotated_envelope(glyph, moved[0].rotation).translated(moved[0].position)
    assert not segment_hits_box(pins["V1"][0].position, moved[2].position,
                                 box)
    assert envelopes_do_not_overlap(box, rotated_envelope(
        glyph, moved[2].rotation).translated(moved[2].position), 0)


def test_supply_trunk_clears_the_whole_signal_label_not_just_its_anchor():
    supply = NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(20, 0), 0, True, False,
                              group="IC")
    signal = NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(20, 10), 0, False, False,
                              text_alignment="left", group="IC")
    endpoints = {"VDD": [PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right", group="IC")]}

    adjusted = clear_signal_stub_corridors([supply, signal], endpoints)

    assert adjusted[0].position.x > Vector2.from_xy_mm(25, 0).x
    assert adjusted[0].position.y == 0
    assert adjusted[1] == signal


def test_rail_clearance_uses_only_pins_served_by_that_local_symbol():
    rails = [NetSymbolTarget("GND", "GND", Vector2.from_xy_mm(20, y),
                             0, True, True, group="IC") for y in (0, 40)]
    signal = NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(20, 20),
                              0, False, False, text_alignment="left", group="IC")
    endpoints = {"GND": [PlacedEndpoint(Vector2.from_xy_mm(17.46, y),
                                         "right", group="IC") for y in (0, 40)]}
    assert clear_signal_stub_corridors([*rails, signal], endpoints) == [*rails, signal]


def test_net_label_yields_on_its_own_wire_before_a_clear_trunk_moves():
    rail = NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(10, 0),
                            0, True, False, group="IC")
    label = NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(10, 10),
                             0, False, False, text_alignment="left", group="IC")
    endpoints = {
        "VDD": [PlacedEndpoint(Vector2.from_xy_mm(0, 20), "right", group="IC")],
        "DATA": [PlacedEndpoint(Vector2.from_xy_mm(0, 10), "right", group="IC")],
    }

    fitted = fit_signal_labels_on_pin_exits([rail, label], endpoints, {})

    assert fitted[0] == rail
    assert 1_270_000 <= fitted[1].position.x < 10_000_000
    assert fitted[1].position.y == label.position.y
    assert fitted[1].rotation == 0  # Translation is enough; retain reading direction.
    assert clear_signal_stub_corridors(fitted, endpoints) == fitted


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("rail_side", [-1, 1])
def test_vertical_pin_label_can_change_side_without_bending_neighbouring_rail(direction, rail_side):
    side = "top" if direction == -1 else "bottom"
    rail = NetSymbolTarget("RAIL", "RAIL", Vector2.from_xy_mm(rail_side * 2.54, direction * 2.54),
                            0, True, direction == 1, group="IC")
    label = NetSymbolTarget("SENSE", "SENSE", Vector2.from_xy_mm(0, direction * 2.54),
                             0, False, False,
                             text_alignment="left" if rail_side == 1 else "right", group="IC")
    endpoints = {
        "RAIL": [PlacedEndpoint(Vector2.from_xy_mm(rail_side * 2.54, 0), side, group="IC")],
        "SENSE": [PlacedEndpoint(Vector2(0, 0), side, group="IC")],
    }
    glyphs = {direction == 1: Envelope(-1_270_000, min(0, direction * 2_540_000),
                                        1_270_000, max(0, direction * 2_540_000))}

    fitted = fit_signal_labels_on_pin_exits([rail, label], endpoints, glyphs)

    assert fitted[0] == rail
    assert fitted[1].position == label.position
    assert fitted[1].rotation == 0
    assert fitted[1].text_alignment == ("right" if rail_side == 1 else "left")
    assert clear_signal_stub_corridors(fitted, endpoints, glyphs) == fitted


@pytest.mark.parametrize("ground,direction", [(True, 1), (False, -1)])
def test_rail_glyph_clears_adjacent_label_even_when_attachment_point_is_clear(ground, direction):
    rail = NetSymbolTarget("RAIL", "RAIL", Vector2.from_xy_mm(10, 10),
                            0, True, ground, group="IC")
    label = NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(10, 10 + 2.54 * direction),
                             0, False, False, text_alignment="right", group="IC")
    endpoints = {"RAIL": [PlacedEndpoint(Vector2.from_xy_mm(12.54, 10),
                                          "left", group="IC")]}
    glyph = Envelope(-1_270_000, min(0, direction * 2_540_000),
                       1_270_000, max(0, direction * 2_540_000))

    adjusted = clear_signal_stub_corridors([rail, label], endpoints, {ground: glyph})

    assert adjusted[0].position.x < 3_000_000 or adjusted[0].rotation in {90, 270}
    assert adjusted[0].position.y == rail.position.y
    assert adjusted[1] == label


def test_shared_supply_glyph_clears_its_own_pin_strokes():
    pins = [PlacedEndpoint(Vector2.from_xy_mm(x, 10), "bottom", "IC", "block",
                             stroke=Envelope(round(x * 1e6) - 250_000, 7_000_000,
                                              round(x * 1e6) + 250_000, 10_250_000))
            for x in (10, 12.54)]
    pins.append(PlacedEndpoint(Vector2.from_xy_mm(10, 15.08), "top", "CAP", "block"))
    glyph = Envelope(-1_270_000, -2_540_000, 1_270_000, 0)
    target = NetSymbolTarget("VEE", "VEE", Vector2.from_xy_mm(10, 12.54),
                              0, True, False, group="block", members=tuple(pins))
    fitted, = clear_signal_stub_corridors([target], {"VEE": pins}, {False: glyph})
    box = rotated_envelope(glyph, fitted.rotation).translated(fitted.position)
    assert all(envelopes_do_not_overlap(box, p.stroke, 250_000)
               for p in pins if p.stroke)


@pytest.mark.parametrize("body", [None, Envelope(20_500_000, 9_000_000,
                                                 22_500_000, 11_000_000)])
def test_logic_tie_glyph_does_not_touch_a_separate_same_net_supply_wire(body):
    supply = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "device", "block",
                              rail_class="supply")
    bypass = PlacedEndpoint(Vector2.from_xy_mm(20, 10), "left", "cap", "block",
                              rail_class="supply")
    tie = PlacedEndpoint(Vector2.from_xy_mm(10, 12.54), "right", "device", "block",
                           rail_class="signal-tie")
    rails = [NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(15, y),
                              0, True, False, group="block", members=members)
             for y, members in [(10, (supply, bypass)), (12.54, (tie,))]]
    glyph = Envelope(-1_270_000, -2_540_000, 1_270_000, 0)
    adjusted = clear_signal_stub_corridors(rails, {"VDD": [supply, bypass, tie]},
                                            {False: glyph}, {"block": [body] if body else []})
    assert not segment_hits_box(supply.position, bypass.position,
                                 rotated_envelope(glyph, adjusted[1].rotation)
                                 .translated(adjusted[1].position))
    if body:
        assert envelopes_do_not_overlap(rotated_envelope(glyph, adjusted[1].rotation)
                                        .translated(adjusted[1].position), body, 635_000)
