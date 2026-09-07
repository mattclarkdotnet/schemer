from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from schemer.generic import generic_flat_layout
from schemer.layout import LayoutPlan, ModuleLayout, Position, replace_position_block
from schemer.process import PlacementDecision
from schemer.quality import component_body_overlap_findings
from schemer.symbol_geometry import (
    align_horizontal_series_terminals,
    align_leaf_series_endpoints,
    align_series_to_device_pins,
    cluster_local_decoupling,
    compact_terminal_connector_gaps,
    hang_leaf_series_from_device_pins,
    orient_terminal_connectors_toward_series_banks,
    orient_two_terminal_components,
    pin_position,
    schematic_quality_findings,
    separate_orthogonal_branch_lanes,
)
from schemer.toolchain import (
    DEFAULT_PCB_COMPILER,
    ToolchainError,
    connectivity_digest,
    evaluate_zener,
)

FIXTURES = Path(__file__).parent / "fixtures" / "generic_layouts"


def _position(positions: dict[str, Position], symbol_id: str) -> Position:
    assert symbol_id in positions, f"fixture refers to absent symbol {symbol_id}"
    return positions[symbol_id]


def _assert_relations(positions: dict[str, Position], expected: dict[str, Any]) -> None:
    for sequence in expected.get("left_to_right", []):
        xs = [_position(positions, symbol_id).x for symbol_id in sequence]
        assert all(left < right for left, right in zip(xs, xs[1:], strict=False))
    for sequence in expected.get("same_y", []):
        assert len({_position(positions, symbol_id).y for symbol_id in sequence}) == 1
    for sequence in expected.get("same_x", []):
        assert len({_position(positions, symbol_id).x for symbol_id in sequence}) == 1
    for left, right in expected.get("different_x", []):
        assert _position(positions, left).x != _position(positions, right).x
    for left, right in expected.get("different_y", []):
        assert _position(positions, left).y != _position(positions, right).y
    for lower, upper in expected.get("below", []):
        assert _position(positions, lower).y > _position(positions, upper).y
    for upper, lower in expected.get("above", []):
        assert _position(positions, upper).y < _position(positions, lower).y
    for subject, anchor in expected.get("right_of", []):
        assert _position(positions, subject).x > _position(positions, anchor).x
    for subject, left, right in expected.get("x_between", []):
        subject_x = _position(positions, subject).x
        bounds = sorted((_position(positions, left).x, _position(positions, right).x))
        assert bounds[0] < subject_x < bounds[1]
    for subject, anchor, maximum_distance in expected.get("near", []):
        subject_position = _position(positions, subject)
        anchor_position = _position(positions, anchor)
        distance = abs(subject_position.x - anchor_position.x) + abs(
            subject_position.y - anchor_position.y
        )
        assert distance <= maximum_distance
    for nearer, farther, anchor in expected.get("closer_to", []):
        anchor_position = _position(positions, anchor)
        nearer_position = _position(positions, nearer)
        farther_position = _position(positions, farther)
        nearer_distance = abs(nearer_position.x - anchor_position.x) + abs(
            nearer_position.y - anchor_position.y
        )
        farther_distance = abs(farther_position.x - anchor_position.x) + abs(
            farther_position.y - anchor_position.y
        )
        assert nearer_distance < farther_distance
    for symbol_id, rotation in expected.get("rotations", {}).items():
        assert _position(positions, symbol_id).rotation == rotation


def _assert_trace(decisions: dict[str, PlacementDecision], expected: dict[str, Any]) -> None:
    for symbol_id, role in expected.get("roles", {}).items():
        assert decisions[symbol_id].role.value == role
    for symbol_id, stage in expected.get("stages", {}).items():
        assert decisions[symbol_id].stage.value == stage
    for symbol_id, owner in expected.get("owners", {}).items():
        assert decisions[symbol_id].owner == owner
    for symbol_id, rotation in expected.get("rotations", {}).items():
        assert decisions[symbol_id].rotation == rotation


