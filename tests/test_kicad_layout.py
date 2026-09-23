import pytest

from schemer.cli import build_parser
from schemer.kicad_api import (
    Junction,
    SchematicField,
    SchematicLine,
    SchematicSymbolInstance,
    SchematicSymbolTransform,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.kicad_layout import (
    _clear_signal_stub_corridors,
    _component_layout_groups,
    _drawing_translation,
    _Envelope,
    _envelope_from_points,
    _envelopes_do_not_overlap,
    _fit_signal_labels_on_pin_exits,
    _label_envelope,
    _localize_rail_symbols,
    _localize_signal_labels,
    _merge_touching_rail_symbols,
    _nearest_local_target,
    _net_symbol_targets,
    _NetSymbolTarget,
    _ordered_unit_position,
    _packed_group_deltas,
    _PlacedEndpoint,
    _position_compact_two_terminal_fields,
    _power_flow_edges,
    _power_stage_order,
    _prune_unused_targets,
    _rail_caption_offsets,
    _route_group,
    _segment_hits_box,
    _separate_same_face_rail_corridors,
    _shared_vertical_trunk,
    _split_rails_across_component_faces,
    _standard_page_for_bounds,
    _text_envelope,
    _translate_symbol,
    _wire_contact,
)


def _line_mm(item: SchematicLine) -> tuple[tuple[float, float], tuple[float, float]]:
    return (
        (item.start.x / 1_000_000, item.start.y / 1_000_000),
        (item.end.x / 1_000_000, item.end.y / 1_000_000),
    )


def test_glyph_envelope_includes_every_vertex_regardless_of_drawing_order():
    assert _envelope_from_points((Vector2(0, 0), Vector2(0, 1_270_000),
                                 Vector2(1_270_000, 1_270_000), Vector2(0, 2_540_000),
                                 Vector2(-1_270_000, 1_270_000))) == _Envelope(
        -1_270_000, 0, 1_270_000, 2_540_000,
    )


def test_cross_block_interfaces_get_targets_without_legacy_position_comments():
    schematic = {"nets": {
        "external": {"id": 1, "name": "EXTERNAL", "kind": "Net"},
        "local": {"id": 2, "name": "LOCAL", "kind": "Net"},
    }}
    a = _PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "a", "FIRST")
    b = _PlacedEndpoint(Vector2.from_xy_mm(40, 10), "left", "b", "SECOND")
    endpoints = {"EXTERNAL": [a, b], "LOCAL": [a, a]}
    templates = _net_symbol_targets(
        schematic, [], {"1": "EXT"}, (0, 0), {}, Vector2(0, 0), endpoints,
    )
    assert [target.net_name for target in templates] == ["EXTERNAL"]
    labels = _localize_signal_labels(templates, endpoints)
    assert {target.group for target in labels} == {"FIRST", "SECOND"}
    assert {target.display_name for target in labels} == {"EXT"}


def test_each_connector_is_a_local_block_even_inside_a_mixed_module():
    def component(reference, kind):
        return {"reference_designator": reference, "attributes": {"type": {"String": kind}}}

    schematic = {"root_ref": "root", "instances": {
        "root.POWER.U1.IC": component("U1", "ic"),
        "root.POWER.J1.PORT": component("J1", "connector"),
        "root.POWER.J2.PORT": component("J2", "connector"),
        "root.POWER.R1.R": component("R1", "resistor"),
        "root.POWER.R2.R": component("R2", "resistor"),
        "root.POWER.R1": {"attributes": {"schematic_properties": {"Json": {
            "role": "pulldown", "owner": "J1", "pin": "CC", "group": "bias",
        }}}},
    }}
    groups, connectors = _component_layout_groups(schematic)
    assert connectors == {"POWER.J1.PORT", "POWER.J2.PORT"}
    assert groups["root.POWER.R1.R"] == groups["root.POWER.J1.PORT"]
    assert groups["root.POWER.R2.R"] == groups["root.POWER.U1.IC"] == "POWER"


def test_power_stages_follow_pin_directions_and_preserve_authored_support_ownership():
    # Deliberately use reverse reference/path order and arbitrary net names.
    physical = {}
    for name, designator in (("Z", "U9"), ("M", "U3"), ("A", "U1")):
        physical[f"root.BLOCK.{name}"] = {"reference_designator": designator, "attributes": {
            "__symbol_value": {"String": '(symbol "P" '
                '(pin power_in line (name "IN") (number "1")) '
                '(pin power_out line (name "OUT") (number "2")))'},
        }}
    physical["root.BLOCK.CAP"] = {"reference_designator": "C1", "attributes": {
        "schematic_properties": {"Json": {"role": "bypass", "owner": "U3",
                                             "pin": "IN", "group": "input"}},
    }}
    schematic = {"root_ref": "root", "instances": physical, "nets": {
        "x": {"name": "X", "kind": "Power", "ports": ["root.BLOCK.Z.OUT", "root.BLOCK.M.IN"]},
        "y": {"name": "Y", "kind": "Power", "ports": ["root.BLOCK.M.OUT", "root.BLOCK.A.IN"]},
    }}
    groups, connectors = _component_layout_groups(schematic)
    assert not connectors
    assert groups["root.BLOCK.CAP"] == "BLOCK.M"
    assert _power_stage_order(schematic, groups) == {"BLOCK": ("BLOCK.Z", "BLOCK.M", "BLOCK.A")}
    assert _power_flow_edges(schematic) == {
        ("root.BLOCK.Z", "root.BLOCK.M"), ("root.BLOCK.M", "root.BLOCK.A"),
    }


def test_single_connector_module_keeps_its_authored_block_name():
    schematic = {"root_ref": "root", "instances": {
        "root.PANEL.J1.PORT": {"reference_designator": "J1", "attributes": {
            "type": {"String": "connector"},
        }},
    }}
    groups, connectors = _component_layout_groups(schematic)
    assert groups == {"root.PANEL.J1.PORT": "PANEL"}
    assert connectors == {"PANEL"}


@pytest.mark.parametrize("kind", ["connector", "optical_receiver", "optical_transmitter"])
def test_external_interface_group_uses_type_not_reference_prefix(kind):
    schematic = {"root_ref": "root", "instances": {
        "root.FUNCTION.PORT": {"reference_designator": "U9", "attributes": {
            "type": {"String": kind},
        }},
        "root.FUNCTION.CAP": {"reference_designator": "C8", "attributes": {
            "schematic_properties": {"Json": {
                "role": "bypass", "owner": "U9", "pin": "VDD", "group": "supply",
            }},
        }},
        "root.FUNCTION.LOGIC": {"reference_designator": "J7", "attributes": {
            "type": {"String": "ic"},
        }},
    }}
    groups, interfaces = _component_layout_groups(schematic)
    assert interfaces == {"FUNCTION.PORT"}
    assert groups["root.FUNCTION.CAP"] == groups["root.FUNCTION.PORT"]
    assert groups["root.FUNCTION.LOGIC"] == "FUNCTION"


