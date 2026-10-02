from __future__ import annotations

import pytest

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.annotations import pin_stroke_envelopes
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
)
from schemer.kicad.items import (
    Junction,
    SchematicLine,
    Vector2,
)
from schemer.native.routing import (
    _clear_pin_escape,
    pin_contact_obstacle,
    preview_clear_route,
    route_group,
    shared_vertical_trunk,
)
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box, wire_contact
from tests.support.routing import _line_mm
from tests.support.schematic import SCHEMATIC


def test_foreign_route_cannot_cross_a_pin_stroke_away_from_its_endpoint():
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.document.symbols[0]
    stroke = pin_stroke_envelopes(editor, symbol)["1"]
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(10, 32.4), "right"),
                 PlacedEndpoint(Vector2.from_xy_mm(30, 32.4), "left")]

    wires = [w for w in route_group(endpoints, obstacles=[stroke])
             if isinstance(w, SchematicLine)]

    assert wires
    assert all(not segment_hits_box(w.start, w.end, stroke) for w in wires)
    assert all(any(p.position in (w.start, w.end) for w in wires) for p in endpoints)


def test_speculative_route_failure_is_not_a_completed_drawing_defect():
    from schemer.core.diagnostics import capture_layout_issues

    pins = [PlacedEndpoint(Vector2.from_xy_mm(10, 10), "left", "a",
                            stroke=Envelope(10_000_001, 9_900_000, 12_000_000, 10_100_000)),
            PlacedEndpoint(Vector2.from_xy_mm(11, 10), "left", "b")]
    with capture_layout_issues(True) as issues:
        assert preview_clear_route(pins, []) == []
    assert issues == []


def test_route_preview_can_request_detours_without_diagnostic_net_name():
    from schemer.core.diagnostics import capture_layout_issues

    obstacles = [Envelope(*(round(v * 1e6) for v in box)) for box in (
        (-2, 6, 22, 7), (-2, -7, 22, -6), (-2, -7, -1, 7), (21, -7, 22, 7),
        (4, -6, 5, 2), (9, -2, 10, 6), (14, -6, 15, 2))]
    pins = [PlacedEndpoint(Vector2.from_xy_mm(x, 0)) for x in (0, 20)]
    with capture_layout_issues(True) as issues:
        assert preview_clear_route(pins, obstacles) == []
        route = preview_clear_route(pins, obstacles, allow_detours=True)
    assert route and issues == []
    assert all(not segment_hits_box(w.start, w.end, b) for w in route for b in obstacles)


def test_opposing_pins_meeting_at_a_tap_are_one_node_not_two_free_stubs():
    node = Vector2.from_xy_mm(10, 10)
    endpoints = [PlacedEndpoint(node, "bottom", "a", "GROUP"),
                 PlacedEndpoint(node, "top", "b", "GROUP"),
                 PlacedEndpoint(Vector2.from_xy_mm(20, 10), "left", "ic", "GROUP")]
    obstacles = [Envelope(9_000_000, 5_000_000, 11_000_000, 9_000_000),
                 Envelope(9_000_000, 11_000_000, 11_000_000, 15_000_000)]
    items = route_group(endpoints, obstacles=obstacles)
    assert any(isinstance(item, Junction) and item.position == node for item in items)
    assert all(item.start.y == item.end.y == node.y
               for item in items if isinstance(item, SchematicLine))


@pytest.mark.parametrize("obstacle", [
    Envelope(15_000_000, 8_000_000, 25_000_000, 12_000_000),
    Envelope(19_750_000, 9_750_000, 20_250_000, 10_250_000),
])
def test_router_clears_bodies_and_foreign_pin_endpoints(obstacle):
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right"),
                 PlacedEndpoint(Vector2.from_xy_mm(30, 10), "left")]
    wires = [w for w in route_group(endpoints, obstacles=[obstacle])
             if isinstance(w, SchematicLine)]
    assert wires
    assert all(not segment_hits_box(w.start, w.end, obstacle) for w in wires)
    assert all(any(pin.position in (w.start, w.end) for w in wires) for pin in endpoints)


