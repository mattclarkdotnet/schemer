import json
import os
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from schemer.active_projection import project_connector_like_active_blocks
from schemer.block_generation import generate_functional_ic_blocks
from schemer.heuristic_block import (
    _PIN_EXIT_STUB,
    _fan_in_continuation_coordinate,
    _net_symbol_attachment,
)
from schemer.layout import LayoutPlan, ModuleLayout, Position
from schemer.package_projection import collapse_multi_unit_ic_packages
from schemer.projection_view import schematic_with_symbol_overrides
from schemer.quality import component_body_overlap_findings
from schemer.signal_terminations import with_signal_termination_symbols
from schemer.symbol_geometry import (
    Point,
    net_symbol_pin_position,
    pin_outward_side,
    pin_positions,
    placed_symbol_body_bounds,
    placed_symbol_bounds,
    schematic_quality_findings,
)
from schemer.toolchain import DEFAULT_PCB_COMPILER, connectivity_digest, evaluate_zener
from schemer.view_policy import clean_schematic_labels

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "package_projection"
    / "active_connector"
    / "IsolationBlocks.zen"
)
SAMPLE_WORKSPACE = Path(
    os.environ.get(
        "SCHEMER_SAMPLE_WORKSPACE",
        str(Path(__file__).parent / "fixtures/sample-board"),
    )
)
SAMPLE_BOARD = SAMPLE_WORKSPACE / "boards" / "sample-board" / "SampleBoard.zen"
SPDIF_INTERFACE = SAMPLE_BOARD.with_name("SpdifInterfaces.zen")
PARALLEL_TRANSFORMER = (
    Path(__file__).parent
    / "fixtures"
    / "interface_blocks"
    / "parallel_transformer"
    / "ParallelTransformer.zen"
)
PARALLEL_TRANSFORMER_EXPECTED = PARALLEL_TRANSFORMER.with_name("expected-layout.json")


@pytest.mark.parametrize("kind", ["Power", "Ground"])
@pytest.mark.parametrize("side", ["left", "right", "top", "bottom"])
def test_rail_convention_takes_precedence_over_a_straight_wire(kind, side):
    net = {"kind": kind, "name": "RAIL"}
    attachment = _net_symbol_attachment("RAIL", net, (Point(0, 0),), side)
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
        schematic, LayoutPlan((ModuleLayout(root, fixture, {}),)),
    )
    assert len(result.module_blocks) == 1
    positions = result.plan.modules[0].positions

    def pin(ref, terminal):
        return pin_positions(schematic["instances"][root + "." + ref],
                             positions["comp:" + ref], terminal)[0]

    upper, lower = pin("BIAS_UP.R", "1"), pin("BIAS_DOWN.R", "1")
    assert upper.x == pytest.approx(lower.x)
    source, sink = pin("MAIN", "OUT_B"), pin("STAGE", "PRI")
    assert source.y == pytest.approx(sink.y)
    shared = pin("SHARED_BIAS.R", "1")
    assert shared.x == pytest.approx((source.x + sink.x) / 2)
    assert source.y - shared.y == pytest.approx(40)
    assert shared.x > upper.x + 40  # includes the neighbouring branch's drawing
    control, bias = pin("STAGE", "SEC"), pin("CONTROL_BIAS.R", "1")
    assert bias.x - control.x == pytest.approx(80)
    assert bias.y - control.y == pytest.approx(40)
    assert pin_outward_side(schematic["instances"][root + ".CONTROL_BIAS.R"],
                            positions["comp:CONTROL_BIAS.R"], "1") == "top"
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
    assert _fan_in_continuation_coordinate(rows) == expected


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
    # This proves the fixture's geometry, not straight viewer routing. The
    # actual two-resistor capture still exposes the pin-exit dogleg.


