import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from schemer.block_generation import generate_functional_ic_blocks
from schemer.hints import PREFIX, parse_hints, replace_hint_preamble
from schemer.layout import LayoutPlan, ModuleLayout, Position, replace_position_block
from schemer.quality import component_body_overlap_findings
from schemer.symbol_geometry import net_symbol_pin_position, pin_outward_side, pin_positions
from schemer.toolchain import (
    DEFAULT_PCB_COMPILER,
    ToolchainError,
    connectivity_digest,
    evaluate_zener,
)

FIXTURE = Path(__file__).parent / "fixtures/interface_blocks/parallel_transformer"


@pytest.mark.parametrize(
    "rotation,side", [(0, "left"), (90, "top"), (180, "right"), (270, "bottom")]
)
def test_corner_pin_exit_uses_stroke_direction_not_nearest_box_edge(rotation, side):
    # Both left and bottom are bounding-box edges at this pin. Its stroke,
    # not the alphabetical ordering of an edge-distance tie, chooses the exit.
    instance = {
        "attributes": {
            "__symbol_value": {
                "String": """
        (symbol "CORNER" (pin passive line (at -5 -5 0) (length 2)
          (name "RETURN") (number "2")))
    """
            }
        }
    }
    assert pin_outward_side(instance, Position(0, 0, rotation), "RETURN") == side


def _record(kind, endpoints, *, id=None):
    return (
        PREFIX
        + json.dumps(
            {
                "version": 1,
                "id": id or kind,
                "kind": kind,
                "endpoints": [{"component": component, "pin": pin} for component, pin in endpoints],
                "reason": "Keep the return visibly part of its local branch.",
            }
        )
        + "\n"
    )


HINTS = _record("local-return", [("TRANSFORMER", "SEC_RET"), ("OUTPUT", "RETURN")]) + _record(
    "pin-exit", [("TRANSFORMER", "PRI_RET")]
)


def test_position_roundtrip_preserves_hint_preamble_and_electrical_source():
    source = "SIGNAL = Net()\n\n" + HINTS
    rendered = replace_position_block(source, {"comp:PART": Position(10, 20)})
    assert rendered.startswith(source)
    assert parse_hints(rendered) == parse_hints(source)
    assert replace_position_block(rendered, {"comp:PART": Position(10, 20)}) == rendered


def test_installing_hints_preserves_positions_and_is_idempotent():
    source = "SIGNAL = Net()\n\n# pcb:sch PART x=1.0000 y=2.0000 rot=0\n"
    installed = replace_hint_preamble(source, HINTS)
    assert parse_hints(installed) == parse_hints(HINTS)
    assert installed.endswith("# pcb:sch PART x=1.0000 y=2.0000 rot=0\n")
    assert replace_hint_preamble(installed, HINTS) == installed
    assert replace_hint_preamble(installed, "") == source
    with pytest.raises(ToolchainError, match="comments only"):
        replace_hint_preamble(source, "OTHER = Net()\n" + HINTS)


@pytest.mark.parametrize(
    "change",
    [
        {"x": 100},
        {"kind": "made-up"},
        {"version": True},
        {"endpoints": [{"component": "OUTPUT", "pin": "RETURN", "offset": 12}]},
    ],
)
def test_unknown_or_geometric_hint_fields_are_rejected(change):
    data = json.loads(HINTS.splitlines()[0][len(PREFIX) :])
    data.update(change)
    with pytest.raises(ToolchainError, match="invalid layout hint"):
        parse_hints(PREFIX + json.dumps(data))


def test_duplicate_requests_and_duplicate_ids_are_rejected():
    for content in (
        HINTS + HINTS,
        HINTS + _record("pin-exit", [("TRANSFORMER", "PRI_RET")], id="different-id"),
    ):
        with pytest.raises(ToolchainError, match="duplicate"):
            parse_hints(content)


@pytest.mark.e2e
def test_comment_hints_drive_generic_geometry_and_survive_compilation(tmp_path, monkeypatch):
    if not DEFAULT_PCB_COMPILER.is_file():
        pytest.skip("local Zener compiler unavailable")
    shutil.copytree(FIXTURE, tmp_path / "fixture", ignore=shutil.ignore_patterns(".pcb"))
    source = tmp_path / "fixture/ParallelTransformer.zen"
    schematic = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    root = schematic["root_ref"]
    module = ModuleLayout(root, source, {})
    plan = LayoutPlan((module,))
    baseline = generate_functional_ic_blocks(schematic, plan)
    source.write_text(source.read_text() + "\n" + HINTS)
    with monkeypatch.context() as pending_review:
        pending_review.setattr("schemer.hints.APPROVED_KINDS", frozenset())
        with pytest.raises(ToolchainError, match="await user review"):
            generate_functional_ic_blocks(schematic, plan)
        experimental = generate_functional_ic_blocks(schematic, plan, allow_experimental_hints=True)

    result = generate_functional_ic_blocks(schematic, plan)
    assert result == experimental
    seeded = replace(
        plan,
        modules=(replace(module, positions={"comp:TRANSFORMER": Position(99999, -77777, 180)}),),
    )
    assert result == generate_functional_ic_blocks(schematic, seeded, allow_experimental_hints=True)
    assert len(result.applied_hints) == 2
    positions = result.plan.modules[0].positions
    assert set(positions) == set(baseline.plan.modules[0].positions)
    for name in ("OUTPUT", "C_COUPLING.C"):
        assert positions["comp:" + name].x < baseline.plan.modules[0].positions["comp:" + name].x
    transformer = schematic["instances"][root + ".TRANSFORMER"]
    return_pin = pin_positions(transformer, positions["comp:TRANSFORMER"], "PRI_RET")[0]
    ground = schematic["nets"]["GND"]
    ground_points = [
        net_symbol_pin_position(ground, pos)
        for key, pos in positions.items()
        if key.startswith("sym:GND#")
    ]
    assert any(
        p.y == pytest.approx(return_pin.y + 40) and p.x < return_pin.x for p in ground_points
    )
    proposed = result.plan.apply_to_schematic(schematic)
    assert connectivity_digest(proposed) == connectivity_digest(schematic)
    assert not component_body_overlap_findings(proposed)
    source.write_text(result.plan.proposed_sources()[source])
    compiled = evaluate_zener(source, DEFAULT_PCB_COMPILER)
    assert connectivity_digest(compiled) == connectivity_digest(schematic)
    rerun = generate_functional_ic_blocks(compiled, plan, allow_experimental_hints=True)
    assert rerun.plan.modules[0].positions == positions
    assert parse_hints(source.read_text()) == parse_hints(HINTS)

    # A real but unrelated return endpoint cannot silently expand the wireset.
    source.write_text(source.read_text().replace('"SEC_RET"', '"PRI_RET"'))
    with pytest.raises(ToolchainError, match="unresolved or unsupported"):
        generate_functional_ic_blocks(compiled, plan, allow_experimental_hints=True)
