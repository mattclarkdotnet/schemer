from pathlib import Path

import pytest

from schemer.blocks import (
    BlockItem,
    BlockLink,
    BlockPlan,
    BlockPort,
    BlockRegion,
    BlockSide,
    LayoutBlock,
    PlacedBlock,
    PortRef,
    Rect,
    RegionKind,
    block_from_positions,
    compose_column,
    compose_linked_row,
    compose_row,
    place_boundary_annotations,
)
from schemer.layout import Position
from schemer.toolchain import ToolchainError


def _leaf(block_id: str, symbol_id: str, *, width: float, height: float) -> LayoutBlock:
    return LayoutBlock(
        block_id,
        width,
        height,
        items=(
            BlockItem(
                symbol_id,
                Position(10, 20),
                Rect(10, 10, width - 20, height - 20),
            ),
        ),
    )


def test_row_composition_flattens_nested_local_coordinates_deterministically() -> None:
    connector = _leaf("connector", "comp:J1", width=100, height=80)
    controller = _leaf("controller", "comp:U1", width=160, height=120)
    sheet = compose_row("sheet", (connector, controller), gap=40, padding=20)
    plan = BlockPlan(sheet, origin_x=1000, origin_y=500)

    assert plan.positions() == {
        "comp:J1": Position(1030, 560),
        "comp:U1": Position(1170, 540),
    }
    assert plan.block_bounds() == {
        "sheet": Rect(1000, 500, 340, 160),
        "sheet/connector": Rect(1020, 540, 100, 80),
        "sheet/controller": Rect(1160, 520, 160, 120),
    }
    assert (
        plan.to_layout_plan("fixture:<root>", Path("Fixture.zen")).modules[0].positions
        == plan.positions()
    )


def test_column_composition_uses_declared_gap_and_alignment() -> None:
    first = _leaf("first", "comp:A", width=60, height=40)
    second = _leaf("second", "comp:B", width=100, height=80)

    column = compose_column("power-branch", (first, second), gap=30, padding=10, alignment="end")

    assert column.width == 120
    assert column.height == 170
    assert column.children == (
        PlacedBlock(first, 50, 10),
        PlacedBlock(second, 10, 80),
    )
    assert BlockPlan(column).findings() == ()


def test_leaf_content_bounds_reserve_labels_without_changing_body_collision_geometry() -> None:
    leaf = block_from_positions(
        "annotated", {"comp:R": Position(20, 30)},
        occupied={"comp:R": Rect(10, 20, 20, 40)},
        content_bounds=Rect(-100, -50, 300, 200), padding=0,
    )
    assert (leaf.width, leaf.height) == (300, 200)
    assert leaf.items[0].position == Position(120, 80)
    assert leaf.items[0].occupied == Rect(110, 70, 20, 40)
    assert BlockPlan(leaf).findings() == ()


def test_a_symbol_can_only_be_owned_by_one_block() -> None:
    first = _leaf("first", "comp:U1", width=100, height=100)
    second = _leaf("second", "comp:U1", width=100, height=100)
    sheet = compose_row("sheet", (first, second), gap=100)

    findings = BlockPlan(sheet).findings()

    assert [finding.code for finding in findings] == ["duplicate-symbol-owner"]
    with pytest.raises(ToolchainError, match="duplicate-symbol-owner"):
        BlockPlan(sheet).positions()


def test_sibling_blocks_must_not_overlap_or_violate_spacing() -> None:
    first = _leaf("first", "comp:A", width=100, height=100)
    second = _leaf("second", "comp:B", width=100, height=100)
    overlapping = LayoutBlock(
        "overlapping",
        260,
        120,
        children=(PlacedBlock(first, 0, 0), PlacedBlock(second, 80, 0)),
        minimum_child_spacing=40,
    )
    too_close = LayoutBlock(
        "too-close",
        260,
        120,
        children=(PlacedBlock(first, 0, 0), PlacedBlock(second, 120, 0)),
        minimum_child_spacing=40,
    )

    assert "child-block-overlap" in {finding.code for finding in BlockPlan(overlapping).findings()}
    assert "child-block-spacing" in {finding.code for finding in BlockPlan(too_close).findings()}


