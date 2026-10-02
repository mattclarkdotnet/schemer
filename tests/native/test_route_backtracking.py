from __future__ import annotations

import pytest

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.items import (
    SchematicLine,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.native.route_backtracking import route_with_label_backtracking
from schemer.native.routing import (
    route_group,
)
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.routing_paths import wire_contact


def test_pruned_pin_escape_rechecks_crossing_clearance_and_moves_label():
    from schemer.kicad.items import LocalLabel

    p = Vector2.from_xy_mm(6.825, 0)
    label = LocalLabel(id="label", position=p, text=Text(
        "IN", p, TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 0, "right", "bottom")))
    endpoints = [PlacedEndpoint(Vector2.from_xy_mm(10, y), "left") for y in (0, 20)]
    endpoints.append(PlacedEndpoint(p))
    foreign = SchematicLine(id="foreign", start=Vector2.from_xy_mm(7.46, -10),
                             end=Vector2.from_xy_mm(7.46, 5.08))
    with pytest.raises(KiCadSchematicError, match="completed route lacks crossing clearance"):
        route_group(endpoints, foreign_wires=[foreign], net_name="IN")
    route, moved = route_with_label_backtracking(endpoints, label, [], [foreign], "IN", [])
    assert moved is not None and moved.position != p
    assert all(not wire_contact(w.start, w.end, foreign)
               for w in route if isinstance(w, SchematicLine))
