from __future__ import annotations

import pytest

from schemer.kicad.items import (
    Vector2,
)
from schemer.native.routing_model import PlacedEndpoint


def test_short_internal_links_route_before_branch_trees_and_named_exits():
    from schemer.native.placement.queries import local_link_priority

    pins = [PlacedEndpoint(Vector2.from_xy_mm(0, y), "left", group="block")
            for y in (0, 5, 15)]
    short = local_link_priority(pins[:2], [])
    assert short < local_link_priority([pins[0], pins[2]], [])
    assert short < local_link_priority(pins, [])
    assert short < local_link_priority(pins[:2], pins[2:])


@pytest.mark.parametrize("value,expected", [("100nF", 1e-7), ("2.2uF", 2.2e-6),
    ("4.7 µF", 4.7e-6), ("1e-8F", 1e-8), ("470pF", 470e-12),
    ("unknown", float("inf")), ("100nF 50V", float("inf"))])
def test_bypass_order_uses_nominal_units_not_display_captions(value, expected):
    from schemer.native.placement.queries import bypass_capacitance
    part = {"attributes": {"capacitance": {"String": value}}}
    assert bypass_capacitance(part) == pytest.approx(expected)