@pytest.mark.parametrize("ground", [False, True])
def test_supply_and_signal_ties_keep_separate_same_net_wiresets(ground):
    pins = [_PlacedEndpoint(Vector2.from_xy_mm(10, y), "left", "device", "block",
                            rail_class=kind)
            for y, kind in [(10, "supply"), (12.54, "signal-tie"), (15.08, "signal-tie")]]
    bypass = _PlacedEndpoint(Vector2.from_xy_mm(5, 10), "right", "cap", "block",
                             rail_class="supply")
    template = _NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, ground)
    rails = _localize_rail_symbols([template], {"RAIL": [*pins, bypass]})
    assert len(rails) == 2
    supply = next(t for t in rails if pins[0] in t.members)
    ties = next(t for t in rails if pins[1] in t.members)
    assert len(supply.members) == 2 and bypass in supply.members
    assert len(ties.members) == 2 and pins[2] in ties.members
    # Later glyph clearance must not undo the partition.
    from dataclasses import replace
    rails = [replace(t, position=Vector2(0, 0)) for t in rails]
    bounds = {ground: _Envelope(-1_270_000, -2_540_000, 1_270_000, 0)}
    assert _merge_touching_rail_symbols(rails, bounds) == rails


def test_legacy_label_proximity_cannot_merge_independently_packed_blocks():
    targets = [
        _NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(x, 10), 0, False, False)
        for x in (0, 100)
    ]
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(x, 10), "left", group=group)
        for x, group in ((10, "panel.j1"), (20, "panel.j2"), (90, "processor"))
    ]
    labels = _localize_signal_labels(targets, {"DATA": endpoints})
    assert {label.group for label in labels} == {endpoint.group for endpoint in endpoints}
    assert len(labels) == 3


def test_target_selection_and_pruning_stay_within_the_block():
    targets = [
        _NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(x, 10), 0, False, False,
                         group=group)
        for x, group in ((10, "first"), (20, "second"))
    ]
    # Both endpoints are closer to the other block's label before packing.
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(x, 10), "left", group=group)
        for x, group in ((21, "first"), (11, "second"))
    ]
    assert [_nearest_local_target(p, targets) for p in endpoints] == [0, 1]
    assert _prune_unused_targets(targets, {"DATA": endpoints}) == targets


def test_rail_membership_survives_termination_movement():
    pin = _PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "IC", "block")
    assigned = _NetSymbolTarget("GND", "GND", Vector2.from_xy_mm(40, 10),
                                0, True, True, group="block", members=(pin,))
    nearer = _NetSymbolTarget("GND", "GND", Vector2.from_xy_mm(13, 10),
                              0, True, True, group="block")
    assert _nearest_local_target(pin, [nearer, assigned]) == 1
    assert _prune_unused_targets([nearer, assigned], {"GND": [pin]}) == [assigned]


def test_same_net_glyphs_share_a_termination_when_clearance_brings_them_together():
    pins = [_PlacedEndpoint(Vector2.from_xy_mm(x, 10), "right", group="block")
            for x in (10, 30)]
    rails = [_NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(40, y),
                              0, True, False, group="block", members=(pin,))
             for y, pin in zip((10, 11.27), pins)]
    bounds = {False: _Envelope(-1_270_000, -2_540_000, 1_270_000, 0)}
    merged = _merge_touching_rail_symbols(rails, bounds)
    assert len(merged) == 1
    assert merged[0].members == tuple(pins)
    assert merged[0].position.y == 10_000_000
    rails[1] = _NetSymbolTarget("OTHER", "OTHER", rails[1].position, 0, True, False,
                               group="block", members=(pins[1],))
    assert _merge_touching_rail_symbols(rails, bounds) == rails


@pytest.mark.parametrize("ground, neighbour_y", [(True, 12.54), (False, 7.46)])
def test_rail_glyph_clears_neighbour_wire_even_without_a_label(ground, neighbour_y):
    pin = _PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "IC", "block")
    neighbour = _PlacedEndpoint(Vector2.from_xy_mm(10, neighbour_y), "right", "IC", "block")
    rail = _NetSymbolTarget("RAIL", "RAIL", Vector2.from_xy_mm(12.54, 10),
                            0, True, ground, group="block", members=(pin,))
    glyph = _Envelope(-1_270_000, 0 if ground else -2_540_000,
                      1_270_000, 2_540_000 if ground else 0)
    moved = _clear_signal_stub_corridors([rail], {"RAIL": [pin], "OTHER": [neighbour]},
                                         {ground: glyph})[0]
    assert moved.position.x > rail.position.x
    assert not _segment_hits_box(neighbour.position, Vector2.from_xy_mm(12.54, neighbour_y),
                                 glyph.translated(moved.position))


@pytest.mark.parametrize("obstacle", [
    _Envelope(15_000_000, 8_000_000, 25_000_000, 12_000_000),
    _Envelope(19_750_000, 9_750_000, 20_250_000, 10_250_000),
])
def test_router_clears_bodies_and_foreign_pin_endpoints(obstacle):
    endpoints = [_PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right"),
                 _PlacedEndpoint(Vector2.from_xy_mm(30, 10), "left")]
    wires = [w for w in _route_group(endpoints, obstacles=[obstacle])
             if isinstance(w, SchematicLine)]
    assert wires
    assert all(not _segment_hits_box(w.start, w.end, obstacle) for w in wires)
    assert all(any(pin.position in (w.start, w.end) for w in wires) for pin in endpoints)


def test_router_does_not_reverse_through_an_attached_component():
    body = _Envelope(10_000_000, 9_000_000, 12_000_000, 11_000_000)
    endpoints = [_PlacedEndpoint(Vector2.from_xy_mm(9, 10), "left"),
                 _PlacedEndpoint(Vector2.from_xy_mm(30, 10), "left")]
    wires = [w for w in _route_group(endpoints, obstacles=[body])
             if isinstance(w, SchematicLine)]
    assert all(not _segment_hits_box(w.start, w.end, body) for w in wires)


def test_router_does_not_create_a_false_tee_on_a_foreign_wire():
    foreign = SchematicLine(id="foreign", start=Vector2.from_xy_mm(20, 10),
                            end=Vector2.from_xy_mm(20, 20))
    endpoints = [_PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right"),
                 _PlacedEndpoint(Vector2.from_xy_mm(30, 10), "left")]
    wires = [w for w in _route_group(endpoints, foreign_wires=[foreign])
             if isinstance(w, SchematicLine)]
    assert all(not _wire_contact(w.start, w.end, foreign) for w in wires)


def test_rail_clearance_preserves_target_order_and_clears_final_neighbour_routes():
    pins = {name: [_PlacedEndpoint(Vector2.from_xy_mm(20, y), "left", "IC", "block")]
            for name, y in (("V1", 10), ("V2", 12.54), ("SIGNAL", 15.08))}
    targets = [_NetSymbolTarget(name, name, Vector2.from_xy_mm(17.46, y), 0,
                                name != "SIGNAL", False, group="block",
                                members=tuple(pins[name]))
               for name, y in (("V2", 12.54), ("SIGNAL", 15.08), ("V1", 10))]
    glyph = _Envelope(-1_270_000, -2_540_000, 1_270_000, 0)
    moved = _clear_signal_stub_corridors(targets, pins, {False: glyph})
    assert [t.net_name for t in moved] == ["V2", "SIGNAL", "V1"]
    assert moved[1] == targets[1]
    assert moved[0].position.x < moved[2].position.x
    assert not _segment_hits_box(pins["V1"][0].position, moved[2].position,
                                 glyph.translated(moved[0].position))