def test_leaf_block_normalizes_legacy_positions_and_rejects_body_overlap() -> None:
    leaf = block_from_positions(
        "legacy-chain",
        {
            "comp:R1": Position(100, 200, rotation=90),
            "sym:OUTPUT#0": Position(260, 200),
        },
        occupied={"comp:R1": Rect(90, 180, 80, 40)},
        padding=20,
    )

    assert BlockPlan(leaf, origin_x=70, origin_y=160).positions() == {
        "comp:R1": Position(100, 200, rotation=90),
        "sym:OUTPUT#0": Position(260, 200),
    }
    assert leaf.width == 210
    assert leaf.height == 80

    overlapping = LayoutBlock(
        "overlap",
        200,
        100,
        items=(
            BlockItem("comp:A", Position(20, 20), Rect(20, 20, 80, 40)),
            BlockItem("comp:B", Position(60, 20), Rect(60, 20, 80, 40)),
        ),
    )
    assert [finding.code for finding in BlockPlan(overlapping).findings()] == ["item-body-overlap"]


def test_boundary_annotations_do_not_preserve_legacy_whitespace() -> None:
    positions = {
        "comp:U1": Position(100, 100),
        "sym:VCC#0": Position(-5000, -5000),
        "sym:GND#0": Position(5000, 5000),
    }
    occupied = {"comp:U1": Rect(50, 50, 100, 100)}

    placed = place_boundary_annotations(
        positions,
        occupied,
        {
            "sym:VCC#0": BlockSide.TOP,
            "sym:GND#0": BlockSide.BOTTOM,
        },
    )
    block = block_from_positions("device", placed, occupied=occupied, padding=40)

    assert placed["sym:VCC#0"] == Position(100, -30)
    assert placed["sym:GND#0"] == Position(100, 230)
    assert block.width == 180
    assert block.height == 340


def test_boundary_annotation_order_follows_target_pin_order() -> None:
    positions = {
        "comp:U1": Position(100, 100),
        "sym:LEFT_SUPPLY#0": Position(5000, -5000),
        "sym:RIGHT_SUPPLY#0": Position(-5000, -5000),
    }
    occupied = {"comp:U1": Rect(50, 50, 100, 100)}

    placed = place_boundary_annotations(
        positions,
        occupied,
        {
            "sym:LEFT_SUPPLY#0": BlockSide.TOP,
            "sym:RIGHT_SUPPLY#0": BlockSide.TOP,
        },
        along_order={
            "sym:LEFT_SUPPLY#0": 20,
            "sym:RIGHT_SUPPLY#0": 80,
        },
    )

    assert placed["sym:LEFT_SUPPLY#0"].x < placed["sym:RIGHT_SUPPLY#0"].x


def test_ports_are_constrained_to_their_declared_boundary_side() -> None:
    valid = LayoutBlock(
        "valid",
        200,
        100,
        ports=(
            BlockPort("input", BlockSide.LEFT, 25, "IN"),
            BlockPort("output", BlockSide.RIGHT, 75, "OUT"),
            BlockPort("supply", BlockSide.TOP, 100, "VCC"),
            BlockPort("return", BlockSide.BOTTOM, 100, "GND"),
        ),
    )
    invalid = LayoutBlock(
        "invalid",
        200,
        100,
        ports=(BlockPort("input", BlockSide.LEFT, 101, "IN"),),
    )

    assert BlockPlan(valid).findings() == ()
    assert [finding.code for finding in BlockPlan(invalid).findings()] == ["port-off-boundary"]