def test_router_does_not_reverse_through_an_attached_component():
    body = Envelope(10_000_000, 9_000_000, 12_000_000, 11_000_000)
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(9, 10), "left"),
                 PlacedEndpoint(Vector2.from_xy_mm(30, 10), "left")]
    wires = [w for w in route_group(endpoints, obstacles=[body])
             if isinstance(w, SchematicLine)]
    assert all(not segment_hits_box(w.start, w.end, body) for w in wires)


def test_router_does_not_create_a_false_tee_on_a_foreign_wire():
    foreign = SchematicLine(id="foreign", start=Vector2.from_xy_mm(20, 10),
                            end=Vector2.from_xy_mm(20, 20))
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right"),
                 PlacedEndpoint(Vector2.from_xy_mm(30, 10), "left")]
    wires = [w for w in route_group(endpoints, foreign_wires=[foreign])
             if isinstance(w, SchematicLine)]
    assert all(not wire_contact(w.start, w.end, foreign) for w in wires)


def test_router_separates_a_crossing_from_a_nearby_junction():
    foreign = SchematicLine(id="foreign", start=Vector2.from_xy_mm(20, 10),
                            end=Vector2.from_xy_mm(20, 20))
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(10, 10.635), "right"),
                 PlacedEndpoint(Vector2.from_xy_mm(30, 10.635), "left")]
    wires = [w for w in route_group(endpoints, foreign_wires=[foreign])
             if isinstance(w, SchematicLine)]
    assert wires
    assert all(not wire_contact(w.start, w.end, foreign) for w in wires)


@pytest.mark.parametrize("side, end", [("bottom", (10, 12.54)), ("right", (12.54, 10))])
def test_artificial_stub_endpoint_moves_past_a_foreign_wire_crossing(side, end):
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), side)
    x, y = end
    foreign = SchematicLine(id="foreign",
                            start=Vector2.from_xy_mm(x - 5 if side == "bottom" else x,
                                                    y - 5 if side == "right" else y),
                            end=Vector2.from_xy_mm(x + 5 if side == "bottom" else x,
                                                  y + 5 if side == "right" else y))
    escape = _clear_pin_escape(pin, [], [foreign])
    assert escape != Vector2.from_xy_mm(*end)
    assert not wire_contact(pin.position, escape, foreign)
    destination = PlacedEndpoint(Vector2.from_xy_mm(30, 30))
    wires = [w for w in route_group([pin, destination], foreign_wires=[foreign])
             if isinstance(w, SchematicLine)]
    assert all(not wire_contact(w.start, w.end, foreign) for w in wires)


@pytest.mark.parametrize("turns", range(4))
def test_shared_trunk_cannot_reverse_a_branch_pin_exit(turns):
    from dataclasses import replace

    from schemer.native.routing import _pin_inward_obstacle

    def point(x, y):
        for _ in range(turns):
            x, y = -y, x
        return Vector2.from_xy_mm(x, y)

    corners = [point(x, y) for x in (-0.25, 0.25) for y in (-2.25, 0.25)]
    pin = PlacedEndpoint(point(0, 0), ("bottom", "left", "top", "right")[turns],
                          stroke=envelope_from_points(corners))
    endpoints = [pin,
                 PlacedEndpoint(point(-10, -0.73), ("right", "bottom", "left", "top")[turns]),
                 PlacedEndpoint(point(10, -0.73), ("left", "top", "right", "bottom")[turns])]
    wires = [w for w in route_group(endpoints) if isinstance(w, SchematicLine)]
    inward = _pin_inward_obstacle(pin)
    assert not any(segment_hits_box(w.start, w.end, inward) for w in wires)
    outward = point(0, 1)
    attached = [w.end if w.start == pin.position else w.start for w in wires
                if pin.position in (w.start, w.end)]
    assert attached and all(p.x * outward.x + p.y * outward.y > 0 for p in attached)
    # Clustering must see the same projected connectivity regardless of the
    # detours that realizing painted pin geometry will subsequently require.
    sketch = [w for w in route_group(endpoints, topology_only=True)
              if isinstance(w, SchematicLine)]
    bare = [w for w in route_group([replace(p, stroke=None) for p in endpoints])
            if isinstance(w, SchematicLine)]
    assert [_line_mm(w) for w in sketch] == [_line_mm(w) for w in bare]


