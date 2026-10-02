from __future__ import annotations

import pytest

from schemer.kicad.geometry.envelopes import (
    Envelope,
)
from schemer.kicad.items import (
    SchematicLine,
    Vector2,
)
from schemer.native.routing_paths import clear_orthogonal_path, segment_hits_box, wire_contact


@pytest.mark.parametrize("horizontal", [True, False])
def test_parallel_foreign_routes_have_readable_lane_clearance(horizontal):
    def point(along, across):
        return Vector2.from_xy_mm(*((along, across) if horizontal else (across, along)))
    wire = SchematicLine(id="", start=point(0, 0), end=point(10, 0))
    assert wire_contact(point(2, 0.1016), point(8, 0.1016), wire)
    assert wire_contact(point(2, 0.635), point(8, 0.635), wire)
    assert not wire_contact(point(2, 1.27), point(8, 1.27), wire)


def test_multi_bend_fallback_preserves_body_and_crossing_clearance():
    obstacles = [Envelope(*(round(v * 1e6) for v in box)) for box in (
        (-2, 6, 22, 7), (-2, -7, 22, -6), (-2, -7, -1, 7), (21, -7, 22, 7),
        (4, -6, 5, 2), (9, -2, 10, 6), (14, -6, 15, 2))]
    a, b = Vector2.from_xy_mm(0, 0), Vector2.from_xy_mm(20, 0)
    wires = [SchematicLine(id="", start=Vector2.from_xy_mm(12, -6),
                           end=Vector2.from_xy_mm(12, 6))]
    assert clear_orthogonal_path(a, b, obstacles, wires) is None
    path = clear_orthogonal_path(a, b, obstacles, wires, allow_detours=True)
    assert path is not None and path[0] == a and path[-1] == b
    assert len(path) > 4
    for p, q in zip(path, path[1:]):
        assert p.x == q.x or p.y == q.y
        assert not any(segment_hits_box(p, q, box) for box in obstacles)
        assert not any(wire_contact(p, q, wire) for wire in wires)


@pytest.mark.parametrize("offset,blocked", [(0, True), (0.635, True), (1.27, False), (5, False)])
@pytest.mark.parametrize("reverse", [False, True])
def test_crossings_stay_clear_of_either_wires_junctions_and_corners(offset, blocked, reverse):
    a, b = Vector2.from_xy_mm(10, 10 + offset), Vector2.from_xy_mm(30, 10 + offset)
    c, d = Vector2.from_xy_mm(20, 10), Vector2.from_xy_mm(20, 30)
    if reverse:
        a, b, c, d = c, d, a, b
    assert wire_contact(a, b, SchematicLine(id="foreign", start=c, end=d)) == blocked