def test_coincident_labels_in_different_blocks_survive_until_packing():
    target = _NetSymbolTarget("DATA", "DATA", Vector2(0, 0), 0, False, False)
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(x, 10), side, group=group)
        for x, side, group in ((10, "right", "a"), (15.08, "left", "b"))
    ]
    labels = _localize_signal_labels([target], {"DATA": endpoints})
    assert len(labels) == 2
    assert labels[0].position == labels[1].position
    assert {label.group for label in labels} == {"a", "b"}


def test_rotated_label_bounds_rotate_justification_about_its_attachment():
    text = Text("DATA", Vector2.from_xy_mm(10, 10),
                TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 90, "right"))
    low, high = _text_envelope(text)
    assert low == Vector2.from_xy_mm(9.365, 10)
    assert high == Vector2.from_xy_mm(10.635, 15.08)


@pytest.mark.parametrize("angle", [0, 90])
def test_native_label_bounds_include_wire_inset_and_stroke(angle):
    text = Text("DATA", Vector2(0, 0),
                TextAttributes(Vector2.from_xy_mm(1.27, 1.27), angle, "left", "bottom"))
    low, high = _label_envelope(text)
    # Native stroke plots extend about 1.86 mm from the electrical anchor,
    # rather than only the nominal 1.27 mm text height.
    assert (low.y if angle == 0 else low.x) < -1_860_000
    assert (high.y if angle == 0 else high.x) < 0


def test_label_fit_includes_neighbouring_single_pin_wire_exits():
    pins = {name: [_PlacedEndpoint(Vector2.from_xy_mm(20, y), "right", "IC", "block")]
            for name, y in (("FIRST", 10), ("SECOND", 12.54), ("CLOCK", 15.08))}
    targets = [_NetSymbolTarget(name, name, Vector2.from_xy_mm(x, pins[name][0].position.y / 1e6),
                                0, False, False, "left", group="block", members=tuple(pins[name]))
               for name, x in (("FIRST", 25.08), ("SECOND", 35.24), ("CLOCK", 25.08))]
    fitted = _fit_signal_labels_on_pin_exits(targets, pins, {})
    assert all(label.rotation == 0 for label in fitted)
    for label in fitted:
        box = _envelope_from_points(_label_envelope(Text(
            label.display_name, label.position,
            TextAttributes(Vector2.from_xy_mm(1.27, 1.27), label.rotation,
                           label.text_alignment, "bottom"),
        )))
        assert all(not _segment_hits_box(pins[other.net_name][0].position, other.position, box)
                   for other in fitted if other.net_name != label.net_name)


def test_supply_trunk_clears_the_whole_signal_label_not_just_its_anchor():
    supply = _NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(20, 0), 0, True, False,
                              group="IC")
    signal = _NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(20, 10), 0, False, False,
                              text_alignment="left", group="IC")
    endpoints = {"VDD": [_PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right", group="IC")]}

    adjusted = _clear_signal_stub_corridors([supply, signal], endpoints)

    assert adjusted[0].position.x > Vector2.from_xy_mm(25, 0).x
    assert adjusted[0].position.y == 0
    assert adjusted[1] == signal


def test_rail_clearance_uses_only_pins_served_by_that_local_symbol():
    rails = [_NetSymbolTarget("GND", "GND", Vector2.from_xy_mm(20, y),
                             0, True, True, group="IC") for y in (0, 40)]
    signal = _NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(20, 20),
                              0, False, False, text_alignment="left", group="IC")
    endpoints = {"GND": [_PlacedEndpoint(Vector2.from_xy_mm(17.46, y),
                                         "right", group="IC") for y in (0, 40)]}
    assert _clear_signal_stub_corridors([*rails, signal], endpoints) == [*rails, signal]


def test_net_label_yields_on_its_own_wire_before_a_clear_trunk_moves():
    rail = _NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(10, 0),
                            0, True, False, group="IC")
    label = _NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(10, 10),
                             0, False, False, text_alignment="left", group="IC")
    endpoints = {
        "VDD": [_PlacedEndpoint(Vector2.from_xy_mm(0, 20), "right", group="IC")],
        "DATA": [_PlacedEndpoint(Vector2.from_xy_mm(0, 10), "right", group="IC")],
    }

    fitted = _fit_signal_labels_on_pin_exits([rail, label], endpoints, {})

    assert fitted[0] == rail
    assert 1_270_000 <= fitted[1].position.x < 10_000_000
    assert fitted[1].position.y == label.position.y
    assert fitted[1].rotation == 0  # Translation is enough; retain reading direction.
    assert _clear_signal_stub_corridors(fitted, endpoints) == fitted


@pytest.mark.parametrize("side,dx,dy", [("left", -1, 0), ("right", 1, 0),
                                      ("top", 0, -1), ("bottom", 0, 1)])
def test_label_can_extend_its_own_stub_to_clear_a_component_caption(side, dx, dy):
    pin = _PlacedEndpoint(Vector2(0, 0), side, group="IC")
    label = _NetSymbolTarget(
        "DATA", "DATA", Vector2(dx * 2_540_000, dy * 2_540_000),
        0, False, False, text_alignment="right" if dx < 0 else "left", group="IC",
    )
    obstruction = _Envelope(-5_000_000, -5_000_000, 5_000_000, 5_000_000)
    fitted = _fit_signal_labels_on_pin_exits(
        [label], {"DATA": [pin]}, {}, {"IC": [obstruction]},
    )[0]
    assert fitted.position.x * dy == fitted.position.y * dx
    assert dx * fitted.position.x + dy * fitted.position.y > 2_540_000
    assert fitted.net_name == label.net_name
    assert fitted.rotation == 0


def test_label_ignores_captions_in_a_different_block():
    label = _NetSymbolTarget("DATA", "DATA", Vector2(2_540_000, 0),
                             0, False, False, text_alignment="left", group="IC")
    endpoints = {"DATA": [_PlacedEndpoint(Vector2(0, 0), "right", group="IC")]}
    assert _fit_signal_labels_on_pin_exits(
        [label], endpoints, {}, {"OTHER": [_Envelope(0, -5_000_000, 20_000_000, 5_000_000)]},
    ) == [label]


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("rail_side", [-1, 1])
def test_vertical_pin_label_can_change_side_without_bending_neighbouring_rail(direction, rail_side):
    side = "top" if direction == -1 else "bottom"
    rail = _NetSymbolTarget("RAIL", "RAIL", Vector2.from_xy_mm(rail_side * 2.54, direction * 2.54),
                            0, True, direction == 1, group="IC")
    label = _NetSymbolTarget("SENSE", "SENSE", Vector2.from_xy_mm(0, direction * 2.54),
                             0, False, False,
                             text_alignment="left" if rail_side == 1 else "right", group="IC")
    endpoints = {
        "RAIL": [_PlacedEndpoint(Vector2.from_xy_mm(rail_side * 2.54, 0), side, group="IC")],
        "SENSE": [_PlacedEndpoint(Vector2(0, 0), side, group="IC")],
    }
    glyphs = {direction == 1: _Envelope(-1_270_000, min(0, direction * 2_540_000),
                                        1_270_000, max(0, direction * 2_540_000))}

    fitted = _fit_signal_labels_on_pin_exits([rail, label], endpoints, glyphs)

    assert fitted[0] == rail
    assert fitted[1].position == label.position
    assert fitted[1].rotation == 0
    assert fitted[1].text_alignment == ("right" if rail_side == 1 else "left")
    assert _clear_signal_stub_corridors(fitted, endpoints, glyphs) == fitted


