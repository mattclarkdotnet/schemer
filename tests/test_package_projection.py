from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from schemer.active_projection import _active_body_symbol, project_connector_like_active_blocks
from schemer.anchor_process import center_primary_ic
from schemer.generic import generic_flat_layout
from schemer.layout import LayoutPlan, ModuleLayout, Position
from schemer.layout_metrics import (
    primary_anchor_metrics,
    top_level_group_envelopes,
    top_level_root_symbol_groups,
)
from schemer.package_projection import _package_body_symbol, collapse_multi_unit_ic_packages
from schemer.primary_projection import (
    ordered_perimeter_symbol,
    project_primary_ic_symbol,
)
from schemer.projection_view import schematic_with_symbol_overrides, select_kicad_symbol
from schemer.quality import (
    top_level_block_clearance_findings,
    top_level_block_overlap_findings,
)
from schemer.shadow import materialize_proposal_shadow
from schemer.spacing import (
    compact_excessive_signal_block_gaps,
    pack_top_level_groups,
    separate_primary_neighbour_overlaps,
    separate_tight_signal_block_gaps,
    spread_parallel_rail_labels,
    spread_repeated_active_channels,
)
from schemer.symbol_geometry import pin_positions, placed_symbol_bounds, symbol_pin_offsets
from schemer.toolchain import (
    DEFAULT_PCB_COMPILER,
    connectivity_digest,
    evaluate_zener,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_WORKSPACE = Path(
    os.environ.get(
        "SCHEMER_SAMPLE_WORKSPACE",
        str(Path(__file__).parent / "fixtures/sample-board"),
    )
)
SAMPLE_BOARD = SAMPLE_WORKSPACE / "boards" / "sample-board" / "SampleBoard.zen"


@pytest.mark.e2e
def test_multi_unit_ic_round_trips_as_one_generic_package(tmp_path: Path) -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture_dir = FIXTURES / "package_projection" / "multi_unit_ic"
    entrypoint = fixture_dir / "MultiUnitIc.zen"
    expected = json.loads((fixture_dir / "expected-layout.json").read_text())
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    unit_count = expected["expected_unit_count_before"]
    positions = {
        f"comp:BUFFER@U{unit}": Position(100, unit * 100) for unit in range(1, unit_count + 1)
    }
    plan = LayoutPlan((ModuleLayout(root_ref, entrypoint, positions),))

    projection = collapse_multi_unit_ic_packages(schematic, plan, entrypoint)

    assert len(projection.collapsed_component_refs) == 1
    assert set(projection.plan.modules[0].positions) == {"comp:BUFFER"}
    assert len(projection.file_overrides) == 1

    shadow = materialize_proposal_shadow(
        entrypoint,
        projection.plan.proposed_sources(),
        tmp_path / "multi-unit-shadow",
        file_overrides=projection.file_overrides,
    )
    rebuilt = evaluate_zener(shadow.entrypoint, DEFAULT_PCB_COMPILER)

    assert connectivity_digest(rebuilt) == connectivity_digest(schematic)
    root_positions = rebuilt["instances"][rebuilt["root_ref"]]["symbol_positions"]
    assert set(root_positions) == {"comp:BUFFER"}
    component = next(
        instance
        for instance in rebuilt["instances"].values()
        if isinstance(instance, dict) and instance.get("kind") == "Component"
    )
    pin_offsets = symbol_pin_offsets(component)
    assert all(pin_offsets[pin][0] < 0 for pin in expected["input_side"])
    assert all(pin_offsets[pin][0] > 0 for pin in expected["output_side"])
    # Arbitrary millimetre pitches can look aligned to our own geometry model
    # yet cause the actual viewer to insert tiny steps at every pin exit.
    for x, y in pin_offsets.values():
        assert x / 1.27 == pytest.approx(round(x / 1.27))
        assert y / 1.27 == pytest.approx(round(y / 1.27))
    for pins in (expected["input_side"], expected["output_side"]):
        rows = sorted({pin_offsets[pin][1] for pin in pins})
        assert all(b - a == pytest.approx(10.16) for a, b in zip(rows, rows[1:]))


@pytest.mark.e2e
def test_largest_generic_ic_is_moved_to_sheet_centre() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "anchor_orientation" / "AnchorOrientation.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = dict(generic_flat_layout(schematic).positions)
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
    separated_positions = generic_flat_layout(schematic).positions
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
def test_primary_body_stays_fixed_while_an_overlapping_neighbour_moves() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "anchor_orientation" / "AnchorOrientation.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    positions = {
        "comp:R1.R": Position(-200, 0, rotation=270),
        "comp:U1.U": Position(300, 0),
        "comp:R2.R": Position(300, 0, rotation=270),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, entrypoint, positions),))

    separated = separate_primary_neighbour_overlaps(schematic, plan, clearance=30)
    result = separated.modules[0].positions
    proposed = separated.apply_to_schematic(schematic)
    primary_bounds = placed_symbol_bounds(
        proposed["instances"][root_ref + ".U1.U"],
        result["comp:U1.U"],
    )
    neighbour_bounds = placed_symbol_bounds(
        proposed["instances"][root_ref + ".R2.R"],
        result["comp:R2.R"],
    )

    assert result["comp:U1.U"] == positions["comp:U1.U"]
    assert result["comp:R2.R"] != positions["comp:R2.R"]
    assert (
        neighbour_bounds.min_x - primary_bounds.max_x >= 30
        or primary_bounds.min_x - neighbour_bounds.max_x >= 30
        or neighbour_bounds.min_y - primary_bounds.max_y >= 30
        or primary_bounds.min_y - neighbour_bounds.max_y >= 30
    )
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.e2e
def test_complete_top_level_groups_are_frozen_then_packed() -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "generic_layouts" / "anchor_orientation" / "AnchorOrientation.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    schematic["instances"][root_ref + ".R2.R"]["attributes"]["value"] = {
        "String": "LONG ELECTRICAL VALUE"
    }
    positions = {
        "comp:R1.R": Position(-200, 0, rotation=270),
        "comp:U1.U": Position(300, 0),
        "comp:R2.R": Position(300, 0, rotation=270),
    }
    plan = LayoutPlan((ModuleLayout(root_ref, entrypoint, positions),))

    current = plan.apply_to_schematic(schematic)
    packed = pack_top_level_groups(current, plan, clearance=100)
    result = packed.modules[0].positions
    proposed = packed.apply_to_schematic(schematic)
    envelopes = top_level_group_envelopes(proposed)

    assert result["comp:U1.U"] == positions["comp:U1.U"]
    assert result["comp:R2.R"].x != positions["comp:R2.R"].x
    assert envelopes["U1"].min_x - envelopes["R1"].max_x == pytest.approx(100)
    assert envelopes["R2"].min_x - envelopes["U1"].max_x == pytest.approx(100)
    r2_geometry = placed_symbol_bounds(
        proposed["instances"][root_ref + ".R2.R"],
        result["comp:R2.R"],
    )
    assert envelopes["R2"].width > r2_geometry.max_x - r2_geometry.min_x + 100
    assert not top_level_block_overlap_findings(proposed)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)
    assert pack_top_level_groups(proposed, packed, clearance=100) == packed


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


