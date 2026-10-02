from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_positions,
)
from schemer.kicad.items import (
    Vector2,
)
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("shunt_count", [1, 2, 3])
def test_cascaded_owned_channels_are_composed_together_independent_of_seed(shunt_count):
    from copy import deepcopy
    from uuid import uuid4

    from schemer.native.placement.chains import compose_owned_series_trees

    schematic, editor = _support_fixture("series")
    definition = '''(symbol "Test:TwoInputs" (symbol "TwoInputs_1_1"
      (rectangle (start -1.27 1.27) (end 1.27 -5.08)
        (stroke (width 0) (type default)) (fill (type background)))
      (pin input line (at -2.54 0 0) (length 1.27) (name "SIG") (number "2"))
      (pin input line (at -2.54 -2.54 0) (length 1.27) (name "OTHER") (number "3"))
      (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND") (number "1"))))'''
    text = editor.get_as_string().replace("(lib_symbols", "(lib_symbols " + definition, 1)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Test:TwoInputs")', 1)
    text = text.replace('(property "Path" "OWNER"',
        f'(pin "3" (uuid "{uuid4()}")) (property "Path" "OWNER"', 1)
    editor = FileSchematic.from_text(text)
    owner = next(s for s in editor.get_symbols() if s.reference == "R1")
    owner.transform.orientation = 0
    editor.update_items(owner)
    schematic["instances"]["root.OWNER"]["attributes"]["__symbol_value"]["String"] = (
        '(symbol "P" (pin (name "SIG") (number "2"))'
        '(pin (name "OTHER") (number "3")) (pin (name "GND") (number "1")))')
    template = editor.document.symbols[1].expression
    text = editor.get_as_string()
    fragment = text[template.start:template.end]
    base_instance = deepcopy(schematic["instances"].pop("root.BIAS"))
    editor.remove_items([s for s in editor.get_symbols() if s.reference == "R2"])
    schematic["nets"] = {"gnd": {"name": "GND", "kind": "Ground", "ports": ["root.OWNER.GND"]}}
    additions, channels = [], []
    for channel, owner_pin in enumerate(("SIG", "OTHER")):
        refs = [f"P{channel}_{i}" for i in range(4 + shunt_count)]
        series, output_shunt, coupling, upstream, *shunts = refs
        specs = [(series, "series", "R1", owner_pin),
                 (output_shunt, "shunt", "R1", owner_pin),
                 (coupling, "ac-coupling", series, "2"),
                 (upstream, "series", coupling, "2"),
                 *((r, "shunt", coupling, "2") for r in shunts)]
        for ref, role, parent, pin in specs:
            additions.append(fragment.replace('"R2"', f'"{ref}"').replace('"BIAS"', f'"{ref}"')
                             .replace(editor.document.symbols[0].uuid, str(uuid4()))
                             .replace("aaaaaaaa-", str(uuid4())[:8] + "-"))
            item = deepcopy(base_instance)
            item["reference_designator"] = ref
            item["attributes"]["__symbol_value"] = {"String":
                '(symbol "P" (pin (name "1") (number "1")) (pin (name "2") (number "2")))'}
            item["attributes"]["schematic_properties"]["Json"] = {
                "role": role, "owner": parent, "pin": pin, "group": f"channel-{channel}"}
            schematic["instances"]["root." + ref] = item
        for name, ports in (
            ("input", [f"OWNER.{owner_pin}", f"{series}.1", f"{output_shunt}.1"]),
            ("coupled", [f"{series}.2", f"{coupling}.1"]),
            ("divided", [f"{coupling}.2", f"{upstream}.1", *(f"{r}.1" for r in shunts)]),
            ("external", [f"{upstream}.2"]),
        ):
            schematic["nets"][f"{channel}-{name}"] = {
                "name": f"{channel}-{name}", "ports": ["root." + p for p in ports]}
        schematic["nets"]["gnd"]["ports"].extend(
            "root." + r + ".2" for r in [output_shunt, *shunts])
        channels.append(refs)
    text = editor.get_as_string()
    editor = FileSchematic.from_text(text[:text.rfind(")")] + "\n".join(additions) + ")")
    for index, symbol in enumerate(editor.get_symbols()):
        if symbol.reference != "R1":
            symbol.position = Vector2.from_xy_mm(100 + index * 17, 150 - index * 3)
            editor.update_items(symbol)
    expected = set().union(*map(set, channels))
    assert compose_owned_series_trees(schematic, editor) == expected
    pins = {s.reference: placed_pin_positions(editor.document, s) for s in editor.document.symbols}
    for refs in channels:
        series, _, coupling, upstream, *_ = refs
        assert pins[series]["1"].y == pins[coupling]["1"].y == pins[upstream]["1"].y
    offsets = {(pins[b]["1"].x - pins[a]["1"].x, pins[b]["1"].y - pins[a]["1"].y)
               for a, b in zip(*channels, strict=True)}
    assert len(offsets) == 1
    dx, dy = offsets.pop()
    assert dx == 0 and dy > 15_000_000
    before = editor.get_as_string()
    assert compose_owned_series_trees(schematic, editor) == expected
    assert editor.get_as_string() == before