def test_rail_caption_can_slide_down_beside_its_glyph_to_clear_the_approach_wire():
    glyph = _Envelope(-1_270_000, 0, 1_270_000, 2_540_000)
    original = _Envelope(-6_286_500, 3_175_000, 6_286_500, 4_445_000)
    wires = [_Envelope(-5_230_000, -150_000, -4_930_000, 10_000_000),
             _Envelope(-150_000, -150_000, 2_690_000, 150_000)]
    clear = [original.translated(offset)
             for offset in _rail_caption_offsets(original, glyph, 1_270_000)
             if all(_envelopes_do_not_overlap(original.translated(offset), wire, 952_500)
                    for wire in wires)]
    assert clear
    assert clear[0].min_x > glyph.max_x
    assert glyph.min_y <= clear[0].center_y <= glyph.max_y


def test_rail_caption_uses_obstacle_edges_between_fixed_candidate_rows():
    glyph = _Envelope(-762_000, -2_540_000, 762_000, 0)
    original = _Envelope(-4_000_000, -4_445_000, 4_000_000, -3_175_000)
    obstacles = (
        (_Envelope(-2_690_000, -2_690_000, 150_000, -2_390_000), 952_500),
        (_Envelope(-6_000_000, -300_000, -1_500_000, 300_000), 250_000),
        (_Envelope(1_500_000, -10_000_000, 10_000_000, 10_000_000), 250_000),
    )
    original_choices = _rail_caption_offsets(original, glyph, 1_270_000)
    choices = _rail_caption_offsets(original, glyph, 1_270_000, obstacles)
    clear = [offset for offset in choices if offset not in original_choices
             and all(_envelopes_do_not_overlap(original.translated(offset), box, gap)
                     for box, gap in obstacles)]
    assert clear


@pytest.mark.parametrize("ground,direction", [(True, 1), (False, -1)])
def test_rail_glyph_clears_adjacent_label_even_when_attachment_point_is_clear(ground, direction):
    rail = _NetSymbolTarget("RAIL", "RAIL", Vector2.from_xy_mm(10, 10),
                            0, True, ground, group="IC")
    label = _NetSymbolTarget("DATA", "DATA", Vector2.from_xy_mm(10, 10 + 2.54 * direction),
                             0, False, False, text_alignment="right", group="IC")
    endpoints = {"RAIL": [_PlacedEndpoint(Vector2.from_xy_mm(12.54, 10),
                                          "left", group="IC")]}
    glyph = _Envelope(-1_270_000, min(0, direction * 2_540_000),
                       1_270_000, max(0, direction * 2_540_000))

    adjusted = _clear_signal_stub_corridors([rail, label], endpoints, {ground: glyph})

    assert adjusted[0].position.x < 3_000_000
    assert adjusted[0].position.y == rail.position.y
    assert adjusted[1] == label


def test_aligned_opposed_pins_route_without_a_dogleg() -> None:
    items = _route_group(
        [
            _PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right"),
            _PlacedEndpoint(Vector2.from_xy_mm(30, 20), "left"),
        ]
    )

    lines = [item for item in items if isinstance(item, SchematicLine)]
    assert not any(isinstance(item, Junction) for item in items)
    assert len(lines) == 3
    assert all(start[1] == end[1] == 20 for start, end in map(_line_mm, lines))


def test_perpendicular_pin_escape_does_not_leave_a_tail_beyond_its_junction():
    items = _route_group([
        _PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right"),
        _PlacedEndpoint(Vector2.from_xy_mm(30, 20), "left"),
        _PlacedEndpoint(Vector2.from_xy_mm(20, 18.73), "bottom"),
    ])
    lines = [item for item in items if isinstance(item, SchematicLine)]
    assert max(max(line.start.y, line.end.y) for line in lines) == 20_000_000
    assert any(isinstance(item, Junction) and item.position == Vector2.from_xy_mm(20, 20)
               for item in items)


def test_vertical_pin_label_is_beside_its_wire_not_centered_across_it():
    target = _NetSymbolTarget("SHIELD", "SHIELD", Vector2(0, 0), 0, False, False)
    endpoint = _PlacedEndpoint(Vector2.from_xy_mm(10, 10), "bottom")
    label = _localize_signal_labels([target], {"SHIELD": [endpoint]})[0]
    assert label.text_alignment == "left"
    assert label.position == Vector2.from_xy_mm(10, 12.54)


def test_label_on_an_inline_local_connection_stays_between_its_pins():
    target = _NetSymbolTarget("BIAS", "BIAS", Vector2(0, 0), 0, False, False)
    pins = [_PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right", group="IC"),
            _PlacedEndpoint(Vector2.from_xy_mm(30, 20), "left", group="IC"),
            _PlacedEndpoint(Vector2.from_xy_mm(50, 20), "left", group="PORT")]
    labels = _localize_signal_labels([target], {"BIAS": pins})
    local = next(t for t in labels if t.group == "IC")
    assert local.position == Vector2.from_xy_mm(12.54, 20)
    assert local.text_alignment == "left"
    fitted = _fit_signal_labels_on_pin_exits(
        labels, {"BIAS": pins}, {},
        {"IC": [_Envelope(10_000_000, 18_000_000, 17_000_000, 21_000_000)]},
    )
    local = next(t for t in fitted if t.group == "IC")
    assert local.position == Vector2.from_xy_mm(27.46, 20)
    assert local.text_alignment == "right"


def test_horizontal_return_bank_does_not_add_an_unnecessary_vertical_stub():
    target = _NetSymbolTarget("GND", "GND", Vector2(0, 0), 0, True, True)
    endpoints = [_PlacedEndpoint(Vector2.from_xy_mm(20, y), "right") for y in (10, 12.54)]
    ground = _localize_rail_symbols([target], {"GND": endpoints})[0]
    assert ground.position == Vector2.from_xy_mm(22.54, 12.54)


@pytest.mark.parametrize("ground", [False, True])
def test_rail_marker_branches_off_a_continuing_vertical_connection(ground):
    target = _NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, ground)
    endpoints = [_PlacedEndpoint(Vector2.from_xy_mm(20, 10), "bottom"),
                 _PlacedEndpoint(Vector2.from_xy_mm(20, 30), "top")]
    rail = _localize_rail_symbols([target], {"RAIL": endpoints})[0]
    assert rail.position.x == 25_080_000
    assert 10_000_000 < rail.position.y < 30_000_000
    assert rail.rotation == 0