@pytest.mark.e2e
def test_connector_like_active_device_projects_as_functional_block(
    tmp_path: Path,
) -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    fixture = FIXTURES / "package_projection" / "active_connector" / "ActiveConnector.zen"
    schematic = evaluate_zener(fixture, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan((ModuleLayout(root_ref, fixture, {"comp:ACTIVE": Position(100, 100)}),))

    projection = project_connector_like_active_blocks(schematic, plan, fixture)

    assert len(projection.projected_component_refs) == 1
    assert len(projection.file_overrides) == 1
    assert '(text "transceiver"' not in next(iter(projection.file_overrides.values()))
    shadow = materialize_proposal_shadow(
        fixture,
        plan.proposed_sources(),
        tmp_path / "active-shadow",
        file_overrides=projection.file_overrides,
    )
    rebuilt = evaluate_zener(shadow.entrypoint, DEFAULT_PCB_COMPILER)
    assert connectivity_digest(rebuilt) == connectivity_digest(schematic)
    component = next(
        instance
        for instance in rebuilt["instances"].values()
        if isinstance(instance, dict) and instance.get("kind") == "Component"
    )
    offsets = symbol_pin_offsets(component)
    assert offsets["VCC"][1] > 0
    assert offsets["GND"][1] < 0
    assert offsets["SIGNAL"][0] < 0


def test_active_body_captions_hidden_generic_pin_names_from_semantic_type() -> None:
    component_ref = "root.RECEIVER"
    pins = "".join(
        f'''(pin passive line (at 0 {index * 2.54} 0) (length 2.54)
          (name "Pin_{index}") (number "{index}"))'''
        for index in range(1, 5)
    )
    instance = {
        "attributes": {
            "__symbol_value": {"String": f'(symbol "GENERIC" {pins})'},
            "prefix": {"String": "U"},
            "type": {"String": "optical_receiver"},
        },
        "children": {f"Pin_{index}": f"{component_ref}.Pin_{index}" for index in range(1, 5)},
    }
    schematic = {
        "nets": {
            "VCC": {"name": "VCC", "kind": "Power", "ports": [f"{component_ref}.Pin_1"]},
            "GND": {"name": "GND", "kind": "Ground", "ports": [f"{component_ref}.Pin_2"]},
            "A": {"name": "A", "kind": "Net", "ports": [f"{component_ref}.Pin_3"]},
            "B": {"name": "B", "kind": "Net", "ports": [f"{component_ref}.Pin_4"]},
        }
    }

    generated = _active_body_symbol("GENERIC", component_ref, instance, schematic)

    assert '(text "optical"' in generated
    assert '(text "receiver"' in generated
    assert "optical_receiver" not in generated


@pytest.mark.parametrize("side_count", [3, 4, 5, 6])
def test_package_caption_uses_free_centre_not_a_pin_row(side_count: int) -> None:
    component_ref = "fixture.PACKAGE"
    pins = "".join(
        f'''(pin {"input" if index <= side_count else "output"} line
          (at 0 {index * 2.54} 0) (length 2.54)
          (name "{index}") (number "{index}"))'''
        for index in range(1, side_count * 2 + 1)
    )
    instance = {
        "attributes": {
            "__symbol_value": {"String": f'(symbol "GENERIC" {pins})'},
            "type": {"String": "logic_inverter_schmitt"},
        }
    }
    schematic = {
        "nets": {
            str(index): {"kind": "Net", "ports": [f"{component_ref}.{index}"]}
            for index in range(1, side_count * 2 + 1)
        }
    }
    generated = _package_body_symbol("GENERIC", component_ref, instance, schematic)
    for word in ("logic", "inverter", "schmitt"):
        assert (f'(text "{word}"' in generated) == (side_count % 2 == 0)

    instance["attributes"].pop("type")
    generated_without_type = _active_body_symbol("GENERIC", component_ref, instance, schematic)
    assert "(text " not in generated_without_type


@pytest.mark.e2e
def test_three_terminal_control_device_faces_owner_signal_and_control_outward() -> None:
    if not DEFAULT_PCB_COMPILER.is_file() or not SAMPLE_BOARD.is_file():
        pytest.skip("local sample-board compiler fixture is unavailable")

    schematic = evaluate_zener(SAMPLE_BOARD, DEFAULT_PCB_COMPILER)
    module_ref = schematic["root_ref"] + ".DSP_CORE"
    source = Path(schematic["instances"][module_ref]["type_ref"]["source_path"])
    q_symbol = "comp:Q101.DMG3402L-7"
    q_ref = module_ref + ".Q101.DMG3402L-7"
    plan = LayoutPlan(
        (
            ModuleLayout(
                module_ref,
                source,
                {
                    "comp:A101.PICO_2": Position(0, 0),
                    q_symbol: Position(500, 0),
                },
            ),
        )
    )

    projection = project_connector_like_active_blocks(schematic, plan, SAMPLE_BOARD)
    projected = schematic_with_symbol_overrides(
        schematic,
        SAMPLE_BOARD,
        projection.file_overrides,
    )
    q = projected["instances"][q_ref]
    position = Position(0, 0)
    bounds = placed_symbol_bounds(q, position)

    assert q_ref in projection.projected_component_refs
    assert pin_positions(q, position, "D")[0].x < bounds.center_x
    assert pin_positions(q, position, "G")[0].x > bounds.center_x
    assert pin_positions(q, position, "S")[0].y > bounds.center_y


def test_generic_sequential_pin_symbol_follows_physical_perimeter_order() -> None:
    pins = "\n".join(
        f'''(pin passive line
          (at {(-5 if number % 2 else 5)} {5 - number} {0 if number % 2 else 180})
          (length 2) (name "Pin_{number}") (number "{number}"))'''
        for number in range(1, 9)
    )
    instance = {
        "children": {f"Pin_{number}": f"component.Pin_{number}" for number in range(1, 9)},
        "attributes": {
            "__symbol_value": {"String": f'(symbol "GENERIC" {pins})'},
            "prefix": {"String": "A"},
            "value": {"String": "Generic module"},
        },
    }

    library = ordered_perimeter_symbol(instance, "GENERIC")

    assert library is not None
    projected = {
        "attributes": {"__symbol_value": {"String": select_kicad_symbol(library, "GENERIC")}}
    }
    offsets = symbol_pin_offsets(projected)
    assert all(offsets[f"Pin_{number}"][0] < 0 for number in range(1, 5))
    assert offsets["Pin_1"][1] > offsets["Pin_4"][1]
    assert all(offsets[f"Pin_{number}"][0] > 0 for number in range(5, 9))
    assert offsets["Pin_5"][1] < offsets["Pin_8"][1]


def test_ordered_perimeter_keeps_authored_functional_pin_names() -> None:
    instance = {
        "children": {"VCC": "component.VCC", "GND": "component.GND"},
        "attributes": {
            "__symbol_value": {
                "String": """(symbol "FUNCTIONAL"
                  (pin power_in line (at -5 0 0) (length 2)
                    (name "VCC") (number "1"))
                  (pin power_in line (at 5 0 180) (length 2)
                    (name "GND") (number "2")))"""
            }
        },
    }

    assert ordered_perimeter_symbol(instance, "FUNCTIONAL") is None


@pytest.mark.e2e
def test_ordered_perimeter_projection_round_trips_through_compiler(tmp_path: Path) -> None:
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip(f"local Zener compiler not found: {DEFAULT_PCB_COMPILER}")

    entrypoint = FIXTURES / "package_projection" / "ordered_perimeter" / "OrderedPerimeter.zen"
    schematic = evaluate_zener(entrypoint, DEFAULT_PCB_COMPILER)
    root_ref = schematic["root_ref"]
    plan = LayoutPlan((ModuleLayout(root_ref, entrypoint, {"comp:MODULE": Position(100, 100)}),))

    projection = project_primary_ic_symbol(schematic, plan, entrypoint)
    shifted = project_primary_ic_symbol(
        schematic,
        LayoutPlan((ModuleLayout(root_ref, entrypoint, {"comp:MODULE": Position(9100, -4200)}),)),
        entrypoint,
    )

    assert projection.ordered_perimeter
    assert shifted.file_overrides == projection.file_overrides
    shadow = materialize_proposal_shadow(
        entrypoint,
        plan.proposed_sources(),
        tmp_path / "ordered-perimeter-shadow",
        file_overrides=projection.file_overrides,
    )
    rebuilt = evaluate_zener(shadow.entrypoint, DEFAULT_PCB_COMPILER)
    assert connectivity_digest(rebuilt) == connectivity_digest(schematic)
    component = next(
        instance
        for instance in rebuilt["instances"].values()
        if isinstance(instance, dict) and instance.get("kind") == "Component"
    )
    offsets = symbol_pin_offsets(component)
    assert all(offsets[f"Pin_{number}"][0] < 0 for number in range(1, 5))
    assert offsets["Pin_1"][1] > offsets["Pin_4"][1]
    assert all(offsets[f"Pin_{number}"][0] > 0 for number in range(5, 9))
    assert offsets["Pin_5"][1] < offsets["Pin_8"][1]
