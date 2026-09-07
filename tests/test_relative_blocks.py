import json
from itertools import combinations
from pathlib import Path

import pytest

from schemer.hints import PREFIX, HintSet, parse_hints
from schemer.layout import LayoutPlan, ModuleLayout, Position
from schemer.layout_metrics import Envelope
from schemer.quality import top_level_block_envelopes, top_level_block_overlap_findings
from schemer.spacing import (
    pack_top_level_groups,
    relative_block_deltas,
    remove_redundant_root_rails,
)
from schemer.toolchain import (
    DEFAULT_PCB_COMPILER,
    ToolchainError,
    connectivity_digest,
    evaluate_zener,
)


def test_branch_peers_share_a_stage_after_measured_predecessors():
    bounds = {
        "SOURCE": Envelope(-900, -100, -800, 0),
        "CORE": Envelope(300, 400, 500, 700),
        "OUT_A": Envelope(-200, 100, 200, 600),
        "OUT_B": Envelope(0, -700, 600, -500),
    }
    relations = (("CORE", "SOURCE"), ("OUT_A", "CORE"), ("OUT_B", "CORE"))
    deltas = relative_block_deltas(bounds, relations, 100)
    placed = {name: box.translated(*deltas[name]) for name, box in bounds.items()}
    assert placed["CORE"].min_x - placed["SOURCE"].max_x == 100
    assert placed["OUT_A"].min_x - placed["CORE"].max_x == 100
    assert placed["OUT_A"].min_x == placed["OUT_B"].min_x
    assert placed["OUT_B"].min_y - placed["OUT_A"].max_y == 100
    for a, b in combinations(placed.values(), 2):
        assert a.max_x <= b.min_x or b.max_x <= a.min_x or a.max_y <= b.min_y or b.max_y <= a.min_y
    assert all(delta == (0, 0) for delta in relative_block_deltas(placed, relations, 100).values())
    moved = {name: box.translated(10000, -9000) for name, box in bounds.items()}
    moved_deltas = relative_block_deltas(moved, relations, 100)
    assert {name: box.translated(*moved_deltas[name]) for name, box in moved.items()} == placed


@pytest.mark.parametrize(
    "relations,match",
    [
        ((("B", "A"), ("A", "B")), "cycle"),
        ((("B", "MISSING"),), "unknown"),
        ((("A", "A"),), "missing"),
    ],
)
def test_bad_sheet_relations_fail_explicitly(relations, match):
    bounds = {name: Envelope(0, 0, 100, 100) for name in ("A", "B")}
    with pytest.raises(ToolchainError, match=match):
        relative_block_deltas(bounds, relations, 100)


def test_new_relation_type_needs_opt_in_and_roundtrips_without_coordinates(tmp_path):
    data = {
        "version": 1,
        "id": "flow",
        "kind": "right-of",
        "blocks": ["B", "A"],
        "reason": "Read the functional chain left to right.",
    }
    content = PREFIX + json.dumps(data) + "\n"
    source = tmp_path / "Circuit.zen"
    source.write_text(content)
    with pytest.raises(ToolchainError, match="await user review"):
        HintSet.from_source(source, allow_experimental=False)
    assert HintSet.from_source(source, allow_experimental=True).hints[0].as_dict() == data
    assert parse_hints(content)[0].blocks == ("B", "A")
    with pytest.raises(ToolchainError, match="itself"):
        parse_hints(PREFIX + json.dumps({**data, "blocks": ["A", "A"]}))


@pytest.mark.e2e
def test_relative_packing_preserves_connectivity_and_component_orientation():
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local Zener compiler unavailable")
    source = (
        Path(__file__).parent / "fixtures/generic_layouts/anchor_orientation/AnchorOrientation.zen"
    )
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    positions = {
        "comp:R1.R": Position(900, -600, 270),
        "comp:U1.U": Position(-300, 0),
        "comp:R2.R": Position(-800, 1000, 270),
    }
    plan = LayoutPlan((ModuleLayout(root, source, positions),))
    relations = (("U1", "R1"), ("R2", "U1"))
    packed = pack_top_level_groups(plan.apply_to_schematic(schematic), plan, right_of=relations)
    proposed = packed.apply_to_schematic(schematic)
    envelopes = top_level_block_envelopes(proposed)
    assert envelopes["R1"].max_x < envelopes["U1"].min_x
    assert envelopes["U1"].max_x < envelopes["R2"].min_x
    assert not top_level_block_overlap_findings(proposed)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)
    assert {key: pos.rotation for key, pos in packed.modules[0].positions.items()} == {
        key: pos.rotation for key, pos in positions.items()
    }
    assert pack_top_level_groups(proposed, packed, right_of=relations) == packed


@pytest.mark.e2e
def test_completed_child_supersedes_only_its_redundant_root_rails():
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local Zener compiler unavailable")
    source = (
        Path(__file__).parent / "fixtures/generic_layouts/anchor_orientation/AnchorOrientation.zen"
    )
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    plan = LayoutPlan(
        (
            ModuleLayout(
                root,
                source,
                {
                    "comp:R1.R": Position(-300, 0),
                    "comp:U1": Position(300, 0),
                    "comp:R2.R": Position(900, 0),
                    "sym:VDD#0": Position(300, 0),
                    "sym:VSS#0": Position(300, 100),
                    "sym:AMP_IN#0": Position(300, 0),
                },
            ),
            ModuleLayout(
                root + ".U1",
                source,
                {
                    "comp:U": Position(0, 0),
                    "sym:VDD#0": Position(0, -200),
                    "sym:AMP_IN#0": Position(-200, 0),
                },
            ),
        )
    )
    current = plan.apply_to_schematic(schematic)
    # Simulate a shared rail also terminating at a non-regenerated input block.
    current["nets"]["VDD"]["ports"].append(root + ".R1.R.P1")
    extra = dict(plan.modules[0].positions, **{"sym:VDD#1": Position(-300, 0)})
    plan = LayoutPlan((ModuleLayout(root, source, extra), plan.modules[1]))
    current = plan.apply_to_schematic(current)
    assert remove_redundant_root_rails(current, plan, set()) == plan
    cleaned = remove_redundant_root_rails(current, plan, {root + ".U1"})
    assert set(cleaned.modules[0].positions) == set(extra) - {"sym:VDD#0"}
    assert cleaned.modules[1] == plan.modules[1]
    assert connectivity_digest(cleaned.apply_to_schematic(current)) == connectivity_digest(current)
    assert (
        remove_redundant_root_rails(cleaned.apply_to_schematic(current), cleaned, {root + ".U1"})
        == cleaned
    )
