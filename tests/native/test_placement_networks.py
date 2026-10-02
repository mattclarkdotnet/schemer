from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_positions,
)
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.placement.networks import place_owned_pin_networks
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_network_move_preserves_transitive_attachments_at_rotated_owner_pins(angle):
    from copy import deepcopy
    from uuid import uuid4

    schematic, editor = _support_fixture("series")
    schematic["instances"]["root.BIAS"]["attributes"]["__symbol_value"] = {"String":
        '(symbol "P" (pin (name "~") (number "1")) (pin (name "~") (number "2")))'}
    schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]["order"] = 0
    original = editor.document.symbols[1].expression
    text = editor.get_as_string()
    template = text[original.start:original.end]
    additions = []
    for reference, path, owner, pin, role in (
        ("R3", "SHUNT", "R1", "SIG", "shunt"),
        ("C8", "COUPLING", "R2", "2", "ac-coupling"),
        ("R7", "UPSTREAM", "C8", "2", "shunt"),
    ):
        fragment = template.replace('"R2"', f'"{reference}"').replace('"BIAS"', f'"{path}"')
        fragment = fragment.replace(editor.document.symbols[1].uuid, str(uuid4()))
        additions.append(fragment)
        item = deepcopy(schematic["instances"]["root.BIAS"])
        item["reference_designator"] = reference
        item["attributes"]["schematic_properties"]["Json"] = {
            "role": role, "owner": owner, "pin": pin, "group": "bias",
        }
        schematic["instances"]["root." + path] = item
    editor = FileSchematic.from_text(text[:text.rfind(")")] + "\n".join(additions) + ")")
    schematic["nets"]["sig"]["ports"].append("root.SHUNT.1")
    schematic["nets"]["gnd"]["ports"] = ["root.OWNER.1", "root.SHUNT.2", "root.UPSTREAM.2"]
    schematic["nets"].update({
        "input": {"name": "INPUT", "ports": ["root.BIAS.2", "root.COUPLING.1"]},
        "upstream": {"name": "UPSTREAM", "ports": ["root.COUPLING.2", "root.UPSTREAM.1"]},
    })
    for symbol in editor.get_symbols():
        positions = {"R2": (150, 150), "C8": (160.16, 150), "R7": (170.32, 150),
                     "R3": (40, 40)}
        if symbol.reference in positions:
            symbol.position = Vector2.from_xy_mm(*positions[symbol.reference])
            symbol.transform.orientation = angle if symbol.reference == "R3" else 0
            editor.update_items(symbol)

    def attachment_vectors():
        pins = {s.reference: placed_pin_positions(editor.document, s)
                for s in editor.document.symbols}
        return [(pins[child]["1"].x - pins[owner]["2"].x,
                 pins[child]["1"].y - pins[owner]["2"].y)
                for owner, child in (("R2", "C8"), ("C8", "R7"))]

    before = attachment_vectors()
    fixed = {s.reference: s.position for s in editor.get_symbols()
             if s.reference in {"R1", "R3"}}
    place_owned_pin_networks(schematic, editor)
    assert attachment_vectors() == before
    assert fixed == {s.reference: s.position for s in editor.get_symbols() if s.reference in fixed}
    assert all(s.position.x < 100_000_000 and s.position.y < 100_000_000
               for s in editor.get_symbols() if s.reference in {"R2", "C8", "R7"})
    after = editor.get_as_string()
    place_owned_pin_networks(schematic, editor)
    assert editor.get_as_string() == after
