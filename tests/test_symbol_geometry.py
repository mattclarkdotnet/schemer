from pathlib import Path

import pytest

from schemer.layout import LayoutPlan, ModuleLayout, Position
from schemer.quality import require_no_detached_connected_components
from schemer.symbol_geometry import (
    Point,
    net_symbol_pin_position,
    pin_position,
    pin_positions,
    placed_symbol_body_bounds,
    placed_symbol_bounds,
    position_net_symbol_pin,
    rotated_offset,
    schematic_quality_findings,
    suppress_internal_signal_net_symbols,
    symbol_body_local_bounds,
    symbol_local_bounds,
    validate_series_feed_order,
)
from schemer.toolchain import ToolchainError

_DIODE_INSTANCE = {
    "attributes": {
        "__symbol_value": {
            "String": """
                (symbol "D"
                  (pin passive line (at -3.81 0 0) (name "K") (number "1"))
                  (pin passive line (at 3.81 0 180) (name "A") (number "2")))
            """
        }
    }
}

_BODY_AND_PINS_INSTANCE = {
    "attributes": {
        "__symbol_value": {
            "String": """
                (symbol "generic"
                  (rectangle (start -1 -1) (end 1 1))
                  (pin passive line (at -3 0 0) (length 2) (name "A") (number "1"))
                  (pin passive line (at 3 0 180) (length 2) (name "B") (number "2")))
            """
        }
    }
}

_DUPLICATE_GROUND_INSTANCE = {
    "attributes": {
        "__symbol_value": {
            "String": """
                (symbol "duplicate-ground"
                  (rectangle (start -2 -2) (end 2 2))
                  (pin power_in line (at -1 -3 90) (name "GND") (number "2"))
                  (pin power_in line (at 1 -3 90) (name "GND") (number "8")))
            """
        }
    }
}

_GROUND_NET = {
    "properties": {
        "__symbol_value": {
            "String": """
                (symbol "GND"
                  (polyline (pts (xy -1.27 -1.27) (xy 0 -2.54) (xy 1.27 -1.27)))
                  (pin power_in line (at 0 0 270) (length 0)
                    (name "") (number "1")))
            """
        }
    }
}


def test_quarter_turns_follow_the_viewers_y_down_coordinates() -> None:
    assert rotated_offset((0, 2.54), 90) == (2.54, 0)
    assert rotated_offset((0, 2.54), 270) == (-2.54, 0)


def test_symbol_bounds_and_pin_positions_use_viewer_stored_anchor_units() -> None:
    bounds = symbol_local_bounds(_DIODE_INSTANCE)

    assert bounds.min_x == pytest.approx(-3.91)
    assert bounds.max_x == pytest.approx(3.91)
    pin = pin_position(_DIODE_INSTANCE, Position(300, 150, 180), "A")
    assert pin.x == pytest.approx(301)
    assert pin.y == pytest.approx(151)


def test_duplicate_logical_pin_names_preserve_every_physical_position() -> None:
    points = pin_positions(_DUPLICATE_GROUND_INSTANCE, Position(100, 200), "GND")

    assert len(points) == 2
    assert points[0].y == points[1].y
    assert points[0].x < points[1].x


def test_net_symbol_anchor_is_derived_from_its_electrical_pin() -> None:
    target = Point(142.6, 555.6)

    position = position_net_symbol_pin(_GROUND_NET, target)

    assert position.x != target.x
    pin = net_symbol_pin_position(_GROUND_NET, position)
    assert pin.x == pytest.approx(target.x)
    assert pin.y == pytest.approx(target.y)

    rotated = position_net_symbol_pin(_GROUND_NET, target, rotation=90)
    assert rotated.rotation == 90
    rotated_pin = net_symbol_pin_position(_GROUND_NET, rotated)
    assert rotated_pin.x == pytest.approx(target.x)
    assert rotated_pin.y == pytest.approx(target.y)


