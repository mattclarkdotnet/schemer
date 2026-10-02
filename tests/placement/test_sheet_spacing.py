from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from schemer.analysis.measurements import top_level_root_symbol_groups
from schemer.analysis.quality import (
    top_level_block_clearance_findings,
    top_level_block_overlap_findings,
)
from schemer.analysis.sheet_metrics import primary_anchor_metrics
from schemer.analysis.topology import connectivity_digest
from schemer.core.layout import LayoutPlan, ModuleLayout, Position
from schemer.integration.toolchain import DEFAULT_PCB_COMPILER, evaluate_zener
from schemer.placement.anchor import center_primary_ic
from schemer.placement.spacing.channels import (
    spread_repeated_active_channels,
)
from schemer.placement.spacing.groups import (
    compact_excessive_signal_block_gaps,
    separate_tight_signal_block_gaps,
)
from schemer.placement.spacing.rails import spread_parallel_rail_labels
from schemer.symbols.geometry import placed_symbol_bounds
from tests.paths import TESTS

FIXTURES = TESTS / "fixtures"


ABX_WORKSPACE = Path(
    os.environ.get(
        "SCHEMER_ABX_WORKSPACE",
        str(TESTS / "fixtures/sample-board"),
    )
)


DIGITAL_ABX = ABX_WORKSPACE / "boards" / "sample-board" / "SampleBoard.zen"


@pytest.mark.e2e
def test_largest_generic_ic_is_moved_to_sheet_centre() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "anchor_orientation" / "AnchorOrientation.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:R1.R": Position(0, 0, rotation=270),
        "comp:U1.U": Position(300, 0),
        "comp:R2.R": Position(600, 0, rotation=270),
    }
    positions["sym:VDD#0"] = Position(
        positions["comp:U1.U"].x,
        positions["comp:U1.U"].y - 100,
    )
    plan = LayoutPlan((ModuleLayout(root_ref, entrypoint, positions),))
    before = primary_anchor_metrics(plan.apply_to_schematic(schematic))

    centered = center_primary_ic(schematic, plan)
    after = primary_anchor_metrics(centered.apply_to_schematic(schematic))

    assert after.primary_ref == before.primary_ref
    assert after.primary_ref.endswith(".U1.U")
    assert after.horizontal_center_offset_ratio < before.horizontal_center_offset_ratio
    assert after.horizontal_center_offset_ratio <= 0.01
    assert after.vertical_center_offset_ratio <= 0.01
    centered_positions = centered.modules[0].positions
    assert centered_positions["sym:VDD#0"].x - centered_positions["comp:U1.U"].x == pytest.approx(
        positions["sym:VDD#0"].x - positions["comp:U1.U"].x
    )
    assert centered_positions["sym:VDD#0"].y - centered_positions["comp:U1.U"].y == pytest.approx(
        positions["sym:VDD#0"].y - positions["comp:U1.U"].y
    )


@pytest.mark.e2e
def test_overlapping_top_level_functional_blocks_are_rejected() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "anchor_orientation" / "AnchorOrientation.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    separated_positions = {
        "comp:R1.R": Position(0, 0, rotation=270),
        "comp:U1.U": Position(300, 0),
        "comp:R2.R": Position(600, 0, rotation=270),
    }
    separated = LayoutPlan(
        (ModuleLayout(root_ref, entrypoint, separated_positions),)
    ).apply_to_schematic(schematic)
    coincident_positions = {symbol_id: Position(100, 100) for symbol_id in separated_positions}
    coincident = LayoutPlan(
        (ModuleLayout(root_ref, entrypoint, coincident_positions),)
    ).apply_to_schematic(schematic)

    assert not top_level_block_overlap_findings(separated)
    findings = top_level_block_overlap_findings(coincident)
    assert findings
    assert {finding.code for finding in findings} == {"top-level-block-overlap"}
    assert any(set(finding.symbol_ids) == {"comp:R1", "comp:U1"} for finding in findings)


