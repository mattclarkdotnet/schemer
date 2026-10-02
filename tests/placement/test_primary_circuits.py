from __future__ import annotations

from pathlib import Path

import pytest

from schemer.analysis.quality import component_body_overlap_findings
from schemer.analysis.symbol_quality import schematic_quality_findings
from schemer.analysis.topology import connectivity_digest
from schemer.analysis.visibility import clean_schematic_labels
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position
from schemer.integration.toolchain import DEFAULT_PCB_COMPILER, evaluate_zener
from schemer.placement.circuits.policy import PIN_EXIT_STUB
from schemer.placement.pipeline import generate_functional_ic_blocks
from schemer.source.signal_terminations import with_signal_termination_symbols
from schemer.symbols.geometry import (
    pin_positions,
    placed_symbol_body_bounds,
)
from schemer.symbols.net_symbols import net_symbol_pin_position
from tests.support.block_generation import (
    DIGITAL_ABX,
    SPDIF_INTERFACE,
    _position_geometry,
)


@pytest.mark.e2e
def test_digital_abx_dsp_block_is_generated_without_coordinate_seeds() -> None:
    if not DEFAULT_PCB_COMPILER.is_file() or not DIGITAL_ABX.is_file():
        pytest.skip("local DigitalAbx compiler fixture is unavailable")

    schematic = evaluate_zener(DIGITAL_ABX, DEFAULT_PCB_COMPILER)
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
    positions = block_plan.positions()
    pico = schematic["instances"][module_ref + ".A101.PICO_2"]
    feed = schematic["instances"][module_ref + ".D101.POWER_SCHOTTKY"]
    reset_switch = schematic["instances"][module_ref + ".Q101.DMG3402L-7"]
    run_pullup = schematic["instances"][module_ref + ".R101.R"]
    pico_vsys = pin_positions(pico, positions["comp:A101.PICO_2"], "VSYS")[0]
    feed_output = pin_positions(feed, positions["comp:D101.POWER_SCHOTTKY"], "K")[0]
    assert feed_output.x == pytest.approx(pico_vsys.x)
    assert feed_output.y < pico_vsys.y
    pico_run = pin_positions(pico, positions["comp:A101.PICO_2"], "RUN")[0]
    switch_drain = pin_positions(reset_switch, positions["comp:Q101.DMG3402L-7"], "D")[0]
    pullup_signal = pin_positions(run_pullup, positions["comp:R101.R"], "2")[0]
    assert switch_drain.y == pytest.approx(pico_run.y)
    assert pico_run.x - switch_drain.x == pytest.approx(180.0)
    assert pullup_signal.x == pytest.approx((switch_drain.x + pico_run.x) / 2)
    assert pullup_signal.y == pytest.approx(pico_run.y)
    proposed = generated.plan.apply_to_schematic(schematic)
    assert not component_body_overlap_findings(proposed)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.e2e
def test_primary_ic_layout_rejects_missing_authored_branch_ownership() -> None:
    if not DEFAULT_PCB_COMPILER.is_file() or not DIGITAL_ABX.is_file():
        pytest.skip("local DigitalAbx compiler fixture is unavailable")

    schematic = evaluate_zener(DIGITAL_ABX, DEFAULT_PCB_COMPILER)
    module_ref = schematic["root_ref"] + ".DSP_CORE"
    source = Path(schematic["instances"][module_ref]["type_ref"]["source_path"])
    wrapper = schematic["instances"][module_ref + ".R101"]
    wrapper["attributes"].pop("schematic_properties")

    with pytest.raises(
        ToolchainError,
        match="requires authored ownership roles.*R101",
    ):
        generate_functional_ic_blocks(
            schematic,
            LayoutPlan((ModuleLayout(module_ref, source, {}),)),
        )


