from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.kicad.geometry.text import field_draw_angle, text_envelope
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.annotations.fields import position_bank_fields, position_component_fields
from schemer.native.association import associate_components
from schemer.native.obstacles import component_body_obstacles
from schemer.native.placement.attachments import (
    align_single_pin_attachments,
    pack_supported_units,
)
from schemer.native.placement.banks import place_owned_shunt_banks
from schemer.native.placement.bridges import place_pin_bridges
from schemer.native.placement.inline import compact_inline_connections
from schemer.native.placement.networks import place_owned_pin_networks
from schemer.native.placement.queries import owner_symbol_for_pin
from schemer.native.routing_model import PlacedEndpoint
from tests.support.schematic import _support_fixture


@pytest.mark.parametrize("output_role", ["pin-bridge", "source-impedance"])
def test_multi_unit_support_resolves_pins_and_packs_completed_networks(output_role):
    from copy import deepcopy

    schematic, editor = _support_fixture("pin-bridge")
    definition = '''(symbol "Test:Amplifier"
      (symbol "Amplifier_1_1"
        (polyline (pts (xy -5.08 5.08) (xy 5.08 0) (xy -5.08 -5.08) (xy -5.08 5.08)))
        (pin input line (at -7.62 -2.54 0) (length 2.54) (name "IN") (number "1"))
        (pin output line (at 7.62 0 180) (length 2.54) (name "SIG") (number "2"))
        (pin input line (at -7.62 2.54 0) (length 2.54) (name "PLUS") (number "3")))
      (symbol "Amplifier_2_1"
        (pin power_in line (at 0 7.62 270) (length 2.54) (name "V+") (number "8"))
        (pin power_in line (at 0 -7.62 90) (length 2.54) (name "V-") (number "4"))))'''
    text = editor.get_as_string().replace("(lib_symbols", "(lib_symbols " + definition, 1)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Test:Amplifier")', 1)
    editor = FileSchematic.from_text(text)
    owner, bridge = editor.get_symbols()
    owner.transform.orientation = 0
    editor.update_items(owner)
    text = editor.get_as_string()
    first, second = [s.expression for s in editor.document.symbols]
    power = text[first.start:first.end].replace("11111111", "dddddddd")
    power = power.replace("(unit 1)", "(unit 2)")
    power = power.replace('(pin "1"', '(pin "8"').replace('(pin "2"', '(pin "4"')
    extra = text[second.start:second.end].replace("aaaaaaaa", "bbbbbbbb")
    extra = extra.replace('"BIAS"', '"EXTRA"').replace('"R2"', '"R3"')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + power + extra + ")")
    schematic["instances"]["root.OWNER"]["attributes"]["__symbol_value"] = {"String": definition}
    props = schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]
    props["other_pin"] = "IN"
    child = deepcopy(schematic["instances"]["root.BIAS"])
    child["reference_designator"] = "R3"
    child["attributes"]["schematic_properties"]["Json"]["role"] = output_role
    if output_role != "pin-bridge":
        del child["attributes"]["schematic_properties"]["Json"]["other_pin"]
    schematic["instances"]["root.EXTRA"] = child
    schematic["nets"]["sig"]["ports"].append("root.EXTRA.1")
    if output_role == "pin-bridge":
        schematic["nets"]["gnd"]["ports"].append("root.EXTRA.2")
    else:
        schematic["nets"]["out"] = {"name": "LOAD", "ports": ["root.EXTRA.2"]}
    assert owner_symbol_for_pin(schematic, editor, "R1", "SIG").unit == 1
    assert owner_symbol_for_pin(schematic, editor, "R1", "V+").unit == 2
    from schemer.native.annotations.fields import consolidate_multi_unit_fields

    fields = position_component_fields(editor, list(editor.get_symbols()))
    consolidate_multi_unit_fields(editor, associate_components(schematic, editor.document), fields)
    editor.update_items(fields)
    units = [s for s in editor.get_symbols() if s.reference == "R1"]
    assert all(s.reference_field.visible for s in units)
    assert sum(s.value_field.visible for s in units) == 1
    align_single_pin_attachments(schematic, editor)
    place_pin_bridges(schematic, editor)
    raw = {s.reference: s for s in editor.document.symbols if s.reference != "R1"}
    a = placed_pin_positions(editor.document, raw["R2"])
    b = placed_pin_positions(editor.document, raw["R3"])
    assert a["2"].x < a["1"].x  # Feedback input is on the left, regardless of part pin order.
    if output_role == "pin-bridge":
        assert b["2"].x == a["2"].x and b["2"].y > a["2"].y
    else:
        assert b["1"].y == owner.position.y and b["1"].x > owner.position.x
    editor.update_items(position_component_fields(editor, list(editor.get_symbols())))
    pack_supported_units(schematic, editor)
    unit = next(s for s in editor.document.symbols if s.reference == "R1" and s.unit == 2)
    assert min(p.y for p in placed_pin_positions(editor.document, unit).values()) > max(
        p.y for s in editor.document.symbols if s.reference in {"R2", "R3"}
        for p in placed_symbol_body_positions(editor.document, s))
    from schemer.kicad.geometry.annotations import pin_name_envelopes
    from schemer.native.routing import _clear_pin_escape
    # A pin-only unit has no solid rectangle spanning its terminals, but
    # its visible pin names are real obstacles. Outward exits remain clear.
    obstacles = component_body_obstacles(editor, {unit.uuid: "power"})["power"]
    assert obstacles == pin_name_envelopes(editor, unit)
    sides = placed_pin_sides(editor.document, unit)
    assert all(_clear_pin_escape(PlacedEndpoint(p, sides[n]), obstacles, [])
               for n, p in placed_pin_positions(editor.document, unit).items())

    if output_role == "pin-bridge":
        # A parallel shunt bank must also resolve the signal unit, even
        # though the final symbol with this reference is the supply unit.
        for ref in ("root.BIAS", "root.EXTRA"):
            intent = schematic["instances"][ref]["attributes"]["schematic_properties"]["Json"]
            intent["role"] = "shunt"
            intent.pop("other_pin")
        place_owned_shunt_banks(schematic, editor)
        bank = [s for s in editor.document.symbols if s.reference in {"R2", "R3"}]
        endpoints = [placed_pin_positions(editor.document, s)["1"] for s in bank]
        signal_unit = next(s for s in editor.document.symbols
                           if s.reference == "R1" and s.unit == 1)
        assert endpoints[0].x == endpoints[1].x
        assert endpoints[0].y != endpoints[1].y
        assert endpoints[0].x > placed_pin_positions(editor.document, signal_unit)["2"].x
    else:
        shunt = schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]
        shunt["role"] = "shunt"
        shunt.pop("other_pin")
        series = schematic["instances"]["root.EXTRA"]["attributes"]["schematic_properties"]["Json"]
        series.update(role="series", order=0)
        place_owned_pin_networks(schematic, editor)


    # Packing a supply unit carries the entire authored support chain, not
    # merely the passive attached directly to its pin.
    parent = schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]
    parent.update(owner="R1", pin="V+")
    child = schematic["instances"]["root.EXTRA"]["attributes"]["schematic_properties"]["Json"]
    child.update(owner="R2", pin="1")
    parts = list(editor.get_symbols())
    for part in parts:
        if part.reference != "R1" or part.unit == 2:
            part.position = Vector2.from_xy_mm(20, 30)
    editor.update_items(parts)
    before = {s.id: s.position.y for s in editor.get_symbols()}
    pack_supported_units(schematic, editor)
    shifts = {s.position.y - before[s.id] for s in editor.get_symbols()
              if s.reference != "R1" or s.unit == 2}
    assert len(shifts) == 1 and next(iter(shifts)) > 0