@pytest.mark.parametrize("obstacle_side", [-1, 1])
def test_continuing_rail_branch_uses_the_clear_side(obstacle_side):
    target = _NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, False)
    endpoints = {
        "RAIL": [_PlacedEndpoint(Vector2.from_xy_mm(20, 10), "bottom"),
                 _PlacedEndpoint(Vector2.from_xy_mm(20, 30), "top")],
        "OTHER": [_PlacedEndpoint(Vector2.from_xy_mm(20 + obstacle_side * 5.08, 25), "top")],
    }
    rail = _localize_rail_symbols([target], endpoints)[0]
    assert rail.position.x == round((20 - obstacle_side * 5.08) * 1_000_000)
    assert rail.position.y == 20_000_000


def test_unaligned_opposed_pins_use_only_the_required_right_angle() -> None:
    items = _route_group(
        [
            _PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right"),
            _PlacedEndpoint(Vector2.from_xy_mm(30, 30), "left"),
        ]
    )

    lines = [item for item in items if isinstance(item, SchematicLine)]
    vertical = [line for line in lines if line.start.x == line.end.x]
    assert len(lines) == 4
    assert len(vertical) == 1


def test_minority_face_branch_joins_inside_the_dominant_trunk_span() -> None:
    items = _route_group(
        [
            _PlacedEndpoint(Vector2.from_xy_mm(30, 10), "top"),
            _PlacedEndpoint(Vector2.from_xy_mm(40, 30), "bottom"),
            _PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right"),
        ]
    )

    horizontal_at_trunk = [
        _line_mm(item)
        for item in items
        if isinstance(item, SchematicLine)
        and item.start.y == item.end.y == Vector2.from_xy_mm(0, 20).y
    ]
    assert ((12.54, 20.0), (30.0, 20.0)) in horizontal_at_trunk
    assert ((30.0, 20.0), (40.0, 20.0)) in horizontal_at_trunk


def test_perpendicular_shunt_axis_respects_horizontal_pin_exit_clearance():
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(32.54, y), "right") for y in (10, 20, 30)
    ] + [
        _PlacedEndpoint(Vector2.from_xy_mm(48, 10), "left"),
        _PlacedEndpoint(Vector2.from_xy_mm(31.27, 40), "top"),
    ]
    assert _shared_vertical_trunk(endpoints) == Vector2.from_xy_mm(32.54, 0).x


def test_perpendicular_branch_alignment_also_handles_left_facing_banks():
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(20, y), "left") for y in (10, 20, 30)
    ] + [_PlacedEndpoint(Vector2.from_xy_mm(21.27, 40), "top")]
    assert _shared_vertical_trunk(endpoints) == Vector2.from_xy_mm(20, 0).x


def test_aligned_perpendicular_shunt_meets_bank_without_a_sideways_step():
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(30, y), "right") for y in (10, 20, 30)
    ] + [
        _PlacedEndpoint(Vector2.from_xy_mm(50, 10), "left"),
        _PlacedEndpoint(Vector2.from_xy_mm(32.54, 40), "top"),
    ]
    lines = [line for line in _route_group(endpoints) if isinstance(line, SchematicLine)]
    assert all(line.start.x == line.end.x == Vector2.from_xy_mm(32.54, 0).x
               for line in lines if max(line.start.y, line.end.y) > 30_000_000)


def test_layout_kicad_command_requires_an_explicit_output() -> None:
    parser = build_parser()

    args = parser.parse_args(
        [
            "layout-kicad",
            "Board.zen",
            "Board.kicad_sch",
            "--output",
            "proposed.kicad_sch",
        ]
    )

    assert args.command == "layout-kicad"
    assert args.output.name == "proposed.kicad_sch"


def test_one_physical_components_units_form_one_ordered_stack() -> None:
    positions = [_ordered_unit_position(120.0, 80.0, index) for index in range(7)]

    assert {x for x, _ in positions} == {120.0}
    assert [round(y, 1) for _, y in positions] == [
        80.0,
        232.4,
        384.8,
        537.2,
        689.6,
        842.0,
        994.4,
    ]


def test_drawing_origin_is_derived_only_from_visible_targets() -> None:
    translation = _drawing_translation([(100, -400, 0), (300, 200, 0)])

    assert translation == (0, 500)


def test_top_level_blocks_pack_to_landscape_without_resizing_or_seed_constraints() -> None:
    mm = 1_000_000
    envelopes = {
        "input": _Envelope(0, 0, 10 * mm, 20 * mm),
        "controller": _Envelope(100 * mm, 40 * mm, 140 * mm, 100 * mm),
        "audio": _Envelope(300 * mm, -80 * mm, 350 * mm, -30 * mm),
        "usb": _Envelope(320 * mm, 240 * mm, 360 * mm, 270 * mm),
    }

    deltas = _packed_group_deltas(envelopes, "controller", clearance_mm=20)
    packed = {group: envelope.translated(deltas[group]) for group, envelope in envelopes.items()}

    for name, box in packed.items():
        assert (box.width, box.height) == (envelopes[name].width, envelopes[name].height)
        assert all(_envelopes_do_not_overlap(box, other, 20 * mm)
                   for peer, other in packed.items() if peer != name)
    width = max(box.max_x for box in packed.values()) - min(box.min_x for box in packed.values())
    height = max(box.max_y for box in packed.values()) - min(box.min_y for box in packed.values())
    assert 1.2 <= width / height <= 1.6
    shifted = {name: box.translated(Vector2(-500 * mm, 200 * mm))
               for name, box in envelopes.items()}
    shifted_deltas = _packed_group_deltas(shifted, "controller", clearance_mm=20)
    assert packed == {name: box.translated(shifted_deltas[name]) for name, box in shifted.items()}


def test_wide_functional_block_does_not_force_a_panorama():
    mm = 1_000_000
    sizes = {"controller": (130, 104), "power": (400, 115), "filter": (60, 30)}
    sizes.update({f"port-{i}": (45, 40) for i in range(6)})
    envelopes = {name: _Envelope(0, 0, w * mm, h * mm) for name, (w, h) in sizes.items()}
    connectors = frozenset(name for name in sizes if name.startswith("port"))
    deltas = _packed_group_deltas(envelopes, "controller", connector_groups=connectors)
    packed = {name: box.translated(deltas[name]) for name, box in envelopes.items()}
    width = max(b.max_x for b in packed.values()) - min(b.min_x for b in packed.values())
    height = max(b.max_y for b in packed.values()) - min(b.min_y for b in packed.values())
    assert 1.2 <= width / height <= 1.6
    functional_bottom = max(box.max_y for name, box in packed.items() if name not in connectors)
    assert all(packed[name].min_y >= functional_bottom + 20_320_000 for name in connectors)


def test_connector_bank_wraps_instead_of_widening_an_authored_layout():
    mm = 1_000_000
    envelopes = {"input": _Envelope(0, 0, 40 * mm, 60 * mm),
                 "output": _Envelope(0, 0, 40 * mm, 60 * mm)}
    connectors = frozenset(f"port-{i}" for i in range(8))
    envelopes.update({name: _Envelope(0, 0, 30 * mm, 30 * mm) for name in connectors})
    deltas = _packed_group_deltas(envelopes, "input", connector_groups=connectors,
                                 right_of=(("output", "input"),))
    packed = {name: box.translated(deltas[name]) for name, box in envelopes.items()}
    assert len({packed[name].min_y for name in connectors}) > 1
    assert all(packed["input"].min_x <= packed[name].min_x
               and packed[name].max_x <= packed["output"].max_x for name in connectors)