@pytest.mark.e2e
def test_digital_abx_usb_block_has_local_rails_and_short_series_doglegs() -> None:
    if not DEFAULT_PCB_COMPILER.is_file() or not DIGITAL_ABX.is_file():
        pytest.skip("local DigitalAbx compiler fixture is unavailable")

    schematic = evaluate_zener(DIGITAL_ABX, DEFAULT_PCB_COMPILER)
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
    assert sum(symbol_id.startswith("sym:USB.USB_HOST_VDD#") for symbol_id in positions) == 2
    assert sum(symbol_id.startswith("sym:USB.USB_DEVICE_VDD#") for symbol_id in positions) == 2

    host_instance = schematic["instances"][module_ref + ".J301.USB4105_GF_A"]
    host_vbus_pin = pin_positions(host_instance, host, "VBUS_A4")[0]
    host_vbus_net = schematic["nets"]["USB.USB_HOST_VBUS"]
    host_vbus_positions = [
        position
        for symbol_id, position in positions.items()
        if symbol_id.startswith("sym:USB.USB_HOST_VBUS#")
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
    # Repeated same-face VBUS pins use one wireset whose supply symbol sits
    # above the complete connector rather than one short L per physical pin.
    assert host_vbus_symbol_pin.y <= host_vbus_pin.y - 40
    assert host_vbus_position.rotation == 0.0

    host_ground_pin = pin_positions(host_instance, host, "GND_A1")[0]
    host_ground_net = schematic["nets"]["USB.USB_HOST_GND"]
    local_ground_pins = [
        net_symbol_pin_position(host_ground_net, position)
        for symbol_id, position in positions.items()
        if symbol_id.startswith("sym:USB.USB_HOST_GND#")
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
                if symbol_id.startswith("sym:USB.USB_HOST_GND#")
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
    assert r301_return.y == pytest.approx(r302_return.y)
    assert abs(r301_return.x - r302_return.x) >= 100.0
    shared_ground = [
        point
        for point in local_ground_pins
        if point.x == pytest.approx(max(r301_return.x, r302_return.x) + 40.0)
        and point.y == pytest.approx(max(r301_return.y, r302_return.y) + PIN_EXIT_STUB)
    ]
    assert len(shared_ground) == 1

    isolator_instance = schematic["instances"][module_ref + ".U301.ADUM3160BRWZ_RL"]
    alignments = (
        ("C301.C", "1", isolator_instance, isolator, "SPU"),
        ("C302.C", "1", isolator_instance, isolator, "SPD"),
        ("R303.R", "1", host_instance, host, "D+_A6"),
        ("R304.R", "1", host_instance, host, "D-_A7"),
        (
            "R305.R",
            "2",
            schematic["instances"][module_ref + ".J302.MOLEX_68784_PIGTAIL"],
            device,
            "Pin_3",
        ),
        (
            "R306.R",
            "2",
            schematic["instances"][module_ref + ".J302.MOLEX_68784_PIGTAIL"],
            device,
            "Pin_2",
        ),
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
        if not symbol_id.startswith("sym:USB.USB_HOST_GND#"):
            continue
        ground = placed_symbol_body_bounds({"attributes": host_ground_net["properties"]}, position)
        for ref in ("R301.R", "R302.R", "R303.R", "R304.R"):
            body = placed_symbol_body_bounds(
                schematic["instances"][module_ref + "." + ref], positions["comp:" + ref]
            )
            assert not (
                ground.min_x < body.max_x
                and body.min_x < ground.max_x
                and ground.min_y < body.max_y
                and body.min_y < ground.max_y
            )
    assert connectivity_digest(proposed) == connectivity_digest(schematic)


@pytest.mark.e2e
def test_spdif_interface_is_composed_from_complete_seed_independent_blocks() -> None:
    if not DEFAULT_PCB_COMPILER.is_file() or not SPDIF_INTERFACE.is_file():
        pytest.skip("local interface compiler fixture is unavailable")

    schematic = evaluate_zener(DIGITAL_ABX, DEFAULT_PCB_COMPILER)
    module_ref = schematic["root_ref"] + ".SPDIF"
    presented = schematic
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
    assert connectivity_digest(proposed) == connectivity_digest(schematic)
