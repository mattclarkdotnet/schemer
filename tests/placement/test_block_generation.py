from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from schemer.analysis.quality import component_body_overlap_findings
from schemer.analysis.topology import connectivity_digest
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position
from schemer.integration.toolchain import DEFAULT_PCB_COMPILER, evaluate_zener
from schemer.placement.circuits.net_symbols import net_symbol_attachment
from schemer.placement.circuits.orientation import fan_in_continuation_coordinate
from schemer.placement.pipeline import generate_functional_ic_blocks
from schemer.symbols.geometry import (
    pin_outward_side,
    pin_positions,
)
from schemer.symbols.model import Point
from tests.support.block_generation import (
    FIXTURE,
    PARALLEL_TRANSFORMER,
)


@pytest.mark.parametrize("kind", ["Power", "Ground"])
@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_rail_convention_takes_precedence_over_a_straight_wire(kind, side):
    net = {"kind": kind, "name": "RAIL"}
    attachment = net_symbol_attachment("RAIL", net, (Point(0, 0),), side)
    exception = (kind, side) in {("Ground", "top"), ("Power", "bottom")}
    assert attachment.rotation == (180 if exception else 0)
    if side in {"left", "right"}:
        assert attachment.target.x == (-80 if side == "left" else 80)
        assert attachment.target.y == (40 if kind == "Ground" else -40)
    else:
        assert attachment.target.x == 0
        assert attachment.target.y == (-80 if side == "top" else 80)


@pytest.mark.e2e
def test_local_control_has_aligned_bias_bank_clear_shared_branch_and_valid_approach():
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local Zener compiler is unavailable")
    fixture = PARALLEL_TRANSFORMER.with_name("LocalControl.zen")
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    result = generate_functional_ic_blocks(
        schematic,
        LayoutPlan((ModuleLayout(root, fixture, {}),)),
    )
    assert len(result.module_blocks) == 1
    positions = result.plan.modules[0].positions

    def pin(ref, terminal):
        return pin_positions(
            schematic["instances"][root + "." + ref], positions["comp:" + ref], terminal
        )[0]

    upper, lower = pin("BIAS_UP.R", "1"), pin("BIAS_DOWN.R", "1")
    assert upper.x == pytest.approx(lower.x)
    source, sink = pin("MAIN", "OUT_B"), pin("STAGE", "PRI")
    assert source.y == pytest.approx(sink.y)
    shared = pin("SHARED_BIAS.R", "1")
    assert shared.x == pytest.approx((source.x + sink.x) / 2)
    assert shared.y == pytest.approx(source.y)
    assert abs(shared.x - upper.x) >= 80  # clears the neighbouring branch drawing
    control, bias = pin("STAGE", "SEC"), pin("CONTROL_BIAS.R", "1")
    assert bias.x - control.x == pytest.approx(80)
    assert bias.y - control.y == pytest.approx(40)
    assert (
        pin_outward_side(
            schematic["instances"][root + ".CONTROL_BIAS.R"], positions["comp:CONTROL_BIAS.R"], "1"
        )
        == "top"
    )
    proposed = result.plan.apply_to_schematic(schematic)
    assert not component_body_overlap_findings(proposed)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.parametrize(
    "rows,expected",
    [
        ((10,), 10),
        ((10, 30), 20),
        ((10, 30, 50), 40),
        ((10, 30, 50, 70), 40),
        ((10, 10, 30, 50), 40),
        ((-30, 0, 30), 15),
    ],
)
def test_fan_in_continuation_avoids_four_way_middle_junction(rows, expected):
    assert fan_in_continuation_coordinate(rows) == expected


@pytest.mark.e2e
def test_opposed_connector_channels_align_one_real_lane_not_an_average() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local Zener compiler is unavailable")
    fixture = FIXTURE.with_name("OpposedChannels.zen")
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    result = generate_functional_ic_blocks(
        schematic, LayoutPlan((ModuleLayout(root, fixture, {}),))
    )
    positions = result.plan.modules[0].positions
    ic = schematic["instances"][root + ".IC"]
    connector = schematic["instances"][root + ".SINK"]
    upper = pin_positions(ic, positions["comp:IC"], "AUX_A")[0]
    connector_upper_net = pin_positions(connector, positions["comp:SINK"], "AUX_A")[0]
    assert upper.y == pytest.approx(connector_upper_net.y)
    lower = pin_positions(ic, positions["comp:IC"], "AUX_B")[0]
    connector_lower_net = pin_positions(connector, positions["comp:SINK"], "SIGNAL")[0]
    assert lower.y != pytest.approx(connector_lower_net.y)
    proposed = result.plan.apply_to_schematic(schematic)
    assert not component_body_overlap_findings(proposed)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.e2e
def test_pin_exit_fixture_separates_endpoint_alignment_from_exit_direction() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local Zener compiler is unavailable")
    fixture = FIXTURE.with_name("PinExit.zen")
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    raw = schematic["instances"][root]["symbol_positions"]
    out = schematic["instances"][root + ".OUT.R"]
    shunt = schematic["instances"][root + ".SHUNT.R"]
    out_position = Position(**raw["comp:OUT.R"])
    shunt_position = Position(**raw["comp:SHUNT.R"])
    first = pin_positions(out, out_position, "2")[0]
    second = pin_positions(shunt, shunt_position, "1")[0]
    assert first.x == pytest.approx(second.x)
    assert second.y - first.y == pytest.approx(140.0)
    assert pin_outward_side(out, out_position, "2") == "right"
    assert pin_outward_side(shunt, shunt_position, "1") == "top"


def test_missing_geometry_fails_instead_of_silently_retaining_seed() -> None:
    schematic = {
        "root_ref": "fixture:<root>",
        "instances": {
            "fixture:<root>": {
                "kind": "Module",
                "symbol_positions": {"comp:R1": {"x": 100, "y": 100, "rotation": 0}},
            },
            "fixture:<root>.R1": {
                "kind": "Component",
                "reference_designator": "R1",
                "attributes": {"type": {"String": "resistor"}},
            },
        },
        "nets": {},
    }
    module = ModuleLayout(
        "fixture:<root>",
        Path("Fixture.zen"),
        {"comp:R1": Position(100, 100)},
    )
    plan = LayoutPlan((module,))

    with pytest.raises(ToolchainError, match="no symbol geometry"):
        generate_functional_ic_blocks(schematic, plan)


@pytest.mark.e2e
def test_completed_owner_envelopes_grow_with_captions() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local Zener compiler unavailable")
    schematic = evaluate_zener(FIXTURE, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    empty = LayoutPlan((ModuleLayout(root, FIXTURE, {}),))
    baseline = generate_functional_ic_blocks(schematic, empty)
    expanded = deepcopy(schematic)
    expanded["instances"][root + ".R_AUX_SHUNT.R"]["attributes"]["value"] = {
        "String": "deliberately long fixture caption " * 4,
    }
    generated = generate_functional_ic_blocks(expanded, empty)
    children = generated.module_blocks[0][1].root.children
    old_children = baseline.module_blocks[0][1].root.children
    assert children[0].block.width > old_children[0].block.width
    assert children[1].x > old_children[1].x
    assert children[1].x - children[0].bounds.right == pytest.approx(40.0)