def test_parent_links_resolve_direct_child_ports_and_require_matching_nets() -> None:
    source = LayoutBlock(
        "source",
        100,
        80,
        ports=(BlockPort("output", BlockSide.RIGHT, 40, "SIGNAL"),),
    )
    sink = LayoutBlock(
        "sink",
        100,
        80,
        ports=(BlockPort("input", BlockSide.LEFT, 40, "SIGNAL"),),
    )
    valid = LayoutBlock(
        "valid-sheet",
        300,
        80,
        children=(PlacedBlock(source, 0, 0), PlacedBlock(sink, 200, 0)),
        links=(
            BlockLink(
                "signal",
                (PortRef("source", "output"), PortRef("sink", "input")),
                "SIGNAL",
            ),
        ),
    )
    invalid = LayoutBlock(
        "invalid-sheet",
        300,
        80,
        children=(PlacedBlock(source, 0, 0), PlacedBlock(sink, 200, 0)),
        links=(
            BlockLink(
                "wrong-net",
                (PortRef("source", "output"), PortRef("sink", "missing")),
                "OTHER",
            ),
        ),
    )

    assert BlockPlan(valid).findings() == ()
    assert {finding.code for finding in BlockPlan(invalid).findings()} == {
        "link-net-mismatch",
        "unknown-link-port",
    }


def test_linked_row_aligns_connected_boundary_ports() -> None:
    source = LayoutBlock(
        "source",
        100,
        100,
        ports=(BlockPort("output", BlockSide.RIGHT, 20, "SIGNAL"),),
    )
    sink = LayoutBlock(
        "sink",
        100,
        100,
        ports=(BlockPort("input", BlockSide.LEFT, 70, "SIGNAL"),),
    )
    link = BlockLink(
        "signal",
        (PortRef("source", "output"), PortRef("sink", "input")),
        "SIGNAL",
    )

    row = compose_linked_row("sheet", (source, sink), (link,), gap=40, padding=10)

    source_placement, sink_placement = row.children
    assert source_placement.y + 20 == sink_placement.y + 70
    assert sink_placement.x - source_placement.bounds.right == 40
    assert BlockPlan(row).findings() == ()


def test_block_local_regions_reject_body_and_annotation_collisions() -> None:
    block = LayoutBlock(
        "signal-chain",
        300,
        160,
        items=(BlockItem("comp:R1", Position(100, 60), Rect(100, 60, 50, 30)),),
        regions=(
            BlockRegion("input-wire", RegionKind.WIRE, Rect(20, 68, 100, 10), "IN"),
            BlockRegion("input-label", RegionKind.ANNOTATION, Rect(30, 65, 60, 20)),
        ),
    )

    assert {finding.code for finding in BlockPlan(block).findings()} == {
        "region-item-overlap",
        "annotation-region-overlap",
    }


def test_same_net_wire_regions_may_join_but_different_nets_may_not_cross() -> None:
    joined = LayoutBlock(
        "junction",
        200,
        200,
        regions=(
            BlockRegion("horizontal", RegionKind.WIRE, Rect(20, 90, 160, 20), "SIGNAL"),
            BlockRegion("vertical", RegionKind.WIRE, Rect(90, 20, 20, 160), "SIGNAL"),
        ),
    )
    crossing = replace_region_net(joined, "vertical", "OTHER")

    assert BlockPlan(joined).findings() == ()
    assert [finding.code for finding in BlockPlan(crossing).findings()] == ["wire-region-crossing"]


def replace_region_net(block: LayoutBlock, region_name: str, net: str) -> LayoutBlock:
    return LayoutBlock(
        block.block_id,
        block.width,
        block.height,
        items=block.items,
        ports=block.ports,
        regions=tuple(
            BlockRegion(
                region.name,
                region.kind,
                region.bounds,
                net if region.name == region_name else region.net,
            )
            for region in block.regions
        ),
        children=block.children,
        links=block.links,
        minimum_child_spacing=block.minimum_child_spacing,
    )