def test_stub_extension_cannot_pass_through_a_body_to_clear_a_crossing():
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), "bottom")
    foreign = SchematicLine(id="foreign", start=Vector2.from_xy_mm(5, 12.54),
                            end=Vector2.from_xy_mm(15, 12.54))
    body = Envelope(9_000_000, 12_600_000, 11_000_000, 15_000_000)
    escape = _clear_pin_escape(pin, [body], [foreign])
    assert escape == Vector2.from_xy_mm(10, 11.27)
    assert not segment_hits_box(pin.position, escape, body)
    assert not wire_contact(pin.position, escape, foreign)
    with pytest.raises(KiCadSchematicError, match="blocked pin exit"):
        _clear_pin_escape(pin, [Envelope(9_000_000, 10_500_000, 11_000_000, 15_000_000)],
                          [foreign])


@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_unrouted_pin_contact_clearance_is_reserved_without_blocking_distant_crossings(side):
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), side)
    box = pin_contact_obstacle(pin)
    dx, dy = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[side]
    near = Vector2(pin.position.x + dx * 885_000, pin.position.y + dy * 885_000)
    far = Vector2(pin.position.x + dx * 3_810_000, pin.position.y + dy * 3_810_000)
    for point, blocked in ((near, True), (far, False)):
        a = Vector2(point.x - dy * 5_000_000, point.y - dx * 5_000_000)
        b = Vector2(point.x + dy * 5_000_000, point.y + dx * 5_000_000)
        assert segment_hits_box(a, b, box) == blocked
    early = [PlacedEndpoint(Vector2(near.x - dy * 5_000_000, near.y - dx * 5_000_000)),
             PlacedEndpoint(Vector2(near.x + dy * 5_000_000, near.y + dx * 5_000_000))]
    wires = [w for w in route_group(early, obstacles=[box]) if isinstance(w, SchematicLine)]
    assert wires
    assert _clear_pin_escape(pin, [], wires) is not None


@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_unrouted_pin_reserves_parallel_clearance_before_other_nets_are_routed(side):
    pin = PlacedEndpoint(Vector2.from_xy_mm(10, 10), side)
    dx, dy = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[side]
    start = Vector2(pin.position.x + dy * 635_000, pin.position.y + dx * 635_000)
    end = Vector2(start.x + dx * 5_080_000, start.y + dy * 5_080_000)
    assert segment_hits_box(start, end, pin_contact_obstacle(pin))
    stub = Vector2(pin.position.x + dx * 1_270_000, pin.position.y + dy * 1_270_000)
    assert wire_contact(pin.position, stub, SchematicLine(id="", start=start, end=end))


def test_aligned_opposed_pins_route_without_a_dogleg() -> None:
    items = route_group(
        [
            PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right"),
            PlacedEndpoint(Vector2.from_xy_mm(30, 20), "left"),
        ]
    )

    lines = [item for item in items if isinstance(item, SchematicLine)]
    assert not any(isinstance(item, Junction) for item in items)
    assert len(lines) == 1
    assert all(start[1] == end[1] == 20 for start, end in map(_line_mm, lines))


def test_artificial_stub_join_does_not_block_a_clear_foreign_crossing():
    horizontal = [w for w in route_group([
        PlacedEndpoint(Vector2.from_xy_mm(0, 0), "right"),
        PlacedEndpoint(Vector2.from_xy_mm(10, 0), "left"),
    ]) if isinstance(w, SchematicLine)]
    crossing = [w for w in route_group([
        PlacedEndpoint(Vector2.from_xy_mm(2.54, -5), "bottom"),
        PlacedEndpoint(Vector2.from_xy_mm(2.54, 5), "top"),
    ], foreign_wires=horizontal) if isinstance(w, SchematicLine)]
    assert len(horizontal) == len(crossing) == 1
    assert crossing[0].start.x == crossing[0].end.x == 2_540_000