@pytest.mark.parametrize("right_of", [(), (("audio", "controller"),)])
def test_connector_only_groups_are_packed_after_functional_groups(right_of) -> None:
    mm = 1_000_000
    envelopes = {
        "controller": _Envelope(100 * mm, 40 * mm, 140 * mm, 100 * mm),
        "audio": _Envelope(300 * mm, 20 * mm, 350 * mm, 70 * mm),
        "panel": _Envelope(-500 * mm, -300 * mm, -470 * mm, -260 * mm),
    }

    deltas = _packed_group_deltas(
        envelopes,
        "controller",
        clearance_mm=20,
        connector_groups=frozenset({"panel"}),
        right_of=right_of,
    )
    packed = {group: envelope.translated(deltas[group]) for group, envelope in envelopes.items()}
    functional_bottom = max(packed["controller"].max_y, packed["audio"].max_y)

    assert packed["panel"].min_y == functional_bottom + 20 * mm
    assert packed["panel"].center_x == pytest.approx(
        (min(packed["controller"].min_x, packed["audio"].min_x)
         + max(packed["controller"].max_x, packed["audio"].max_x))
        / 2
    )


def test_authored_group_stages_include_connector_predecessors() -> None:
    mm = 1_000_000
    envelopes = {
        "controller": _Envelope(100 * mm, 40 * mm, 140 * mm, 100 * mm),
        "audio": _Envelope(-300 * mm, -80 * mm, -250 * mm, -30 * mm),
        "usb": _Envelope(320 * mm, 240 * mm, 360 * mm, 270 * mm),
        "panel": _Envelope(-500 * mm, -300 * mm, -470 * mm, -260 * mm),
    }

    deltas = _packed_group_deltas(
        envelopes,
        "controller",
        clearance_mm=20,
        connector_groups=frozenset({"panel"}),
        right_of=(
            ("controller", "panel"),
            ("audio", "controller"),
            ("usb", "controller"),
        ),
    )
    packed = {group: envelope.translated(deltas[group]) for group, envelope in envelopes.items()}

    assert packed["controller"].min_x == packed["panel"].max_x + 20 * mm
    assert packed["audio"].min_x == packed["usb"].min_x
    assert packed["audio"].min_x == packed["controller"].max_x + 20 * mm
    assert packed["audio"].max_y + 20 * mm == packed["usb"].min_y


@pytest.mark.parametrize("right,bottom,page", [
    (287, 200, "A4"),
    (287.001, 200, "A3"),
    (287, 200.001, "A3"),
    (410, 287, "A3"),
    (410.001, 287, "A2"),
    (410, 287.001, "A2"),
    (584, 410, "A2"),
    (831, 584, "A1"),
    (1179, 831, "A0"),
])
def test_sheet_size_is_selected_after_layout_from_content_bounds(right, bottom, page) -> None:
    mm = 1_000_000

    settings = _standard_page_for_bounds(
        _Envelope(10 * mm, 10 * mm, round(right * mm), round(bottom * mm)),
    )

    assert settings.page_size == page
    assert settings.orientation == "landscape"


def test_named_signal_endpoints_receive_separate_outward_labels() -> None:
    target = _NetSymbolTarget(
        net_name="CONTROL",
        display_name="CONTROL",
        position=Vector2.from_xy_mm(30, 20),
        rotation=0,
        rail=False,
        ground=False,
    )
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(10, 20), "left", group="input"),
        _PlacedEndpoint(Vector2.from_xy_mm(40, 30), "right", group="output"),
    ]

    adjusted = _localize_signal_labels(
        [target],
        {"CONTROL": endpoints},
    )

    assert [item.position for item in adjusted] == [
        Vector2.from_xy_mm(7.46, 20),
        Vector2.from_xy_mm(42.54, 30),
    ]
    assert [item.text_alignment for item in adjusted] == ["right", "left"]


@pytest.mark.parametrize("seed_count", [1, 2])
def test_one_functional_group_uses_real_wires_instead_of_duplicate_labels(seed_count) -> None:
    target = _NetSymbolTarget(
        net_name="LOCAL_CONTROL",
        display_name="LOCAL_CONTROL",
        position=Vector2.from_xy_mm(30, 20),
        rotation=0,
        rail=False,
        ground=False,
    )
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right", group="controller"),
        _PlacedEndpoint(Vector2.from_xy_mm(30, 20), "left", group="controller"),
    ]

    assert _localize_signal_labels([target] * seed_count, {"LOCAL_CONTROL": endpoints}) == []


def test_same_face_rail_cluster_does_not_span_an_intervening_signal_exit():
    template = _NetSymbolTarget("VDD", "VDD", Vector2(0, 0), 0, True, False)
    pins = [_PlacedEndpoint(Vector2.from_xy_mm(10, y), "right", "device", "block",
                            rail_class="signal-tie") for y in (10, 20)]
    signal = _PlacedEndpoint(Vector2.from_xy_mm(10, 15), "right", "device", "block")
    rails = _localize_rail_symbols([template], {"VDD": pins, "DATA": [signal]})
    assert len(rails) == 2


def test_authored_support_bank_can_share_rail_beyond_pairwise_cluster_distance():
    template = _NetSymbolTarget("VDD", "VDD", Vector2(0, 0), 0, True, False)
    core = _PlacedEndpoint(Vector2.from_xy_mm(40, 10), "left", "core", "block",
                            rail_class="supply")
    caps = [_PlacedEndpoint(Vector2.from_xy_mm(x, 10), "top", f"cap{x}", "block",
                            rail_class="supply", bank=("core", "input")) for x in (20, 0)]
    rails = _localize_rail_symbols([template], {"VDD": [core, *caps]})
    assert len(rails) == 1
    assert len(rails[0].members) == 3


def test_named_net_keeps_local_branches_without_wrapping_around_device():
    target = _NetSymbolTarget("REF", "REF", Vector2(0, 0), 0, False, False)
    left = _PlacedEndpoint(Vector2.from_xy_mm(10, 30), "left", "core", "block")
    right = _PlacedEndpoint(Vector2.from_xy_mm(30, 10), "right", "core", "block")
    local = _PlacedEndpoint(Vector2.from_xy_mm(5, 30), "bottom", "shunt", "block")
    below = _PlacedEndpoint(Vector2.from_xy_mm(40, 25), "right", "part", "block")
    barrier = _PlacedEndpoint(Vector2.from_xy_mm(30, 15), "right", "core", "block")

    labels = _localize_signal_labels([target], {"REF": [left, right, local, below],
                                                "OUTPUT": [barrier]})

    assert len(labels) == 3
    assert any(label.members == (left, local) for label in labels)
    assert any(label.members == (right,) for label in labels)
    assert any(label.members == (below,) for label in labels)
    # Caption fitting must not silently rejoin the three regions.
    fitted = _fit_signal_labels_on_pin_exits(labels,
                                             {"REF": [left, right, local, below]}, {})
    assert [label.members for label in fitted] == [label.members for label in labels]


