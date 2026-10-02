from __future__ import annotations

import pytest

from schemer.analysis.circuits import (
    component_layout_groups,
)
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
)
from schemer.kicad.geometry.library import (
    placed_pin_positions,
)
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.reuse import reuse_repeated_block_geometry
from tests.support.schematic import SCHEMATIC, _support_fixture


def test_repeated_local_geometry_changes_positions_without_changing_representation():
    from copy import deepcopy
    from uuid import uuid4

    from schemer.analysis.inventory import structural_inventory
    from tests.support.repeated import fixture

    source, _ = fixture()
    source["instances"]["root.NET"]["attributes"] = {}
    seed = FileSchematic.from_text(SCHEMATIC)
    text = seed.get_as_string()
    raw = seed.document.symbols[0].expression
    template = text[raw.start:raw.end]
    seed.remove_items(list(seed.get_items()))
    text = seed.get_as_string()
    fragments = []
    refs = [ref for ref, value in source["instances"].items()
            if value.get("reference_designator")]
    for index, ref in enumerate(refs):
        part = source["instances"][ref]
        fragment = template.replace("11111111-1111-1111-1111-111111111111", str(uuid4()))
        fragment = fragment.replace('"FILTER.R_INPUT.R"', '"' + ref.removeprefix("root.") + '"')
        fragment = fragment.replace('"R1"', '"' + part["reference_designator"] + '"')
        fragment = fragment.replace("(at 20.32 30.48 90)",
                                    f"(at {20+index*index*40} {20+index*30} 90)")
        fragments.append(fragment)
    editor = FileSchematic.from_text(text[:text.rfind(")")] + "\n".join(fragments) + ")")
    before = deepcopy(source)
    groups, connectors = component_layout_groups(source)
    inventory = structural_inventory(source)
    poses_before = [(s.position.x, s.position.y) for s in editor.get_symbols()]
    reuse_repeated_block_geometry(source, editor, groups, inventory)
    assert source == before
    assert component_layout_groups(source) == (groups, connectors)
    assert [(s.position.x, s.position.y) for s in editor.get_symbols()] != poses_before
    symbols = {s.reference: s for s in editor.document.symbols}
    a, b, c, d = [placed_pin_positions(editor.document, symbols[name])
                  for name in ("U98", "R9", "U2", "R42")]
    assert (b["2"].x-a["1"].x, b["2"].y-a["1"].y) == (
        d["2"].x-c["1"].x, d["2"].y-c["1"].y)


@pytest.mark.parametrize("pitch", [1.27, 2.54])
def test_connected_composition_preserves_clear_parallel_rows_but_separates_collisions(pitch):
    from schemer.kicad.items import place_symbol
    from schemer.native.reuse import compact_connected_circuits

    schematic, editor = _support_fixture("shunt")
    editor = FileSchematic.from_text(editor.get_as_string().replace(
        '(start -1.27 -1.27) (end 1.27 1.27)',
        '(start -1.27 -0.762) (end 1.27 0.762)'))
    a, b = editor.get_symbols()
    place_symbol(a, Vector2.from_xy_mm(20.32, 30.48), 0)
    place_symbol(b, Vector2.from_xy_mm(20.32, 30.48 + pitch), 0)
    editor.update_items([a, b])
    groups, _ = component_layout_groups(schematic)
    compact_connected_circuits(schematic, editor, groups, frozenset(groups.values()))
    a, b = editor.get_symbols()
    if pitch == 2.54:
        assert b.position.x == a.position.x
        assert b.position.y - a.position.y == 2_540_000
    else:
        assert (b.position.x != a.position.x
                or abs(b.position.y - a.position.y) >= 2_159_000)


@pytest.mark.parametrize("axis", ["x", "y"])
def test_empty_slab_compaction_preserves_overlapping_spans(axis):
    from schemer.native.reuse import _compact_axis_offsets

    boxes = {"a": Envelope(0, 0, 10, 10), "b": Envelope(5, 5, 30, 30),
             "c": Envelope(100, 100, 110, 110), "d": Envelope(150, 150, 160, 160)}
    offsets = _compact_axis_offsets(boxes, axis, 5)
    assert offsets == {"a": 0, "b": 0, "c": -65, "d": -100}
