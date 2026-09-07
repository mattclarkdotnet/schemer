from pathlib import Path

import pytest

from schemer.layout import (
    ModuleLayout,
    Position,
    format_position_block,
    position_block_start,
    replace_position_block,
    resolve_module_position_ids,
    source_diff,
    symbol_id_to_comment_key,
)
from schemer.toolchain import ToolchainError


def test_symbol_id_to_comment_key_matches_pcb_sch() -> None:
    assert symbol_id_to_comment_key("comp:R1.R") == "R1.R"
    assert symbol_id_to_comment_key("sym:VCC#2") == "VCC.2"
    with pytest.raises(ToolchainError):
        symbol_id_to_comment_key("R1")


def test_format_position_block_uses_natural_order() -> None:
    positions = {
        "comp:R10.R": Position(10, 20),
        "comp:R2.R": Position(30, 40, rotation=90),
        "sym:VCC#0": Position(50, 60, mirror="x"),
    }

    assert format_position_block(positions) == (
        "# pcb:sch R2.R x=30.0000 y=40.0000 rot=90\n"
        "# pcb:sch R10.R x=10.0000 y=20.0000 rot=0\n"
        "# pcb:sch VCC.0 x=50.0000 y=60.0000 rot=0 mirror=x\n"
    )


def test_replace_position_block_preserves_ordinary_source() -> None:
    source = (
        'load("thing.zen", "Thing")\nThing(name="R1")\n\n# pcb:sch OLD x=1.0000 y=2.0000 rot=0\n'
    )

    updated = replace_position_block(source, {"comp:R1": Position(12.7, -25.4)})

    assert updated == (
        'load("thing.zen", "Thing")\nThing(name="R1")\n\n# pcb:sch R1 x=12.7000 y=-25.4000 rot=0\n'
    )
    assert position_block_start(updated) == len('load("thing.zen", "Thing")\nThing(name="R1")\n')


def test_source_diff_is_empty_only_for_identical_text() -> None:
    path = Path("Example.zen")
    assert source_diff(path, "x\n", "x\n") == ""
    assert "-x" in source_diff(path, "x\n", "y\n")
    assert "+y" in source_diff(path, "x\n", "y\n")


def test_resolve_module_position_ids_scopes_internal_nets_only() -> None:
    root_ref = "/project/Board.zen:<root>"
    module = ModuleLayout(
        instance_ref=root_ref + ".CHILD",
        source_path=Path("Child.zen"),
        positions={
            "comp:R1.R": Position(1, 2),
            "sym:GND#0": Position(3, 4),
            "sym:LOCAL_RAIL#1": Position(5, 6),
        },
    )
    schematic = {
        "root_ref": root_ref,
        "nets": {
            "GND": {"name": "GND"},
            "CHILD.LOCAL_RAIL": {"name": "CHILD.LOCAL_RAIL"},
        },
    }

    assert resolve_module_position_ids(module, schematic) == {
        "comp:R1.R": Position(1, 2),
        "sym:GND#0": Position(3, 4),
        "sym:CHILD.LOCAL_RAIL#1": Position(5, 6),
    }