def _assert_terminal_flows(
    schematic: dict[str, Any], positions: dict[str, Position], expected: dict[str, Any]
) -> None:
    root_ref = schematic["root_ref"]
    for flow in expected.get("terminal_flows", []):
        symbol_id = flow["component"]
        component_ref = root_ref + "." + symbol_id.removeprefix("comp:")
        instance = schematic["instances"][component_ref]
        upstream = pin_position(instance, positions[symbol_id], flow["upstream_pin"])
        downstream = pin_position(instance, positions[symbol_id], flow["downstream_pin"])
        assert upstream.x < downstream.x, (
            f"{symbol_id} terminal order opposes semantic left-to-right flow: "
            f"{flow['upstream_pin']}={upstream.x}, {flow['downstream_pin']}={downstream.x}"
        )


@pytest.mark.e2e
@pytest.mark.parametrize("fixture_dir", sorted(FIXTURES.iterdir()), ids=lambda path: path.name)
def test_generic_layout_round_trip(fixture_dir: Path, tmp_path: Path) -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    working_dir = tmp_path / fixture_dir.name
    shutil.copytree(fixture_dir, working_dir)
    entrypoint = next(working_dir.glob("*.zen"))
    expected = json.loads((working_dir / "expected-layout.json").read_text())
    assert isinstance(expected.get("scenario"), str) and expected["scenario"]

    before = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    layout = generic_flat_layout(before)
    positions = layout.positions

    assert layout == generic_flat_layout(before), "layout and trace must be deterministic"
    _assert_relations(positions, expected)
    _assert_trace(layout.decisions, expected)
    _assert_terminal_flows(before, positions, expected)

    entrypoint.write_text(replace_position_block(entrypoint.read_text(), positions))
    after = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)

    assert connectivity_digest(after) == connectivity_digest(before)
    root = after["instances"][after["root_ref"]]
    accepted = root.get("symbol_positions", {})
    assert set(accepted) == set(positions)
    assert accepted == {
        symbol_id: position.as_viewer_dict() for symbol_id, position in positions.items()
    }


@pytest.mark.e2e
def test_repeated_series_bank_plan_is_oriented_from_topology_not_identifiers() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = FIXTURES / "repeated_series_bank" / "RepeatedSeriesBank.zen"
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:R1.R": Position(300, 100, rotation=90),
        "comp:R2.R": Position(300, 200, rotation=90),
        "comp:R3.R": Position(300, 300, rotation=90),
        "comp:R4.R": Position(600, 200, rotation=90),
        "sym:INPUT_A#0": Position(0, 100),
        "sym:INPUT_B#0": Position(0, 200),
        "sym:INPUT_C#0": Position(0, 300),
        "sym:COMMON#0": Position(600, 350),
        "sym:OUTPUT#0": Position(900, 200),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, fixture, positions),))

    oriented = orient_two_terminal_components(schematic, plan)

    assert {
        symbol_id: position.rotation
        for symbol_id, position in oriented.modules[0].positions.items()
        if symbol_id.startswith("comp:")
    } == {
        "comp:R1.R": 270,
        "comp:R2.R": 270,
        "comp:R3.R": 270,
        "comp:R4.R": 270,
    }


@pytest.mark.e2e
def test_local_decoupling_is_attached_to_its_connectivity_owner() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = FIXTURES / "decoupled_ic" / "DecoupledIc.zen"
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                fixture,
                {
                    "comp:U1.U": Position(100, 100),
                    "comp:C1.C": Position(1500, 100),
                },
            ),
        )
    )

    before = schematic_quality_findings(plan.apply_to_schematic(schematic))
    clustered = cluster_local_decoupling(schematic, plan)
    positions = clustered.modules[0].positions
    after = schematic_quality_findings(clustered.apply_to_schematic(schematic))

    assert any(finding.code == "local-passive-owner-gap" for finding in before)
    assert not any(finding.code == "local-passive-owner-gap" for finding in after)
    assert positions["comp:U1.U"] == Position(100, 100)
    assert abs(positions["comp:C1.C"].x - positions["comp:U1.U"].x) < 200
    assert positions["comp:C1.C"].x > positions["comp:U1.U"].x