def test_perpendicular_pin_escape_does_not_leave_a_tail_beyond_its_junction():
    items = route_group([
        PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right"),
        PlacedEndpoint(Vector2.from_xy_mm(30, 20), "left"),
        PlacedEndpoint(Vector2.from_xy_mm(20, 18.73), "bottom"),
    ])
    lines = [item for item in items if isinstance(item, SchematicLine)]
    assert max(max(line.start.y, line.end.y) for line in lines) == 20_000_000
    assert any(isinstance(item, Junction) and item.position == Vector2.from_xy_mm(20, 20)
               for item in items)


def test_unaligned_opposed_pins_use_only_the_required_right_angle() -> None:
    items = route_group(
        [
            PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right"),
            PlacedEndpoint(Vector2.from_xy_mm(30, 30), "left"),
        ]
    )

    lines = [item for item in items if isinstance(item, SchematicLine)]
    vertical = [line for line in lines if line.start.x == line.end.x]
    assert len(lines) == 3
    assert len(vertical) == 1


def test_minority_face_branch_joins_inside_the_dominant_trunk_span() -> None:
    items = route_group(
        [
            PlacedEndpoint(Vector2.from_xy_mm(30, 10), "top"),
            PlacedEndpoint(Vector2.from_xy_mm(40, 30), "bottom"),
            PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right"),
        ]
    )

    horizontal_at_trunk = [
        _line_mm(item)
        for item in items
        if isinstance(item, SchematicLine)
        and item.start.y == item.end.y == Vector2.from_xy_mm(0, 20).y
    ]
    assert ((10.0, 20.0), (30.0, 20.0)) in horizontal_at_trunk
    assert ((30.0, 20.0), (40.0, 20.0)) in horizontal_at_trunk


def test_perpendicular_shunt_axis_respects_horizontal_pin_exit_clearance():
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(32.54, y), "right") for y in (10, 20, 30)
    ] + [
        PlacedEndpoint(Vector2.from_xy_mm(48, 10), "left"),
        PlacedEndpoint(Vector2.from_xy_mm(31.27, 40), "top"),
    ]
    assert shared_vertical_trunk(endpoints) == Vector2.from_xy_mm(32.54, 0).x


def test_perpendicular_branch_alignment_also_handles_left_facing_banks():
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(20, y), "left") for y in (10, 20, 30)
    ] + [PlacedEndpoint(Vector2.from_xy_mm(21.27, 40), "top")]
    assert shared_vertical_trunk(endpoints) == Vector2.from_xy_mm(20, 0).x


def test_aligned_perpendicular_shunt_meets_bank_without_a_sideways_step():
    endpoints = [
        PlacedEndpoint(Vector2.from_xy_mm(30, y), "right") for y in (10, 20, 30)
    ] + [
        PlacedEndpoint(Vector2.from_xy_mm(50, 10), "left"),
        PlacedEndpoint(Vector2.from_xy_mm(32.54, 40), "top"),
    ]
    lines = [line for line in route_group(endpoints) if isinstance(line, SchematicLine)]
    assert all(line.start.x == line.end.x == Vector2.from_xy_mm(32.54, 0).x
               for line in lines if max(line.start.y, line.end.y) > 30_000_000)


def test_horizontal_feedback_trunk_does_not_reverse_an_output_escape():
    pins = [PlacedEndpoint(Vector2.from_xy_mm(30, y), "right") for y in (20, 30)]
    pins.extend([PlacedEndpoint(Vector2.from_xy_mm(35, 10), "right"),
                 PlacedEndpoint(Vector2.from_xy_mm(35, 10), "left")])
    assert shared_vertical_trunk(pins) == 35_000_000
