from pathlib import Path

import pytest

from schemer.block_generation import generate_functional_ic_blocks
from schemer.heuristic_block import _LOCAL_BRANCH_SPAN, _NET_SYMBOL_STUB
from schemer.layout import LayoutPlan, ModuleLayout
from schemer.symbol_geometry import net_symbol_pin_position, pin_positions, placed_symbol_bounds
from schemer.toolchain import DEFAULT_PCB_COMPILER, connectivity_digest, evaluate_zener


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
    ground_pin = pin("IC", "Pin_1")
    ground_positions = [value for key, value in positions.items() if key.startswith("sym:GND#")]
    assert len(ground_positions) == 2
    assert ground_positions[0].x == pytest.approx(ground_positions[1].x)
    ground_position = min(ground_positions, key=lambda value: value.y)
    ground = net_symbol_pin_position(schematic["nets"]["GND"], ground_position)
    body = placed_symbol_bounds(schematic["instances"][root + ".SERIES.R"],
                                positions["comp:SERIES.R"])
    assert ground.x < body.min_x
    assert ground.y == pytest.approx(ground_pin.y + 40)
    assert ground_position.rotation == 0
    assert connectivity_digest(plan.apply_to_schematic(schematic)) == connectivity_digest(schematic)