def test_internal_signal_labels_are_removed_so_local_components_wire_directly() -> None:
    root_ref = "Fixture.zen:<root>"
    schematic = {
        "root_ref": root_ref,
        "instances": {
            root_ref: {
                "kind": "Module",
                "symbol_positions": {},
                "attributes": {
                    "__signature": {
                        "Json": {
                            "parameters": [
                                {
                                    "is_config": False,
                                    "value": {"Net": {"name": "OUTPUT"}},
                                }
                            ]
                        }
                    }
                },
            },
            root_ref + ".U": {
                "kind": "Component",
                "reference_designator": "U1",
                "children": {},
                "attributes": {},
            },
            root_ref + ".R": {
                "kind": "Component",
                "reference_designator": "R1",
                "children": {},
                "attributes": {},
            },
        },
        "nets": {
            "LOCAL": {
                "name": "LOCAL",
                "kind": "Net",
                "ports": [root_ref + ".U.OUT", root_ref + ".R.P1"],
            },
            "OUTPUT": {
                "name": "OUTPUT",
                "kind": "Net",
                "ports": [root_ref + ".R.P2"],
            },
        },
    }
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                Path("Fixture.zen"),
                {
                    "comp:U": Position(0, 0),
                    "comp:R": Position(500, 0),
                    "sym:LOCAL#0": Position(250, 0),
                    "sym:OUTPUT#0": Position(700, 0),
                },
            ),
        )
    )

    before = schematic_quality_findings(plan.apply_to_schematic(schematic))
    direct = suppress_internal_signal_net_symbols(schematic, plan)

    assert any(finding.code == "internal-signal-net-symbol" for finding in before)
    assert "sym:LOCAL#0" not in direct.modules[0].positions
    assert "sym:OUTPUT#0" in direct.modules[0].positions
    assert not any(
        finding.code == "internal-signal-net-symbol"
        for finding in schematic_quality_findings(direct.apply_to_schematic(schematic))
    )


def test_connected_component_outside_local_attachment_radius_is_rejected() -> None:
    root_ref = "Fixture.zen:<root>"
    first_ref = root_ref + ".U"
    second_ref = root_ref + ".R"
    schematic = {
        "root_ref": root_ref,
        "instances": {
            root_ref: {"kind": "Module", "symbol_positions": {}},
            first_ref: {
                **_BODY_AND_PINS_INSTANCE,
                "kind": "Component",
                "reference_designator": "U1",
            },
            second_ref: {
                **_BODY_AND_PINS_INSTANCE,
                "kind": "Component",
                "reference_designator": "R1",
            },
        },
        "nets": {
            "LOCAL": {
                "name": "LOCAL",
                "kind": "Net",
                "ports": [first_ref + ".B", second_ref + ".A"],
            }
        },
    }
    far = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                Path("Fixture.zen"),
                {"comp:U": Position(0, 0), "comp:R": Position(1000, 0)},
            ),
        )
    ).apply_to_schematic(schematic)
    near = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                Path("Fixture.zen"),
                {"comp:U": Position(0, 0), "comp:R": Position(200, 0)},
            ),
        )
    ).apply_to_schematic(schematic)

    with pytest.raises(ToolchainError, match="no nearby visual peer"):
        require_no_detached_connected_components(far)
    assert any(
        finding.code == "detached-local-signal" for finding in schematic_quality_findings(far)
    )
    require_no_detached_connected_components(near)
    assert not any(
        finding.code == "detached-local-signal" for finding in schematic_quality_findings(near)
    )


def test_body_bounds_exclude_pin_strokes_but_keep_complete_symbol_anchor() -> None:
    complete = symbol_local_bounds(_BODY_AND_PINS_INSTANCE)
    body = symbol_body_local_bounds(_BODY_AND_PINS_INSTANCE)
    complete_placed = placed_symbol_bounds(_BODY_AND_PINS_INSTANCE, Position(0, 0))
    body_placed = placed_symbol_body_bounds(_BODY_AND_PINS_INSTANCE, Position(0, 0))

    assert complete.min_x == pytest.approx(-3.1)
    assert body.min_x == pytest.approx(-1.1)
    assert complete_placed.min_x == pytest.approx(0)
    assert body_placed.min_x == pytest.approx(20)


def test_unrotated_diode_is_rejected_when_its_feed_order_is_inverted() -> None:
    with pytest.raises(ToolchainError, match="inverted terminal feeds"):
        validate_series_feed_order(
            _DIODE_INSTANCE,
            Position(300, 150),
            upstream_pin="A",
            downstream_pin="K",
            upstream_feed=Position(225, 50),
            downstream_feed=Position(375, 50),
            label="D1 input -> output",
        )


def test_rotated_diode_places_upstream_terminal_left_of_downstream_terminal() -> None:
    geometry = validate_series_feed_order(
        _DIODE_INSTANCE,
        Position(300, 150, rotation=180),
        upstream_pin="A",
        downstream_pin="K",
        upstream_feed=Position(225, 50),
        downstream_feed=Position(375, 50),
        label="D1 input -> output",
    )

    assert geometry.upstream_feed.x < geometry.downstream_feed.x
    assert geometry.upstream_pin.x < geometry.downstream_pin.x
    assert pin_position(_DIODE_INSTANCE, Position(300, 150, 180), "A").x == pytest.approx(301)