@pytest.mark.e2e
def test_excessive_connected_block_gap_is_compacted_toward_primary() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "anchor_orientation" / "AnchorOrientation.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                entrypoint,
                {
                    "comp:R1.R": Position(-2000, 0, rotation=270),
                    "comp:U1.U": Position(300, 0),
                    "comp:R2.R": Position(600, 0, rotation=270),
                    "sym:INPUT#0": Position(-2100, 0),
                },
            ),
        )
    )

    compacted = compact_excessive_signal_block_gaps(schematic, plan)
    before = plan.modules[0].positions
    after = compacted.modules[0].positions

    assert after["comp:R1.R"].x > before["comp:R1.R"].x
    assert after["sym:INPUT#0"].x - before["sym:INPUT#0"].x == pytest.approx(
        after["comp:R1.R"].x - before["comp:R1.R"].x
    )
    assert after["comp:U1.U"] == before["comp:U1.U"]
    assert after["comp:R2.R"] == before["comp:R2.R"]
    assert connectivity_digest(compacted.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
def test_tight_connected_block_gap_is_opened_away_from_primary() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "anchor_orientation" / "AnchorOrientation.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:R1.R": Position(-200, 0, rotation=270),
        "comp:U1.U": Position(300, 0),
        "comp:R2.R": Position(500, 0, rotation=270),
        "sym:OUTPUT#0": Position(600, 0),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, entrypoint, positions),))

    separated = separate_tight_signal_block_gaps(schematic, plan, minimum_gap=160)
    before = plan.modules[0].positions
    after = separated.modules[0].positions
    proposed = separated.apply_to_schematic(schematic)

    assert after["comp:R2.R"].x > before["comp:R2.R"].x
    assert after["sym:OUTPUT#0"].x - before["sym:OUTPUT#0"].x == pytest.approx(
        after["comp:R2.R"].x - before["comp:R2.R"].x
    )
    assert after["comp:U1.U"] == before["comp:U1.U"]
    assert after["comp:R1.R"] == before["comp:R1.R"]
    assert not top_level_block_clearance_findings(proposed, minimum_clearance=160)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.e2e
def test_root_net_symbol_ownership_is_constrained_by_connectivity() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "anchor_orientation" / "AnchorOrientation.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                entrypoint,
                {
                    "comp:R1.R": Position(-300, 0, rotation=270),
                    "comp:U1.U": Position(300, 0),
                    "comp:R2.R": Position(900, 0, rotation=270),
                    # Deliberately put VDD on top of an unrelated component.
                    "sym:VDD#0": Position(-300, 0),
                },
            ),
        )
    )

    assignments = top_level_root_symbol_groups(plan.apply_to_schematic(schematic))

    assert assignments["sym:VDD#0"] == "U1"


@pytest.mark.e2e
def test_parallel_rail_labels_receive_readable_pitch() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "common_power_branch" / "CommonPowerBranch.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:U1.U": Position(300, 200),
        "sym:VRAW#0": Position(100, 0),
        "sym:VSS#0": Position(140, 0),
        "sym:VDD#0": Position(180, 0),
        "sym:GND#0": Position(300, 400),
        "sym:INPUT#0": Position(0, 200),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, entrypoint, positions),))

    spread = spread_parallel_rail_labels(schematic, plan, minimum_pitch=160)
    result = spread.modules[0].positions
    rail_x = [result[symbol_id].x for symbol_id in ("sym:VRAW#0", "sym:VSS#0", "sym:VDD#0")]

    assert all(right - left >= 160 for left, right in zip(rail_x, rail_x[1:], strict=False))
    assert result["comp:U1.U"] == positions["comp:U1.U"]
    assert result["sym:INPUT#0"] == positions["sym:INPUT#0"]
    assert connectivity_digest(spread.apply_to_schematic(schematic)) == connectivity_digest(
        schematic
    )


@pytest.mark.e2e
def test_repeated_active_channels_receive_body_and_annotation_corridors() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture_dir = FIXTURES / "package_projection" / "active_connector"
    entrypoint = fixture_dir / "RepeatedActiveChannels.zen"
    expected = json.loads((fixture_dir / "expected-repeated-layout.json").read_text())
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:R_A.R": Position(250, 0, rotation=270),
        "comp:R_B.R": Position(250, 150, rotation=270),
        "comp:R_C.R": Position(250, 300, rotation=270),
        "comp:ACTIVE_A": Position(500, 0),
        "comp:ACTIVE_B": Position(500, 150),
        "comp:ACTIVE_C": Position(500, 300),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, entrypoint, positions),))

    spread = spread_repeated_active_channels(
        schematic,
        plan,
        minimum_body_clearance=expected["minimum_body_clearance"],
    )
    result = spread.modules[0].positions
    proposed = spread.apply_to_schematic(schematic)
    active_ids = ("comp:ACTIVE_A", "comp:ACTIVE_B", "comp:ACTIVE_C")
    active_refs = tuple(
        root_ref + "." + symbol_id.removeprefix("comp:") for symbol_id in active_ids
    )
    bounds = [
        placed_symbol_bounds(
            proposed["instances"][component_ref],
            result[symbol_id],
        )
        for component_ref, symbol_id in zip(active_refs, active_ids, strict=True)
    ]

    assert all(
        lower.min_y - upper.max_y >= expected["minimum_body_clearance"]
        for upper, lower in zip(bounds, bounds[1:], strict=False)
    )
    for suffix in ("A", "B", "C"):
        assert (
            result[f"comp:R_{suffix}.R"].y - result[f"comp:ACTIVE_{suffix}"].y
            == positions[f"comp:R_{suffix}.R"].y - positions[f"comp:ACTIVE_{suffix}"].y
        )
    assert connectivity_digest(proposed) == connectivity_digest(schematic)
