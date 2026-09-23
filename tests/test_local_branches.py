from pathlib import Path

import pytest

from schemer.block_generation import generate_functional_ic_blocks
from schemer.heuristic_block import (
    _LOCAL_BRANCH_SPAN,
    _NET_SYMBOL_STUB,
    _net_symbol_attachment,
    _net_symbol_drawing_envelope,
    _NetSymbolAttachment,
    _rail_drawing_envelope,
)
from schemer.layout import LayoutPlan, ModuleLayout
from schemer.signal_terminations import SYMBOL as SIGNAL_TERMINATION_SYMBOL
from schemer.symbol_geometry import (
    Point,
    net_symbol_pin_position,
    pin_positions,
    placed_symbol_bounds,
)
from schemer.toolchain import DEFAULT_PCB_COMPILER, connectivity_digest, evaluate_zener


@pytest.mark.parametrize(
    "side,rotation",
    [("left", 270.0), ("right", 90.0), ("top", 180.0), ("bottom", 0.0)],
)
def test_named_endpoint_orientation_follows_the_wire_direction(side, rotation):
    attachment = _net_symbol_attachment(
        "SIGNAL",
        {"name": "SIGNAL", "kind": "Net", "properties": {}},
        (Point(0, 0),),
        side,
    )

    assert attachment.rotation == rotation


@pytest.mark.e2e
def test_compound_branch_spacing_and_outward_ground_clearance():
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("compiler unavailable")
    source = (Path(__file__).parent
              / "fixtures/package_projection/ordered_perimeter/LocalBranches.zen")
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    plan = generate_functional_ic_blocks(
        schematic, LayoutPlan((ModuleLayout(root, source, {}),)),
    ).plan
    positions = plan.modules[0].positions

    def pin(ref, terminal):
        return pin_positions(schematic["instances"][root + "." + ref],
                             positions["comp:" + ref], terminal)[0]

    supply, feed = pin("IC", "Pin_2"), pin("FEED.R", "2")
    assert feed.y == pytest.approx(supply.y)
    assert feed.x - supply.x == pytest.approx(_LOCAL_BRANCH_SPAN)
    assert _LOCAL_BRANCH_SPAN > 2 * _NET_SYMBOL_STUB
    local_symbols = [net_symbol_pin_position(schematic["nets"]["LOCAL"], value)
                     for key, value in positions.items() if key.startswith("sym:LOCAL#")]
    assert any(point.x == pytest.approx((supply.x + feed.x) / 2) for point in local_symbols)

    source_pin, series_pin = pin("IC", "Pin_5"), pin("SERIES.R", "1")
    assert source_pin.y == pytest.approx(series_pin.y)
    ground_pins = [pin("IC", terminal) for terminal in ("Pin_1", "Pin_7")]
    ground_positions = [value for key, value in positions.items() if key.startswith("sym:GND#")]
    assert len(ground_positions) == 1
    ground_position = ground_positions[0]
    ground = net_symbol_pin_position(schematic["nets"]["GND"], ground_position)
    body = placed_symbol_bounds(schematic["instances"][root + ".IC"], positions["comp:IC"])
    assert ground.x < body.min_x
    assert ground.y > max(point.y for point in ground_pins)
    assert ground_position.rotation == 0
    spare_position = next(
        value for key, value in positions.items() if key.startswith("sym:SPARE#")
    )
    spare_net = schematic["nets"]["SPARE"]
    projected_spare = {
        **spare_net,
        "properties": {
            **spare_net.get("properties", {}),
            "__symbol_value": SIGNAL_TERMINATION_SYMBOL,
        },
    }
    spare_pin = net_symbol_pin_position(projected_spare, spare_position)
    spare_envelope = _net_symbol_drawing_envelope(_NetSymbolAttachment(
        "SPARE",
        spare_net,
        spare_pin,
        rotation=spare_position.rotation,
        outward_side="left",
    ))
    ground_envelope = _rail_drawing_envelope(schematic["nets"]["GND"], ground_position)
    assert (
        ground_envelope.max_x <= spare_envelope.min_x
        or spare_envelope.max_x <= ground_envelope.min_x
        or ground_envelope.max_y <= spare_envelope.min_y
        or spare_envelope.max_y <= ground_envelope.min_y
    )
    assert connectivity_digest(plan.apply_to_schematic(schematic)) == connectivity_digest(schematic)