@pytest.mark.e2e
def test_sequential_decouplers_cannot_claim_the_same_owner_slot() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = FIXTURES / "ordered_decoupling" / "OrderedDecoupling.zen"
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                fixture,
                {
                    "comp:U1.U": Position(100, 100),
                    "comp:C1.C": Position(1000, 100),
                    "comp:C2.C": Position(1100, 100),
                },
            ),
        )
    )

    clustered = cluster_local_decoupling(schematic, plan)
    positions = clustered.modules[0].positions

    assert positions["comp:C1.C"] != positions["comp:C2.C"]
    assert not component_body_overlap_findings(clustered.apply_to_schematic(schematic))


@pytest.mark.e2e
def test_unowned_local_rail_passive_is_rejected() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = FIXTURES / "decoupled_ic" / "DecoupledIc.zen"
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                fixture,
                {"comp:C1.C": Position(900, 100)},
            ),
        )
    )

    with pytest.raises(ToolchainError, match="unowned local rail passive"):
        cluster_local_decoupling(schematic, plan)


@pytest.mark.e2e
def test_vertical_rail_branch_moves_out_of_a_colliding_series_lane() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = FIXTURES / "series_with_shunt" / "SeriesWithShunt.zen"
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:R1.R": Position(100, 0, rotation=270),
        "comp:R2.R": Position(300, 0, rotation=270),
        "comp:R3.R": Position(300, 60, rotation=0),
        "sym:INPUT#0": Position(-100, 0),
        "sym:OUTPUT#0": Position(500, 0),
        "sym:GND#0": Position(300, 180),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, fixture, positions),))

    refined = separate_orthogonal_branch_lanes(schematic, plan)
    result = refined.modules[0].positions

    assert result["comp:R1.R"] == positions["comp:R1.R"]
    assert result["comp:R2.R"] == positions["comp:R2.R"]
    assert result["comp:R1.R"].x < result["comp:R3.R"].x < result["comp:R2.R"].x
    assert result["sym:GND#0"].x == result["comp:R3.R"].x


@pytest.mark.e2e
def test_terminal_connector_closes_excessive_series_bank_gap() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture_dir = Path(__file__).parent / "fixtures" / "refinement" / "terminal_series_pair"
    fixture = fixture_dir / "TerminalSeriesPair.zen"
    expected = json.loads((fixture_dir / "expected-layout.json").read_text())
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:R1.R": Position(700, 350, rotation=90),
        "comp:R2.R": Position(700, 650, rotation=90),
        "comp:J1.PH": Position(1300, 500, rotation=90),
        "sym:INPUT_P#0": Position(400, 450),
        "sym:INPUT_N#0": Position(400, 550),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, fixture, positions),))

    before_findings = schematic_quality_findings(plan.apply_to_schematic(schematic))
    assert any(finding.code == "series-terminal-dogleg" for finding in before_findings)

    oriented = orient_terminal_connectors_toward_series_banks(schematic, plan)
    leaves_aligned = align_leaf_series_endpoints(schematic, oriented)
    aligned = align_horizontal_series_terminals(schematic, leaves_aligned)
    refined = compact_terminal_connector_gaps(schematic, aligned)
    result = refined.modules[0].positions

    _assert_relations(result, expected)
    connector = result["comp:J1.PH"]
    assert abs(connector.x - result["comp:R1.R"].x) <= expected["maximum_gap"] + 1e-6
    assert result["sym:INPUT_P#0"].x < result["comp:R1.R"].x < connector.x
    assert result["sym:INPUT_N#0"].x < result["comp:R2.R"].x < connector.x
    assert (
        abs(result["comp:R1.R"].x - result["comp:R2.R"].x) >= expected["minimum_horizontal_stagger"]
    )
    assert result["comp:R1.R"].y > positions["comp:R1.R"].y
    assert result["comp:R2.R"].y < positions["comp:R2.R"].y
    after_findings = schematic_quality_findings(refined.apply_to_schematic(schematic))
    assert not any(
        finding.code in {"series-terminal-dogleg", "series-bank-pitch"}
        for finding in after_findings
    )