@pytest.mark.parametrize("count", [2, 3, 4, 5])
@pytest.mark.parametrize("owner_rotation", [0, 180])
@pytest.mark.parametrize("seed_rotation", [0, 90])
def test_owned_runs_stack_after_two_parts(count, owner_rotation, seed_rotation):
    from copy import deepcopy
    from uuid import uuid4

    from schemer.kicad.items import place_symbol
    from schemer.native.placement.chains import stack_owned_passive_runs

    schematic, editor = _support_fixture("series")
    schematic["instances"]["root.BIAS"]["attributes"]["__symbol_value"] = {"String":
        '(symbol "P" (pin passive line (name "1") (number "1")) '
        '(pin passive line (name "2") (number "2")))'}
    owner, first = editor.get_symbols()
    place_symbol(owner, Vector2.from_xy_mm(100, 100), owner_rotation)
    place_symbol(first, Vector2.from_xy_mm(120, 100), 0)
    editor.update_items([owner, first])
    template = editor.document.symbols[1].expression
    text = editor.get_as_string()
    fragment = text[template.start:template.end]
    extras = []
    refs = ["R2"]
    paths = ["BIAS"]
    for i in range(1, count):
        ref, path = f"R{i+2}", f"CHAIN{i}"
        extras.append(fragment.replace(first.id, str(uuid4()))
                      .replace('"R2"', f'"{ref}"').replace('"BIAS"', f'"{path}"'))
        item = deepcopy(schematic["instances"]["root.BIAS"])
        item["reference_designator"] = ref
        item["attributes"]["schematic_properties"]["Json"].update(owner=refs[-1], pin="2")
        schematic["instances"]["root." + path] = item
        refs.append(ref)
        paths.append(path)
    editor = FileSchematic.from_text(text[:text.rfind(")")] + "\n".join(extras) + ")")
    for i, s in enumerate(editor.get_symbols()):
        if s.reference != "R1":
            place_symbol(s, Vector2.from_xy_mm(120 + i * 20, 100), seed_rotation)
            editor.update_items(s)
    schematic["nets"] = {"in": {"name": "IN", "ports": ["root.OWNER.SIG", "root.BIAS.1"]}}
    for i, path in enumerate(paths):
        schematic["nets"][str(i)] = {"name": f"NODE{i}", "ports": [
            f"root.{path}.2", *([f"root.{paths[i+1]}.1"] if i+1 < count else [])]}
    original = deepcopy(schematic)
    before = editor.get_as_string()
    banks = stack_owned_passive_runs(schematic, editor)
    assert schematic == original
    if count <= 2:
        assert editor.get_as_string() == before
        return
    from schemer.native.placement.inline import align_authored_inline_banks

    folded = frozenset(banks)
    geometry = {s.uuid: (s.position, s.rotation)
                for s in editor.document.symbols if s.uuid in folded}
    align_single_pin_attachments(schematic, editor, excluded=folded)
    compact_inline_connections(schematic, editor, excluded=folded)
    align_authored_inline_banks(schematic, editor, excluded=folded)
    assert geometry == {s.uuid: (s.position, s.rotation)
                        for s in editor.document.symbols if s.uuid in folded}
    bank = {s.reference: s for s in editor.document.symbols if s.reference in refs}
    rows = {}
    for s in bank.values():
        rows.setdefault(s.position[1], []).append(s)
    assert len(rows) == count
    assert max(map(len, rows.values())) == 1
    assert len({s.position[0] for s in bank.values()}) == 1
    for i in range(1, count):
        a, b = bank[refs[i-1]], bank[refs[i]]
        assert placed_pin_positions(editor.document, a)["2"].x == (
            placed_pin_positions(editor.document, b)["1"].x)
    components = position_component_fields(editor, list(editor.get_symbols()))
    position_bank_fields(editor, components, banks)
    editor.update_items(components)
    captions = [s for s in editor.get_symbols() if s.reference in refs]
    assert len({s.reference_field.text.position.x for s in captions}) == 1
    right = max(p.x for s in bank.values()
                for p in placed_pin_positions(editor.document, s).values()) + 2_540_000
    for s in captions:
        assert s.reference_field.text.position.y == s.value_field.text.position.y == s.position.y
        assert s.reference_field.text.position.x > right
        for f in (s.reference_field, s.value_field):
            assert field_draw_angle(f.text.attributes.angle, s.transform.orientation) == 0
        ref_box = text_envelope(s.reference_field.text, s.transform.orientation)
        value_box = text_envelope(s.value_field.text, s.transform.orientation)
        assert ref_box[1].x < value_box[0].x
    assert next(s for s in editor.document.symbols if s.reference == "R1").position == (100, 100)


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("shared_group", [True, False])
def test_owned_pin_network_keeps_series_beside_shunt_without_pin_alignment(angle, shared_group):
    from copy import deepcopy

    schematic, editor = _support_fixture("series")
    props = schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]
    props["order"] = 0
    text = editor.get_as_string()
    node = editor.document.symbols[1].expression
    extra = text[node.start:node.end].replace("aaaaaaaa", "bbbbbbbb")
    extra = extra.replace('"R2"', '"R3"').replace('"BIAS"', '"SHUNT"')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + extra + ")")
    schematic["instances"]["root.SHUNT"] = deepcopy(schematic["instances"]["root.BIAS"])
    schematic["instances"]["root.SHUNT"]["reference_designator"] = "R3"
    attributes = schematic["instances"]["root.SHUNT"]["attributes"]["schematic_properties"]["Json"]
    attributes.update(role="shunt", group="bias" if shared_group else "other")
    attributes.pop("order")
    schematic["nets"]["sig"]["ports"].append("root.SHUNT.1")
    schematic["nets"]["gnd"]["ports"] = ["root.OWNER.1", "root.SHUNT.2"]
    schematic["nets"]["input"] = {"name": "INPUT", "ports": ["root.BIAS.2"]}
    for symbol in editor.get_symbols():
        if symbol.reference == "R2":
            symbol.position = Vector2.from_xy_mm(150, 150)
        elif symbol.reference == "R3":
            symbol.position = Vector2.from_xy_mm(40, 40)
            symbol.transform.orientation = angle
        editor.update_items(symbol)
    before = {s.reference: (s.position, s.transform.orientation) for s in editor.get_symbols()}

    align_single_pin_attachments(schematic, editor)
    assert before == {s.reference: (s.position, s.transform.orientation)
                      for s in editor.get_symbols()}
    place_owned_pin_networks(schematic, editor)

    after = {s.reference: (s.position, s.transform.orientation) for s in editor.get_symbols()}
    assert before["R1"] == after["R1"]
    assert before["R3"] == after["R3"]
    if not shared_group:
        assert before == after
        return
    series, shunt = editor.document.symbols[1:]
    a = placed_pin_positions(editor.document, series)["1"]
    b = placed_pin_positions(editor.document, shunt)["1"]
    assert abs(a.x - b.x) == abs(a.y - b.y) == 2_540_000
    a_side = placed_pin_sides(editor.document, series)["1"]
    b_side = placed_pin_sides(editor.document, shunt)["1"]
    assert (a_side in {"left", "right"}) != (b_side in {"left", "right"})
    place_owned_pin_networks(schematic, editor)
    assert after == {s.reference: (s.position, s.transform.orientation)
                     for s in editor.get_symbols()}


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("shared_node", [False, True])
@pytest.mark.parametrize("local_peer", [False, True])
@pytest.mark.parametrize("role", ["series-termination", "series", "shunt"])
def test_authored_owner_overrides_bridge_ambiguity_and_stale_pin_row(
    angle, shared_node, local_peer, role,
):
    schematic, editor = _support_fixture(role)
    if role == "series":
        schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"][
            "order"] = 0
    definition = '''(symbol "Test:IC" (symbol "IC_1_1"
      (pin input line (at -2.54 0 0) (length 1.27) (name "IN") (number "1"))
      (pin output line (at 2.54 0 180) (length 1.27) (name "SIG") (number "2"))
      (pin input line (at -2.54 -2.54 0) (length 1.27) (name "EN") (number "3"))))'''
    text = editor.get_as_string().replace("(lib_symbols", "(lib_symbols " + definition, 1)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Test:IC")', 1)
    editor = FileSchematic.from_text(text)
    owner = next(s for s in editor.document.symbols if s.reference == "R1")
    copy = text[owner.expression.start:owner.expression.end].replace("11111111", "cccccccc")
    copy = copy.replace('"OWNER"', '"REMOTE"').replace('"R1"', '"U9"')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + copy + ")")
    schematic["instances"]["root.REMOTE"] = {"reference_designator": "U9"}
    if local_peer:
        schematic["instances"]["root.REMOTE"]["attributes"] = {
            "schematic_properties": {"Json": {
                "role": "buffer", "group": "support", "owner": "R1", "pin": "SIG",
            }},
        }
    schematic["nets"]["gnd"]["ports"] = ["root.BIAS.2", "root.REMOTE.1"]
    if shared_node:
        schematic["nets"]["gnd"]["ports"].remove("root.REMOTE.1")
        schematic["nets"]["sig"]["ports"].append("root.REMOTE.1")
    for s in editor.get_symbols():
        s.transform.orientation = angle
        if s.reference == "R2":
            s.position = Vector2.from_xy_mm(80, 80)
        elif s.reference == "U9":
            s.position = Vector2.from_xy_mm(150, 150)
        editor.update_items(s)
    align_single_pin_attachments(schematic, editor)
    if shared_node and local_peer:
        assert next(s for s in editor.get_symbols() if s.reference == "R2").position == (
            Vector2.from_xy_mm(80, 80)
        )
        return
    owner, child = [next(s for s in editor.document.symbols if s.reference == ref)
                    for ref in ("R1", "R2")]
    a = placed_pin_positions(editor.document, owner)["2"]
    b = placed_pin_positions(editor.document, child)["1"]
    side = placed_pin_sides(editor.document, owner)["2"]
    dx, dy = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}[side]
    assert b == Vector2(a.x + dx * 5_080_000, a.y + dy * 5_080_000)


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("owner_rotation", [0, 90])
@pytest.mark.parametrize("through_series", [False, True])
@pytest.mark.parametrize("boundary", [None, "module", "series"])
def test_single_pin_attachments_follow_pin_axes_without_roles(
    direction, owner_rotation, through_series, boundary,
):
    schematic, editor = _support_fixture("pulldown")
    text = editor.get_as_string()
    resistor = next(s for s in editor.document.symbols if s.reference == "R2")
    expression = resistor.expression
    copy = text[expression.start:expression.end].replace("aaaaaaaa", "bbbbbbbb")
    copy = copy.replace('"BIAS"', '"BIAS2"').replace('"R2"', '"R3"')
    if through_series:
        inline = copy.replace("bbbbbbbb", "cccccccc")
        inline = inline.replace('"BIAS2"', '"INLINE"').replace('"R3"', '"R4"')
        copy += inline
    definition = f'''(symbol "Test:Port" (symbol "Port_1_1"
      (pin passive line (at {direction * 2.54} 0 {180 if direction == 1 else 0})
        (length 1.27) (name "SIG") (number "1"))
      (pin passive line (at {direction * 2.54} -2.54 {180 if direction == 1 else 0})
        (length 1.27) (name "SIG2") (number "2"))))'''
    text = text[:text.rfind(")")] + copy + ")"
    text = text.replace("(lib_symbols", "(lib_symbols " + definition, 1)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Test:Port")', 1)
    text = text.replace('(start -1.27 -1.27) (end 1.27 1.27)',
                        '(start -1.27 -0.635) (end 1.27 0.635)')
    editor = FileSchematic.from_text(text)
    for item in editor.get_symbols():
        if item.reference == "R1":
            item.position = Vector2(0, 0)
            item.transform.orientation = owner_rotation
        elif item.reference == "R4":
            item.position = Vector2.from_xy_mm(direction * 18, 10)
            item.transform.orientation = 0
        else:
            x = direction * (15 if item.reference == "R2" else 25)
            item.position = Vector2.from_xy_mm(x, 20)
            item.transform.orientation = 270
        editor.update_items(item)
    schematic["instances"]["root.OWNER"]["attributes"]["__symbol_value"] = {"String":
        '(symbol "Port" (pin (name "SIG") (number "1")) (pin (name "SIG2") (number "2")))'}
    schematic["instances"]["root.BIAS2"] = {"reference_designator": "R3", "attributes": {
        "schematic_properties": {"Json": {
            "role": "pulldown", "group": "bias", "owner": "R1", "pin": "SIG2",
        }},
    }}
    schematic["nets"] = {
        "a": {"name": "A", "ports": ["root.OWNER.SIG", "root.BIAS.1"]},
        "b": {"name": "B", "ports": ["root.OWNER.SIG2", "root.BIAS2.1"]},
        "g": {"name": "GND", "kind": "Ground", "ports": ["root.BIAS.2", "root.BIAS2.2"]},
        "data": {"name": "DATA", "ports": []},
    }
    if through_series:
        schematic["instances"]["root.INLINE"] = {"reference_designator": "R4"}
        schematic["nets"]["raw"] = {"name": "RAW", "ports": ["root.INLINE.1"]}
        schematic["nets"]["data"]["ports"] = ["root.INLINE.2"]

    # No authored role is needed for this geometric default.
    for instance in schematic["instances"].values():
        instance.get("attributes", {}).pop("schematic_properties", None)
    if boundary == "series":
        for order, name in enumerate(("BIAS", "BIAS2")):
            schematic["instances"]["root." + name]["attributes"]["schematic_properties"] = {
                "Json": {"role": "series", "group": "filter", "order": order},
            }
    elif boundary == "module":
        for name in ("BIAS", "BIAS2"):
            instance = schematic["instances"].pop("root." + name)
            schematic["instances"]["root.REMOTE." + name] = instance
            for net in schematic["nets"].values():
                net["ports"] = [p.replace("root." + name + ".", "root.REMOTE." + name + ".")
                                for p in net["ports"]]
        for item in editor.get_symbols():
            if item.reference in {"R2", "R3"}:
                item.field("Path").text.value = "REMOTE." + item.zener_path
                editor.update_items(item)
    before = {s.reference: s.position for s in editor.get_symbols()}
    align_single_pin_attachments(schematic, editor)

    if boundary:
        assert before == {s.reference: s.position for s in editor.get_symbols()}
        return

    owner = next(s for s in editor.document.symbols if s.reference == "R1")
    owner_pins = placed_pin_positions(editor.document, owner)
    owner_sides = placed_pin_sides(editor.document, owner)
    for reference, number in (("R2", "1"), ("R3", "2")):
        raw = next(s for s in editor.document.symbols if s.reference == reference)
        pins = placed_pin_positions(editor.document, raw)
        dx, dy = {"left": (-1, 0), "right": (1, 0),
                  "top": (0, -1), "bottom": (0, 1)}[owner_sides[number]]
        anchor = owner_pins[number]
        assert pins["1"] == Vector2(anchor.x + dx * 5_080_000, anchor.y + dy * 5_080_000)
        assert (pins["2"].x - pins["1"].x) * dy == 0
        assert (pins["2"].y - pins["1"].y) * dx == 0
    positions = {s.reference: (s.position, s.transform.orientation) for s in editor.get_symbols()}
    align_single_pin_attachments(schematic, editor)
    assert positions == {s.reference: (s.position, s.transform.orientation)
                         for s in editor.get_symbols()}

    if through_series:
        # A second device with repeated pins on the far net makes this a
        # bridge, not a single-owner attachment. Do not pull it to either end.
        device = next(s for s in editor.document.symbols if s.reference == "R4")
        source = editor.get_as_string()
        expr = device.expression
        changed = source[expr.start:expr.end].replace('"Device:R"', '"Test:Port"')
        editor = FileSchematic.from_text(source[:expr.start] + changed + source[expr.end:])
        schematic["instances"]["root.INLINE"]["attributes"] = schematic["instances"][
            "root.OWNER"]["attributes"]
        schematic["nets"]["g"]["ports"].remove("root.BIAS.2")
        schematic["nets"].pop("raw")
        schematic["nets"]["data"]["ports"] = [
            "root.BIAS.2", "root.INLINE.SIG", "root.INLINE.SIG2",
        ]
        resistor = next(s for s in editor.get_symbols() if s.reference == "R2")
        resistor.position = Vector2.from_xy_mm(70, 70)
        editor.update_items(resistor)
        align_single_pin_attachments(schematic, editor)
        assert next(s for s in editor.get_symbols() if s.reference == "R2").position == (
            Vector2.from_xy_mm(70, 70)
        )