def _position_geometry(schematic: dict[str, object], module_ref: str) -> list[tuple[object, ...]]:
    instances = schematic["instances"]
    assert isinstance(instances, dict)
    module = instances[module_ref]
    assert isinstance(module, dict)
    positions = module["symbol_positions"]
    assert isinstance(positions, dict)
    return sorted(
        (
            float(position["x"]),
            float(position["y"]),
            float(position.get("rotation", 0.0)),
            position.get("mirror"),
        )
        for position in positions.values()
        if isinstance(position, dict)
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
        "comp:SOURCE", "comp:R_LEFT.R", "comp:R_AUX_SHUNT.R", "comp:R_AUX_SHUNT_2.R",
    }
    assert {item.symbol_id for item in children[1].block.items} >= {
        "comp:BARRIER", "comp:C_LOCAL.C",
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
        max(point.y for point in shunt_return_points) + _PIN_EXIT_STUB,
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
            body = placed_symbol_body_bounds(schematic["instances"][root_ref + "." + ref],
                                             result["comp:" + ref])
            assert not (ground.min_x < body.max_x and body.min_x < ground.max_x
                        and ground.min_y < body.max_y and body.min_y < ground.max_y)
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


def test_non_isolation_module_is_left_unchanged() -> None:
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

    result = generate_functional_ic_blocks(schematic, plan)

    assert result.plan == plan
    assert result.module_blocks == ()


@pytest.mark.e2e
def test_completed_owner_envelopes_grow_with_captions_and_survive_later_passes() -> None:
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

    # Legacy per-symbol spacing/packing cannot dismantle a completed local
    # composition, including when that composition owns the whole sheet.
    damaged = replace(generated.plan, modules=(replace(
        generated.plan.modules[0], positions={"comp:SOURCE": Position(-9000, 5000)},
    ), ModuleLayout("unsupported", FIXTURE, {"comp:R": Position(10, 20)})))
    restored = generated.preserve_completed_modules(damaged)
    assert restored.modules[0] == generated.plan.modules[0]
    assert restored.modules[1] == damaged.modules[1]


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


@pytest.mark.e2e
def test_sample_board_dsp_block_is_generated_without_coordinate_seeds() -> None:
    if not DEFAULT_PCB_COMPILER.is_file() or not SAMPLE_BOARD.is_file():
        pytest.skip("local sample-board compiler fixture is unavailable")

    schematic = evaluate_zener(SAMPLE_BOARD, DEFAULT_PCB_COMPILER)
    module_ref = schematic["root_ref"] + ".DSP_CORE"
    source = Path(schematic["instances"][module_ref]["type_ref"]["source_path"])
    empty = LayoutPlan((ModuleLayout(module_ref, source, {}),))
    arbitrary = LayoutPlan(
        (
            ModuleLayout(
                module_ref,
                source,
                {
                    "comp:A101.PICO_2": Position(-9000, 4000),
                    "comp:D101.POWER_SCHOTTKY": Position(7000, -5000),
                    "comp:Q101.DMG3402L-7": Position(-3000, -6000),
                    "comp:R101.R": Position(8000, 8000),
                    "comp:R102.R": Position(-8000, 8000),
                    "comp:R103.R": Position(6000, 7000),
                    "comp:R104.R": Position(7000, 6000),
                    "comp:R105.R": Position(5000, 9000),
                },
            ),
        )
    )

    generated = generate_functional_ic_blocks(schematic, empty)
    shifted = generate_functional_ic_blocks(schematic, arbitrary)

    assert generated == shifted
    assert len(generated.module_blocks) == 1
    _, block_plan = generated.module_blocks[0]
    assert block_plan.root.block_id == "primary-ic-local"
    assert block_plan.findings() == ()
    proposed = generated.plan.apply_to_schematic(schematic)
    assert not component_body_overlap_findings(proposed)
    assert not schematic_quality_findings(proposed)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.e2e
def test_sample_board_usb_block_has_local_rails_and_short_series_doglegs() -> None:
    if not DEFAULT_PCB_COMPILER.is_file() or not SAMPLE_BOARD.is_file():
        pytest.skip("local sample-board compiler fixture is unavailable")

    schematic = evaluate_zener(SAMPLE_BOARD, DEFAULT_PCB_COMPILER)
    module_ref = schematic["root_ref"] + ".USB"
    source = Path(schematic["instances"][module_ref]["type_ref"]["source_path"])
    empty_plan = LayoutPlan((ModuleLayout(module_ref, source, {}),))

    result = generate_functional_ic_blocks(schematic, empty_plan)

    assert len(result.module_blocks) == 1
    _, block_plan = result.module_blocks[0]
    assert block_plan.findings() == ()
    positions = result.plan.modules[0].positions
    host = positions["comp:J301.USB4105_GF_A"]
    isolator = positions["comp:U301.ADUM3160BRWZ_RL"]
    device = positions["comp:J302.MOLEX_68784_PIGTAIL"]
    assert host.x < positions["comp:R303.R"].x < isolator.x
    assert host.x < positions["comp:R304.R"].x < isolator.x
    assert isolator.x < positions["comp:R305.R"].x < device.x
    assert isolator.x < positions["comp:R306.R"].x < device.x
    assert positions["comp:C301.C"].x < isolator.x < positions["comp:C302.C"].x
    assert sum(symbol_id.startswith("sym:USB_HOST_VDD#") for symbol_id in positions) == 2
    assert sum(symbol_id.startswith("sym:USB_DEVICE_VDD#") for symbol_id in positions) == 2

    host_instance = schematic["instances"][module_ref + ".J301.USB4105_GF_A"]
    host_vbus_pin = pin_positions(host_instance, host, "VBUS_A4")[0]
    host_vbus_net = schematic["nets"]["USB.USB_HOST_VBUS"]
    host_vbus_positions = [
        position
        for symbol_id, position in positions.items()
        if symbol_id.startswith("sym:USB_HOST_VBUS#")
    ]
    host_vbus_position = min(
        host_vbus_positions,
        key=lambda position: (
            abs(net_symbol_pin_position(host_vbus_net, position).x - host_vbus_pin.x)
            + abs(net_symbol_pin_position(host_vbus_net, position).y - host_vbus_pin.y)
        ),
    )
    host_vbus_symbol_pin = net_symbol_pin_position(host_vbus_net, host_vbus_position)
    assert host_vbus_symbol_pin.x == pytest.approx(host_vbus_pin.x + 80.0)
    assert host_vbus_symbol_pin.y == pytest.approx(host_vbus_pin.y - 40)
    assert host_vbus_position.rotation == 0.0

    host_ground_pin = pin_positions(host_instance, host, "GND_A1")[0]
    host_ground_net = schematic["nets"]["USB.USB_HOST_GND"]
    local_ground_pins = [
        net_symbol_pin_position(host_ground_net, position)
        for symbol_id, position in positions.items()
        if symbol_id.startswith("sym:USB_HOST_GND#")
    ]
    nearest_ground = min(
        local_ground_pins,
        key=lambda point: abs(point.x - host_ground_pin.x) + abs(point.y - host_ground_pin.y),
    )
    assert nearest_ground.x == pytest.approx(host_ground_pin.x)
    assert nearest_ground.y > host_ground_pin.y

    for local_ref in ("C301.C",):
        component = schematic["instances"][module_ref + "." + local_ref]
        component_position = positions["comp:" + local_ref]
        branch_pin = pin_positions(component, component_position, "1")[0]
        return_pin = pin_positions(component, component_position, "2")[0]
        local_ground = min(
            local_ground_pins,
            key=lambda point: abs(point.x - return_pin.x) + abs(point.y - return_pin.y),
        )
        assert local_ground.y == pytest.approx(return_pin.y + 40)
        assert abs(local_ground.x - return_pin.x) == pytest.approx(40.0)
        assert (local_ground.x - return_pin.x) * (return_pin.x - branch_pin.x) > 0
        local_ground_position = min(
            (
                position
                for symbol_id, position in positions.items()
                if symbol_id.startswith("sym:USB_HOST_GND#")
            ),
            key=lambda position: (
                abs(net_symbol_pin_position(host_ground_net, position).x - local_ground.x)
                + abs(net_symbol_pin_position(host_ground_net, position).y - local_ground.y)
            ),
        )
        assert local_ground_position.rotation == 0.0

    r301 = schematic["instances"][module_ref + ".R301.R"]
    r302 = schematic["instances"][module_ref + ".R302.R"]
    r301_return = pin_positions(r301, positions["comp:R301.R"], "2")[0]
    r302_return = pin_positions(r302, positions["comp:R302.R"], "2")[0]
    assert r301_return.y < r302_return.y
    assert r301_return.x == pytest.approx(r302_return.x)
    shared_ground = [
        point
        for point in local_ground_pins
        if point.x == pytest.approx(max(r301_return.x, r302_return.x) + 40.0)
        and point.y == pytest.approx(max(r301_return.y, r302_return.y) + _PIN_EXIT_STUB)
    ]
    assert len(shared_ground) == 1

    isolator_instance = schematic["instances"][module_ref + ".U301.ADUM3160BRWZ_RL"]
    alignments = (
        ("R301.R", "1", host_instance, host, "CC1"),
        ("R302.R", "1", host_instance, host, "CC2"),
        ("C301.C", "1", isolator_instance, isolator, "SPU"),
        ("C302.C", "1", isolator_instance, isolator, "SPD"),
        ("R303.R", "1", host_instance, host, "D+_A6"),
        ("R304.R", "1", host_instance, host, "D-_A7"),
        ("R305.R", "2", schematic["instances"][module_ref + ".J302.MOLEX_68784_PIGTAIL"],
         device, "Pin_3"),
        ("R306.R", "2", schematic["instances"][module_ref + ".J302.MOLEX_68784_PIGTAIL"],
         device, "Pin_2"),
    )
    for passive_ref, passive_terminal, owner, owner_position, owner_terminal in alignments:
        passive = schematic["instances"][module_ref + "." + passive_ref]
        passive_pin = pin_positions(
            passive,
            positions["comp:" + passive_ref],
            passive_terminal,
        )[0]
        owner_pin = pin_positions(owner, owner_position, owner_terminal)[0]
        assert passive_pin.y == pytest.approx(owner_pin.y)

    proposed = result.plan.apply_to_schematic(
        with_signal_termination_symbols(schematic, result.plan),
    )
    presented = clean_schematic_labels(proposed)
    assert _position_geometry(presented, module_ref) == _position_geometry(proposed, module_ref)
    usb_overlaps = [
        finding
        for finding in component_body_overlap_findings(proposed)
        if all(subject.startswith(module_ref + ".") for subject in finding.symbol_ids)
    ]
    usb_geometry = [
        finding
        for finding in schematic_quality_findings(proposed)
        if finding.module_ref == module_ref
    ]
    assert usb_overlaps == []
    assert {finding.code for finding in usb_geometry} <= {"local-passive-owner-gap"}
    # Wider bypass placement is intentional, but it is still a local branch
    # on the local bank's bottom row, with bounded reach and a cleared return.
    for ref, terminal in (("C301.C", "SPU"), ("C302.C", "SPD")):
        capacitor = schematic["instances"][module_ref + "." + ref]
        supply_pin = pin_positions(capacitor, positions["comp:" + ref], "1")[0]
        ic_pin = pin_positions(isolator_instance, isolator, terminal)[0]
        assert abs(supply_pin.x - ic_pin.x) == pytest.approx(200)
    for symbol_id, position in positions.items():
        if not symbol_id.startswith("sym:USB_HOST_GND#"):
            continue
        ground = placed_symbol_body_bounds({"attributes": host_ground_net["properties"]}, position)
        for ref in ("R301.R", "R302.R", "R303.R", "R304.R"):
            body = placed_symbol_body_bounds(schematic["instances"][module_ref + "." + ref],
                                             positions["comp:" + ref])
            assert not (ground.min_x < body.max_x and body.min_x < ground.max_x
                        and ground.min_y < body.max_y and body.min_y < ground.max_y)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.e2e
def test_spdif_interface_is_composed_from_complete_seed_independent_blocks() -> None:
    if not DEFAULT_PCB_COMPILER.is_file() or not SPDIF_INTERFACE.is_file():
        pytest.skip("local interface compiler fixture is unavailable")

    schematic = evaluate_zener(SAMPLE_BOARD, DEFAULT_PCB_COMPILER)
    module_ref = schematic["root_ref"] + ".SPDIF"
    seeded_positions = {
        "comp:" + instance_ref.removeprefix(module_ref + "."): Position(0, 0)
        for instance_ref, instance in schematic["instances"].items()
        if instance_ref.startswith(module_ref + ".")
        and instance.get("kind") == "Component"
        and instance.get("reference_designator")
    }
    collapsed_id = "comp:U204.74HC14D_653"
    seeded_positions.pop(collapsed_id)
    seeded_positions.update({f"{collapsed_id}@{index}": Position(0, 0) for index in range(3)})
    seed_plan = LayoutPlan((ModuleLayout(module_ref, SPDIF_INTERFACE, seeded_positions),))
    package_projection = collapse_multi_unit_ic_packages(
        schematic,
        seed_plan,
        SAMPLE_BOARD,
    )
    active_projection = project_connector_like_active_blocks(
        schematic,
        package_projection.plan,
        SAMPLE_BOARD,
    )
    presented = schematic_with_symbol_overrides(
        schematic,
        SAMPLE_BOARD,
        {
            **package_projection.file_overrides,
            **active_projection.file_overrides,
        },
    )
    empty = LayoutPlan((ModuleLayout(module_ref, SPDIF_INTERFACE, {}),))
    arbitrary = LayoutPlan(
        (
            ModuleLayout(
                module_ref,
                SPDIF_INTERFACE,
                {
                    "comp:U201.PLR237_T10BK": Position(-8000, 6000, 90),
                    "comp:U204.74HC14D_653": Position(9000, -7000, 270),
                    "comp:T201.DA101C": Position(-5000, -5000, 180),
                    "comp:J201.CAX_AV_104A_R": Position(7000, 8000, 90),
                },
            ),
        )
    )

    generated = generate_functional_ic_blocks(presented, empty)
    shifted = generate_functional_ic_blocks(presented, arbitrary)

    assert generated == shifted
    assert len(generated.module_blocks) == 1
    _, block_plan = generated.module_blocks[0]
    assert block_plan.root.block_id == "multi-active-interface"
    assert block_plan.findings() == ()
    assert set(block_plan.block_bounds()) == {
        "multi-active-interface",
        "multi-active-interface/endpoint-channel-bank",
        "multi-active-interface/endpoint-channel-bank/endpoint-channel-0",
        "multi-active-interface/endpoint-channel-bank/endpoint-channel-1",
        "multi-active-interface/endpoint-channel-bank/endpoint-channel-2",
        "multi-active-interface/parallel-transformer-chain",
    }

    positions = generated.plan.modules[0].positions
    for series_ref, active_ref in (
        ("R201.R", "U201.PLR237_T10BK"),
        ("R202.R", "U202.PLT237_T10WH"),
        ("R203.R", "U203.PLT237_T10WH"),
    ):
        assert positions["comp:" + series_ref].x < positions["comp:" + active_ref].x
    assert positions["comp:U204.74HC14D_653"].x < positions["comp:R204.R"].x
    assert positions["comp:R204.R"].x < positions["comp:T201.DA101C"].x
    assert positions["comp:T201.DA101C"].x < positions["comp:C205.C"].x
    assert positions["comp:C205.C"].x < positions["comp:J201.CAX_AV_104A_R"].x
    assert sum(symbol_id.startswith("sym:SPDIF_TX_COAX#") for symbol_id in positions) == 1

    driver = presented["instances"][module_ref + ".U204.74HC14D_653"]
    for passive_ref, passive_terminal, driver_terminal in (
        ("R204.R", "1", "2"),
        ("R205.R", "1", "4"),
        ("R206.R", "1", "6"),
    ):
        passive = presented["instances"][module_ref + "." + passive_ref]
        passive_pin = pin_positions(
            passive,
            positions["comp:" + passive_ref],
            passive_terminal,
        )[0]
        driver_pin = pin_positions(
            driver,
            positions["comp:U204.74HC14D_653"],
            driver_terminal,
        )[0]
        assert passive_pin.y == pytest.approx(driver_pin.y)

    proposed = generated.plan.apply_to_schematic(presented)
    assert component_body_overlap_findings(proposed) == ()
    assert schematic_quality_findings(proposed) == ()
    assert connectivity_digest(proposed) == connectivity_digest(schematic)
