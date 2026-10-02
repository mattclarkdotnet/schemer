from __future__ import annotations

import json

import pytest

from schemer.analysis.quality import component_body_overlap_findings
from schemer.analysis.symbol_quality import schematic_quality_findings
from schemer.analysis.topology import connectivity_digest
from schemer.core.layout import LayoutPlan, ModuleLayout, Position
from schemer.integration.toolchain import DEFAULT_PCB_COMPILER, evaluate_zener
from schemer.placement.circuits.policy import PIN_EXIT_STUB
from schemer.placement.pipeline import generate_functional_ic_blocks
from schemer.source.signal_terminations import with_signal_termination_symbols
from schemer.symbols.geometry import (
    pin_outward_side,
    pin_positions,
    placed_symbol_body_bounds,
    placed_symbol_bounds,
)
from schemer.symbols.model import Point
from schemer.symbols.net_symbols import net_symbol_pin_position
from tests.support.block_generation import (
    FIXTURE,
    PARALLEL_TRANSFORMER,
    PARALLEL_TRANSFORMER_EXPECTED,
)


@pytest.mark.e2e
def test_isolation_chain_is_generated_from_topology_not_seed_positions() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    schematic = evaluate_zener(FIXTURE, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:SOURCE": Position(0, 200),
        "comp:R_LEFT.R": Position(300, 200, rotation=90),
        "comp:BARRIER": Position(600, 200),
        "comp:C_LOCAL.C": Position(620, 50),
        "comp:R_AUX_SHUNT.R": Position(120, 350),
        "comp:R_AUX_SHUNT_2.R": Position(150, 450),
        "comp:R_RIGHT.R": Position(900, 200, rotation=90),
        "comp:SINK": Position(1200, 200),
        "sym:VCC#0": Position(700, 0),
        "sym:VDD#0": Position(500, 0),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, FIXTURE, positions),))

    first = generate_functional_ic_blocks(schematic, plan)
    empty_plan = LayoutPlan((ModuleLayout(root_ref, FIXTURE, {}),))
    second = generate_functional_ic_blocks(schematic, empty_plan)

    assert first == second
    assert len(first.module_blocks) == 1
    _, block_plan = first.module_blocks[0]
    assert block_plan.root.block_id == "functional-ic"
    assert block_plan.findings() == ()
    children = block_plan.root.children
    assert len(children) == 3
    for left, right in zip(children, children[1:]):
        assert right.bounds.x - left.bounds.right == pytest.approx(40.0)
    assert {item.symbol_id for item in children[0].block.items} >= {
        "comp:SOURCE",
        "comp:R_LEFT.R",
        "comp:R_AUX_SHUNT.R",
        "comp:R_AUX_SHUNT_2.R",
    }
    assert {item.symbol_id for item in children[1].block.items} >= {
        "comp:BARRIER",
        "comp:C_LOCAL.C",
    }
    result = first.plan.modules[0].positions
    assert result["comp:SOURCE"].x < result["comp:R_LEFT.R"].x < result["comp:BARRIER"].x
    assert result["comp:BARRIER"].x < result["comp:R_RIGHT.R"].x < result["comp:SINK"].x
    assert {symbol_id for symbol_id in result if symbol_id.startswith("comp:")} == {
        symbol_id for symbol_id in positions if symbol_id.startswith("comp:")
    }
    assert {"sym:VCC#0", "sym:VDD#0"} <= set(result)
    assert any(symbol_id.startswith("sym:GND#") for symbol_id in result)
    ground_net = schematic["nets"]["GND"]
    ground_symbol_pins = [
        net_symbol_pin_position(ground_net, position)
        for symbol_id, position in result.items()
        if symbol_id.startswith("sym:GND#")
    ]
    for local_ref in ("C_LOCAL.C",):
        component = schematic["instances"][root_ref + "." + local_ref]
        component_position = result["comp:" + local_ref]
        branch_pin = pin_positions(component, component_position, "1")[0]
        return_pin = pin_positions(component, component_position, "2")[0]
        expected_x = return_pin.x + 40.0 * (1 if return_pin.x > branch_pin.x else -1)
        assert any(
            point.x == pytest.approx(expected_x) and point.y == pytest.approx(return_pin.y + 40)
            for point in ground_symbol_pins
        )
        local_ground_position = min(
            (
                position
                for symbol_id, position in result.items()
                if symbol_id.startswith("sym:GND#")
            ),
            key=lambda position: (
                abs(net_symbol_pin_position(ground_net, position).x - expected_x)
                + abs(net_symbol_pin_position(ground_net, position).y - return_pin.y)
            ),
        )
        assert local_ground_position.rotation == 0.0
    source = schematic["instances"][root_ref + ".SOURCE"]
    barrier = schematic["instances"][root_ref + ".BARRIER"]
    for local_ref, owner_terminal in (
        ("R_AUX_SHUNT.R", "AUX_A"),
        ("R_AUX_SHUNT_2.R", "AUX_B"),
    ):
        assert pin_positions(
            schematic["instances"][root_ref + "." + local_ref],
            result["comp:" + local_ref],
            "1",
        )[0].y == pytest.approx(pin_positions(source, result["comp:SOURCE"], owner_terminal)[0].y)
    shunt_return_points = [
        pin_positions(
            schematic["instances"][root_ref + "." + local_ref],
            result["comp:" + local_ref],
            "2",
        )[0]
        for local_ref in ("R_AUX_SHUNT.R", "R_AUX_SHUNT_2.R")
    ]
    shunt_branch_points = [
        pin_positions(
            schematic["instances"][root_ref + "." + local_ref],
            result["comp:" + local_ref],
            "1",
        )[0]
        for local_ref in ("R_AUX_SHUNT.R", "R_AUX_SHUNT_2.R")
    ]
    shunt_rows = sorted(zip(shunt_branch_points, shunt_return_points), key=lambda row: row[0].y)
    top_branch, top_return = shunt_rows[0]
    bottom_branch, bottom_return = shunt_rows[1]
    assert top_return.x == pytest.approx(bottom_return.x)
    outward = 1 if top_return.x > top_branch.x else -1
    shared_ground = Point(
        (
            max(point.x for point in shunt_return_points)
            if outward > 0
            else min(point.x for point in shunt_return_points)
        )
        + outward * 40.0,
        max(point.y for point in shunt_return_points) + PIN_EXIT_STUB,
    )
    assert (
        sum(
            point.x == pytest.approx(shared_ground.x) and point.y == pytest.approx(shared_ground.y)
            for point in ground_symbol_pins
        )
        == 1
    )
    assert pin_positions(
        schematic["instances"][root_ref + ".C_LOCAL.C"],
        result["comp:C_LOCAL.C"],
        "1",
    )[0].y == pytest.approx(pin_positions(barrier, result["comp:BARRIER"], "VCC")[0].y)
    # The bank's net glyph is a drawing obstacle too, not an invisible point.
    # This fixture uses only generic symbols; guard every local return glyph
    # against every passive body, including the following series branch.
    for key, position in result.items():
        if not key.startswith("sym:GND#"):
            continue
        ground = placed_symbol_body_bounds({"attributes": ground_net["properties"]}, position)
        for ref in ("R_AUX_SHUNT.R", "R_AUX_SHUNT_2.R", "R_LEFT.R", "R_RIGHT.R", "C_LOCAL.C"):
            body = placed_symbol_body_bounds(
                schematic["instances"][root_ref + "." + ref], result["comp:" + ref]
            )
            assert not (
                ground.min_x < body.max_x
                and body.min_x < ground.max_x
                and ground.min_y < body.max_y
                and body.min_y < ground.max_y
            )
    ground_pins = pin_positions(barrier, result["comp:BARRIER"], "GND")
    assert len(ground_pins) == 3
    proposed = first.plan.apply_to_schematic(with_signal_termination_symbols(schematic, first.plan))
    assert not component_body_overlap_findings(proposed)
    # The old fixed body-gap diagnostic remains visible: a complete bypass
    # branch now reserves space for the neighbouring signal termination.
    assert {finding.code for finding in schematic_quality_findings(proposed)} <= {
        "local-passive-owner-gap",
    }
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.e2e
def test_parallel_transformer_fixture_is_built_from_zero_in_local_blocks() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    schematic = evaluate_zener(PARALLEL_TRANSFORMER, DEFAULT_PCB_COMPILER)
    module_ref = schematic["root_ref"]
    empty = LayoutPlan((ModuleLayout(module_ref, PARALLEL_TRANSFORMER, {}),))
    arbitrary = LayoutPlan(
        (
            ModuleLayout(
                module_ref,
                PARALLEL_TRANSFORMER,
                {
                    "comp:END_A": Position(-9000, 7000, 180),
                    "comp:DRIVER": Position(8000, -6000, 90),
                    "comp:TRANSFORMER": Position(-7000, -8000, 270),
                    "comp:OUTPUT": Position(6000, 9000, 180),
                },
            ),
        )
    )

    generated = generate_functional_ic_blocks(schematic, empty)
    shifted = generate_functional_ic_blocks(schematic, arbitrary)

    assert generated == shifted
    assert len(generated.module_blocks) == 1
    _, block_plan = generated.module_blocks[0]
    assert block_plan.root.block_id == "multi-active-interface"
    assert block_plan.findings() == ()
    positions = generated.plan.modules[0].positions
    expected = json.loads(PARALLEL_TRANSFORMER_EXPECTED.read_text())
    for left, right in expected["left_to_right"]:
        assert positions[left].x < positions[right].x
    assert set(block_plan.block_bounds()) == set(expected["block_paths"])
    bank_rows = []
    for row in expected["terminal_rows"]:
        left = schematic["instances"][module_ref + "." + row["left_component"]]
        right = schematic["instances"][module_ref + "." + row["right_component"]]
        left_pin = pin_positions(
            left,
            positions["comp:" + row["left_component"]],
            row["left_terminal"],
        )[0]
        right_pin = pin_positions(
            right,
            positions["comp:" + row["right_component"]],
            row["right_terminal"],
        )[0]
        assert left_pin.y == pytest.approx(right_pin.y)
        assert right_pin.x - left_pin.x == pytest.approx(4 * PIN_EXIT_STUB)
        bank_rows.append(left_pin.y)

    continuation = expected["bank_continuation"]
    assert continuation["row"] == "top"
    continuation_pin = pin_positions(
        schematic["instances"][module_ref + "." + continuation["component"]],
        positions["comp:" + continuation["component"]],
        continuation["terminal"],
    )[0]
    assert continuation_pin.y == pytest.approx(min(bank_rows))

    shunt = schematic["instances"][module_ref + ".R_SHUNT.R"]
    shunt_position = positions["comp:R_SHUNT.R"]
    shunt_common = pin_positions(shunt, shunt_position, "1")[0]
    shunt_return = pin_positions(shunt, shunt_position, "2")[0]
    assert shunt_return.y > shunt_common.y
    assert shunt_common.y > max(bank_rows)
    for row in expected["terminal_rows"]:
        passive = schematic["instances"][module_ref + "." + row["right_component"]]
        position = positions["comp:" + row["right_component"]]
        common_pin = pin_positions(passive, position, "2")[0]
        assert pin_outward_side(passive, position, "2") == "right"
        # The viewer requires a 50-mil outward escape before the shared
        # vertical trunk. Align the shunt with that trunk, not the pin end.
        assert shunt_common.x - common_pin.x == pytest.approx(12.7)
    assert shunt_return.x == pytest.approx(shunt_common.x)

    assert continuation_pin.x - common_pin.x == pytest.approx(8 * PIN_EXIT_STUB)
    assert shunt_common.y - max(bank_rows) == pytest.approx(4 * PIN_EXIT_STUB)
    for left_ref, left_terminal, right_ref, right_terminal in (
        ("TRANSFORMER", "SEC", "C_COUPLING.C", "1"),
        ("C_COUPLING.C", "2", "OUTPUT", "SIGNAL"),
    ):
        left_pin = pin_positions(
            schematic["instances"][module_ref + "." + left_ref],
            positions["comp:" + left_ref],
            left_terminal,
        )[0]
        right_pin = pin_positions(
            schematic["instances"][module_ref + "." + right_ref],
            positions["comp:" + right_ref],
            right_terminal,
        )[0]
        assert right_pin.y == pytest.approx(left_pin.y)
        assert right_pin.x - left_pin.x == pytest.approx(4 * PIN_EXIT_STUB)

    # Each local bypass uses its owner's rail termination, not a duplicate
    # label pair. Geometry is necessary evidence; real wire continuity still
    # needs a viewer check because the router owns the drawn edges.
    for net in {row[3] for row in expected["local_bypasses"]}:
        assert sum(key.startswith(f"sym:{net}#") for key in positions) == sum(
            row[3] == net for row in expected["local_bypasses"]
        )
    for owner_ref, terminal, cap_ref, _ in expected["local_bypasses"]:
        owner = schematic["instances"][module_ref + "." + owner_ref]
        cap = schematic["instances"][module_ref + "." + cap_ref]
        owner_pos = positions["comp:" + owner_ref]
        cap_pos = positions["comp:" + cap_ref]
        owner_pin = pin_positions(owner, owner_pos, terminal)[0]
        supply_pin = pin_positions(cap, cap_pos, "1")[0]
        assert supply_pin.y < owner_pin.y
        assert supply_pin.x > placed_symbol_bounds(owner, owner_pos).max_x
        assert pin_positions(cap, cap_pos, "2")[0].y > supply_pin.y

    proposed = generated.plan.apply_to_schematic(schematic)
    assert component_body_overlap_findings(proposed) == ()
    assert schematic_quality_findings(proposed) == ()
    assert connectivity_digest(proposed) == connectivity_digest(schematic)