@pytest.mark.e2e
def test_leaf_series_bank_hangs_from_the_connected_device_side() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "refinement"
        / "terminal_series_pair"
        / "TerminalSeriesPair.zen"
    )
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                fixture,
                {
                    "comp:R1.R": Position(700, 350, rotation=90),
                    "comp:R2.R": Position(700, 650, rotation=90),
                    "comp:J1.PH": Position(1300, 500, rotation=90),
                    "sym:INPUT_P#0": Position(400, 450),
                    "sym:INPUT_N#0": Position(400, 550),
                },
            ),
        )
    )

    oriented = orient_terminal_connectors_toward_series_banks(schematic, plan)
    hung = hang_leaf_series_from_device_pins(schematic, oriented)
    positions = hung.modules[0].positions

    connector_x = positions["comp:J1.PH"].x
    assert positions["sym:INPUT_P#0"].x < positions["comp:R1.R"].x < connector_x
    assert positions["sym:INPUT_N#0"].x < positions["comp:R2.R"].x < connector_x
    assert abs(positions["comp:R2.R"].x - positions["comp:R1.R"].x) >= 90
    assert not any(
        finding.code == "series-terminal-dogleg"
        for finding in schematic_quality_findings(hung.apply_to_schematic(schematic))
    )


@pytest.mark.e2e
def test_series_elements_reregister_to_projected_device_pin_pitch() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "refinement"
        / "terminal_series_pair"
        / "TerminalSeriesPair.zen"
    )
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                fixture,
                {
                    "comp:R1.R": Position(700, 200, rotation=90),
                    "comp:R2.R": Position(700, 800, rotation=90),
                    "comp:J1.PH": Position(1100, 500, rotation=90),
                    "sym:INPUT_P#0": Position(400, 200),
                    "sym:INPUT_N#0": Position(400, 800),
                },
            ),
        )
    )
    oriented = orient_terminal_connectors_toward_series_banks(schematic, plan)
    registered = align_series_to_device_pins(schematic, oriented)
    positioned = registered.apply_to_schematic(schematic)
    positions = registered.modules[0].positions

    connector_ref = root_ref + ".J1.PH"
    connector = positioned["instances"][connector_ref]
    connector_position = positions["comp:J1.PH"]
    for resistor_name, net_name in (("R1", "TERMINAL_P"), ("R2", "TERMINAL_N")):
        resistor_ref = f"{root_ref}.{resistor_name}.R"
        resistor = positioned["instances"][resistor_ref]
        net = next(
            net
            for net in positioned["nets"].values()
            if isinstance(net, dict) and str(net.get("name", "")).endswith(net_name)
        )
        resistor_terminal = next(
            port.removeprefix(resistor_ref + ".")
            for port in net["ports"]
            if port.startswith(resistor_ref + ".")
        )
        connector_terminal = next(
            port.removeprefix(connector_ref + ".")
            for port in net["ports"]
            if port.startswith(connector_ref + ".")
        )
        resistor_pin = pin_position(
            resistor,
            positions[f"comp:{resistor_name}.R"],
            resistor_terminal,
        )
        connector_pin = pin_position(
            connector,
            connector_position,
            connector_terminal,
        )
        assert resistor_pin.y == pytest.approx(connector_pin.y)


@pytest.mark.e2e
def test_misplaced_series_bank_moves_between_unambiguous_endpoints() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = (
        Path(__file__).parent
        / "fixtures"
        / "refinement"
        / "terminal_series_pair"
        / "TerminalSeriesPair.zen"
    )
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root_ref,
                fixture,
                {
                    "comp:R1.R": Position(0, 450, rotation=90),
                    "comp:R2.R": Position(0, 550, rotation=90),
                    "comp:J1.PH": Position(1100, 500, rotation=90),
                    "sym:INPUT_P#0": Position(400, 450),
                    "sym:INPUT_N#0": Position(400, 550),
                },
            ),
        )
    )

    oriented = orient_terminal_connectors_toward_series_banks(schematic, plan)
    aligned = align_horizontal_series_terminals(
        schematic,
        oriented,
        maximum_adjustment=1000,
    )
    positions = aligned.modules[0].positions

    assert positions["comp:R1.R"].x > plan.modules[0].positions["comp:R1.R"].x
    assert positions["comp:R2.R"].x > plan.modules[0].positions["comp:R2.R"].x
    assert positions["sym:INPUT_P#0"].x < positions["comp:R1.R"].x
    assert positions["sym:INPUT_N#0"].x < positions["comp:R2.R"].x
    assert abs(positions["comp:R1.R"].x - positions["comp:R2.R"].x) >= 100