@pytest.mark.parametrize("series_x", [30, 34.92])
def test_named_reference_does_not_cross_a_neighbouring_local_branch(series_x):
    target = _NetSymbolTarget("REF", "REF", Vector2(0, 0), 0, False, False)
    left = _PlacedEndpoint(Vector2.from_xy_mm(40, 20), "left", "core", "block")
    right = _PlacedEndpoint(Vector2.from_xy_mm(60, 0), "right", "core", "block")
    shunt = _PlacedEndpoint(Vector2.from_xy_mm(20, 17), "bottom", "shunt", "block")
    reference = _PlacedEndpoint(Vector2.from_xy_mm(10, 30), "left", "cap", "block")
    signal = [
        _PlacedEndpoint(Vector2.from_xy_mm(40, 17), "left", "core", "block"),
        _PlacedEndpoint(Vector2.from_xy_mm(series_x, 17), "right", "series", "block"),
        _PlacedEndpoint(Vector2.from_xy_mm(15, 30), "right", "cap", "block"),
    ]
    labels = _localize_signal_labels([target], {"REF": [left, right, shunt, reference],
                                                "CONTROL": signal})
    assert any(label.members == (left,) for label in labels)


def test_label_fitting_does_not_extend_a_separated_stub_across_local_wiring():
    pin = _PlacedEndpoint(Vector2.from_xy_mm(40, 20), "left", "core", "block")
    label = _NetSymbolTarget("REFERENCE", "LONG_REFERENCE_NET", Vector2.from_xy_mm(37.46, 20),
                             0, False, False, "right", group="block", members=(pin,))
    control = [
        _PlacedEndpoint(Vector2.from_xy_mm(40, 17.46), "left", "core", "block"),
        _PlacedEndpoint(Vector2.from_xy_mm(34.92, 17.46), "right", "series", "block"),
        _PlacedEndpoint(Vector2.from_xy_mm(15, 30), "right", "cap", "block"),
    ]
    fitted = _fit_signal_labels_on_pin_exits(
        [label], {"REFERENCE": [pin], "CONTROL": control}, {},
        {"block": [_Envelope(20_000_000, 16_000_000, 36_000_000, 21_000_000)]},
    )[0]
    assert fitted.position.x > 37_460_000
    assert fitted.rotation in (90, 270)
    box = _envelope_from_points(_text_envelope(Text(
        fitted.display_name, fitted.position,
        TextAttributes(Vector2.from_xy_mm(1.27, 1.27), fitted.rotation,
                       fitted.text_alignment, "bottom"),
    )))
    assert all(not _segment_hits_box(w.start, w.end, box)
               for w in _route_group(control) if isinstance(w, SchematicLine))


@pytest.mark.parametrize("body", [None, _Envelope(20_500_000, 9_000_000,
                                                 22_500_000, 11_000_000)])
def test_logic_tie_glyph_does_not_touch_a_separate_same_net_supply_wire(body):
    supply = _PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "device", "block",
                              rail_class="supply")
    bypass = _PlacedEndpoint(Vector2.from_xy_mm(20, 10), "left", "cap", "block",
                              rail_class="supply")
    tie = _PlacedEndpoint(Vector2.from_xy_mm(10, 12.54), "right", "device", "block",
                           rail_class="signal-tie")
    rails = [_NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(15, y),
                              0, True, False, group="block", members=members)
             for y, members in [(10, (supply, bypass)), (12.54, (tie,))]]
    glyph = _Envelope(-1_270_000, -2_540_000, 1_270_000, 0)
    adjusted = _clear_signal_stub_corridors(rails, {"VDD": [supply, bypass, tie]},
                                            {False: glyph}, {"block": [body] if body else []})
    assert not _segment_hits_box(supply.position, bypass.position,
                                 glyph.translated(adjusted[1].position))
    if body:
        assert _envelopes_do_not_overlap(glyph.translated(adjusted[1].position), body, 635_000)


def test_shared_signal_gets_one_label_per_functional_group() -> None:
    target = _NetSymbolTarget(
        net_name="RESET",
        display_name="RESET",
        position=Vector2.from_xy_mm(30, 20),
        rotation=0,
        rail=False,
        ground=False,
    )
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(10, 20), "right", group="controller"),
        _PlacedEndpoint(Vector2.from_xy_mm(15, 30), "top", group="controller"),
        _PlacedEndpoint(Vector2.from_xy_mm(50, 20), "left", group="panel"),
    ]

    adjusted = _localize_signal_labels([target], {"RESET": endpoints})

    assert len(adjusted) == 2
    assert {item.owner for item in adjusted} == {None}
    assert {(item.position.x, item.position.y) for item in adjusted} == {
        (Vector2.from_xy_mm(17.54, 20).x, Vector2.from_xy_mm(17.54, 20).y),
        (Vector2.from_xy_mm(47.46, 20).x, Vector2.from_xy_mm(47.46, 20).y),
    }


def test_unselected_duplicate_net_symbol_is_removed() -> None:
    near = _NetSymbolTarget(
        "GND",
        "GND",
        Vector2.from_xy_mm(10, 10),
        0,
        True,
        True,
    )
    orphan = _NetSymbolTarget(
        "GND",
        "GND",
        Vector2.from_xy_mm(100, 100),
        0,
        True,
        True,
    )

    retained = _prune_unused_targets(
        [near, orphan],
        {"GND": [_PlacedEndpoint(Vector2.from_xy_mm(12, 10), "left", "root.U1")]},
    )

    assert retained == [near]


def test_rail_pin_on_opposite_face_receives_a_local_vertical_termination() -> None:
    target = _NetSymbolTarget(
        net_name="VDD",
        display_name="VDD",
        position=Vector2.from_xy_mm(5, 20),
        rotation=0,
        rail=True,
        ground=False,
    )
    endpoint = _PlacedEndpoint(
        Vector2.from_xy_mm(20, 20),
        "right",
        "root.POWER.U1",
    )

    adjusted = _split_rails_across_component_faces([target], {"VDD": [endpoint]})

    assert [item.position for item in adjusted] == [
        Vector2.from_xy_mm(5, 20),
        Vector2.from_xy_mm(22.54, 14.92),
    ]


def test_nearby_ground_endpoints_share_one_south_facing_local_symbol() -> None:
    template = _NetSymbolTarget(
        "GND",
        "GND",
        Vector2.from_xy_mm(100, 100),
        90,
        True,
        True,
    )
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(10, 10), "bottom", "root.U1"),
        _PlacedEndpoint(Vector2.from_xy_mm(20, 12), "bottom", "root.C1"),
    ]

    adjusted = _localize_rail_symbols([template], {"GND": endpoints})

    assert len(adjusted) == 1
    assert adjusted[0].position == Vector2.from_xy_mm(15, 14.54)
    assert adjusted[0].rotation == 0


