from __future__ import annotations

import pytest

from schemer.kicad.items import (
    GlobalLabel,
    SchematicLine,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.native.contacts import wire_hits_label
from schemer.native.route_backtracking import route_with_label_backtracking
from schemer.native.routing_model import PlacedEndpoint


@pytest.mark.parametrize("end,hits", [((-0.635, 0), False), ((0.635, 0), True), ((0, 1), True)])
def test_short_wire_exemption_only_applies_outside_a_boxed_labels_tip(end, hits):
    label = GlobalLabel(id="", position=Vector2(0, 0), text=Text(
        "PORT", Vector2(0, 0), TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                                             0, "left", "center")))
    assert wire_hits_label(SchematicLine(id="", start=label.position,
                                         end=Vector2.from_xy_mm(*end)), label) == hits


def test_rotated_flag_backtracks_instead_of_accepting_a_sideways_attachment():
    label = GlobalLabel(id="flag", position=Vector2(0, 0), text=Text(
        "SIGNAL", Vector2(0, 0), TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                                               90, "right", "center")))
    pins = [PlacedEndpoint(Vector2.from_xy_mm(10, 0), "left"),
            PlacedEndpoint(label.position, "top", escape_length=635_000)]
    route, moved = route_with_label_backtracking(pins, label, [], [], "SIGNAL", [])
    assert moved is not None and route
    assert all(not wire_hits_label(w, moved) for w in route if isinstance(w, SchematicLine))
    assert any(pins[0].position in (w.start, w.end) for w in route if isinstance(w, SchematicLine))