@pytest.mark.parametrize("ground,direction", [(True, 1), (False, -1)])
def test_nearby_rail_pins_do_not_merge_through_a_different_net(ground, direction):
    template = _NetSymbolTarget("RAIL", "RAIL", Vector2(0, 0), 0, True, ground)
    endpoints = {
        "RAIL": [
            _PlacedEndpoint(Vector2.from_xy_mm(20, 10 * direction), "left", "root.T1"),
            _PlacedEndpoint(Vector2.from_xy_mm(10, 30 * direction),
                            "bottom" if ground else "top", "root.R1"),
        ],
        "SIGNAL": [
            _PlacedEndpoint(Vector2.from_xy_mm(10, 20 * direction),
                            "top" if ground else "bottom", "root.R1"),
        ],
    }

    targets = _localize_rail_symbols([template], endpoints)

    assert len(targets) == 2
    assert {target.owner for target in targets} == {"root.T1", "root.R1"}


def test_distant_supply_endpoints_receive_separate_north_facing_symbols() -> None:
    template = _NetSymbolTarget(
        "VDD",
        "VDD",
        Vector2.from_xy_mm(100, 100),
        90,
        True,
        False,
    )
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(10, 30), "top", "root.U1"),
        _PlacedEndpoint(Vector2.from_xy_mm(60, 30), "top", "root.U2"),
    ]

    adjusted = _localize_rail_symbols([template], {"VDD": endpoints})

    assert [item.position for item in adjusted] == [
        Vector2.from_xy_mm(10, 27.46),
        Vector2.from_xy_mm(60, 27.46),
    ]
    assert all(item.rotation == 0 for item in adjusted)


def test_bypass_supply_termination_stays_directly_above_owner_pin():
    template = _NetSymbolTarget("VDD", "VDD", Vector2(0, 0), 0, True, False)
    endpoints = [
        _PlacedEndpoint(Vector2.from_xy_mm(10, 30), "top", "root.U1"),
        _PlacedEndpoint(Vector2.from_xy_mm(15.08, 24.92), "left", "root.C1"),
    ]

    targets = _localize_rail_symbols([template], {"VDD": endpoints})

    assert len(targets) == 1
    assert targets[0].position == Vector2.from_xy_mm(10, 24.92)


def test_separated_channels_do_not_push_supply_symbols_off_their_pin_axes():
    targets = [
        _NetSymbolTarget(net, net, Vector2.from_xy_mm(10, y), 0, True, False, group="BANK")
        for net, y in [("VDD_A", 10), ("VDD_B", 50), ("VDD_B", 90)]
    ]
    endpoints = {
        "VDD_A": [_PlacedEndpoint(Vector2.from_xy_mm(10, 15), "top", group="BANK")],
        "VDD_B": [_PlacedEndpoint(Vector2.from_xy_mm(10, y), "top", group="BANK")
                  for y in (55, 95)],
    }
    assert _separate_same_face_rail_corridors(targets, endpoints) == targets


def test_simple_south_facing_ground_uses_only_one_normal_stub():
    template = _NetSymbolTarget("GND", "GND", Vector2(0, 0), 0, True, True)
    pin = _PlacedEndpoint(Vector2.from_xy_mm(10, 30), "bottom", "root.U1")

    targets = _localize_rail_symbols([template], {"GND": [pin]})

    assert targets[0].position == Vector2.from_xy_mm(10, 32.54)
    wires = [line for line in _route_group([pin, _PlacedEndpoint(targets[0].position)])
             if isinstance(line, SchematicLine)]
    assert len(wires) == 1
    assert _line_mm(wires[0]) == ((10, 30), (10, 32.54))


def test_distinct_rails_on_one_component_face_use_separate_corridors() -> None:
    supply = _NetSymbolTarget(
        "VDD",
        "VDD",
        Vector2.from_xy_mm(100, 100),
        0,
        True,
        False,
    )
    ground = _NetSymbolTarget(
        "GND",
        "GND",
        Vector2.from_xy_mm(100, 100),
        0,
        True,
        True,
    )
    owner = "root.J1"
    group = "INPUT"
    endpoints = {
        "VDD": [
            _PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", owner, group),
            _PlacedEndpoint(Vector2.from_xy_mm(10, 12.54), "right", owner, group),
        ],
        "GND": [
            _PlacedEndpoint(Vector2.from_xy_mm(10, 15.08), "right", owner, group),
            _PlacedEndpoint(Vector2.from_xy_mm(10, 17.62), "right", owner, group),
        ],
    }

    localized = _localize_rail_symbols([supply, ground], endpoints)
    adjusted = _separate_same_face_rail_corridors(localized, endpoints)

    assert adjusted[0].position.x == Vector2.from_xy_mm(12.54, 0).x
    assert adjusted[1].position.x == Vector2.from_xy_mm(15.08, 0).x
    assert adjusted[0].owner == adjusted[1].owner == owner
    assert adjusted[0].group == adjusted[1].group == group


def test_moving_symbol_preserves_fields_until_geometry_pass_repositions_them() -> None:
    text_size = Vector2.from_xy_mm(1.27, 1.27)
    symbol = SchematicSymbolInstance(
        id="symbol",
        zener_path="BLOCK.R1",
        library_id="Device:R",
        unit=1,
        position=Vector2.from_xy_mm(10, 10),
        transform=SchematicSymbolTransform(0),
        fields=[
            SchematicField(
                "Reference",
                Text("R1", Vector2.from_xy_mm(10, 8), TextAttributes(text_size, 90)),
            ),
            SchematicField(
                "Value",
                Text("10k", Vector2.from_xy_mm(10, 12), TextAttributes(text_size, 90)),
            ),
        ],
    )

    _translate_symbol(symbol, Vector2.from_xy_mm(20, 20), 90)

    assert symbol.reference_field.text.position == Vector2.from_xy_mm(20, 18)
    assert symbol.value_field.text.position == Vector2.from_xy_mm(20, 22)


def test_vertical_two_terminal_caption_is_stacked_beside_the_body() -> None:
    text_size = Vector2.from_xy_mm(1.27, 1.27)
    symbol = SchematicSymbolInstance(
        id="symbol",
        zener_path="BLOCK.D1",
        library_id="Device:D_Schottky",
        unit=1,
        position=Vector2.from_xy_mm(20, 20),
        transform=SchematicSymbolTransform(270),
        fields=[
            SchematicField(
                "Reference",
                Text("D1", Vector2.from_xy_mm(20, 18), TextAttributes(text_size, 270)),
            ),
            SchematicField(
                "Value",
                Text("SS14", Vector2.from_xy_mm(20, 22), TextAttributes(text_size, 270)),
            ),
        ],
    )

    _position_compact_two_terminal_fields(
        symbol,
        [Vector2.from_xy_mm(19, 19), Vector2.from_xy_mm(21, 21)],
        {"1": "top", "2": "bottom"},
        text_size.y,
    )

    assert symbol.reference_field.text.position.x > Vector2.from_xy_mm(21, 0).x
    assert symbol.value_field.text.position.x == symbol.reference_field.text.position.x
    assert symbol.reference_field.text.position.y < symbol.value_field.text.position.y
    assert symbol.reference_field.text.attributes.angle == 90
    assert symbol.value_field.text.attributes.angle == 90
    assert symbol.reference_field.text.attributes.horizontal_alignment == "left"
