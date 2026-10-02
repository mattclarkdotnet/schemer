from __future__ import annotations

import pytest

from schemer.analysis.circuits import (
    component_layout_groups,
)
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
)
from schemer.kicad.geometry.text import (
    field_draw_angle,
    oriented_field_text,
    text_envelope,
)
from schemer.kicad.items import (
    SchematicField,
    SchematicLine,
    SchematicSymbolInstance,
    SchematicSymbolTransform,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.native.annotations.fields import (
    _position_compact_two_terminal_fields,
    position_bank_fields,
    position_component_fields,
)
from schemer.native.routing import (
    route_group,
)
from schemer.native.routing_model import PlacedEndpoint
from schemer.native.routing_paths import segment_hits_box
from tests.support.schematic import SCHEMATIC, _support_fixture


@pytest.mark.parametrize("half_width", [1.27, 10])
def test_component_caption_preserves_normal_line_spacing(half_width):
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        "(start -1.27 -1.27) (end 1.27 1.27)",
        f"(start {-half_width} -1.27) (end {half_width} 1.27)",
    ))
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.transform.orientation = 0
    editor.update_items(symbol)

    positioned = position_component_fields(editor, [symbol])[0]

    reference = positioned.reference_field.text
    value = positioned.value_field.text
    assert reference.position.x == value.position.x == symbol.position.x
    assert value.position.y - reference.position.y == 2 * value.attributes.size.y
    body_top = min(
        p.y for p in placed_symbol_body_positions(editor.document, editor.document.symbols[0])
    )
    assert text_envelope(value)[1].y < body_top


def test_stacked_pin_axis_parts_get_captions_on_their_own_rows():
    _, editor = _support_fixture(None)
    symbols = editor.get_symbols()
    for symbol, y in zip(symbols, (25.4, 27.94), strict=True):
        symbol.transform.orientation = 0
        symbol.position = Vector2.from_xy_mm(20.32, y)
    editor.update_items(symbols)
    symbols = position_component_fields(editor, symbols)

    position_bank_fields(editor, symbols, {})

    for symbol in symbols:
        reference = symbol.reference_field.text
        value = symbol.value_field.text
        assert reference.position.y == value.position.y == symbol.position.y - 1_270_000
        assert reference.position.x < value.position.x
    assert (
        symbols[1].reference_field.text.position.y - symbols[0].reference_field.text.position.y
        == 2_540_000
    )


def test_bank_caption_slots_survive_return_trunk_routing():
    from schemer.kicad.geometry.envelopes import envelope_from_points
    from schemer.native.obstacles import label_layout_obstacles
    _, editor = _support_fixture(None)
    symbols = editor.get_symbols()
    for symbol, y in zip(symbols, (25.4, 27.94), strict=True):
        symbol.transform.orientation = 0
        symbol.position = Vector2.from_xy_mm(20.32, y)
    editor.update_items(symbols)
    symbols = position_component_fields(editor, symbols)
    position_bank_fields(editor, symbols, {})
    editor.update_items(symbols)
    groups = {s.id: "bank" for s in symbols}
    obstacles = label_layout_obstacles(editor, groups)["bank"]
    pins = [PlacedEndpoint(placed_pin_positions(editor.document, s)["2"], "right")
            for s in editor.document.symbols]
    route = route_group([*pins, PlacedEndpoint(Vector2.from_xy_mm(35, 35))],
                         obstacles=obstacles)
    captions = [envelope_from_points(text_envelope(f.text, s.transform.orientation))
                for s in symbols for f in (s.reference_field, s.value_field)]
    assert route
    assert not any(segment_hits_box(w.start, w.end, box)
                   for w in route if isinstance(w, SchematicLine) for box in captions)


@pytest.mark.parametrize("vertical", [False, True])
def test_bank_caption_yields_to_a_foreign_pin_before_becoming_a_route_obstacle(vertical):
    from schemer.kicad.geometry.envelopes import envelope_from_points, envelopes_do_not_overlap
    from schemer.native.annotations.fields import _clear_bank_caption_pin_exits
    from schemer.native.obstacles import label_layout_obstacles
    from schemer.native.routing import pin_contact_obstacle
    _, editor = _support_fixture(None)
    symbols = editor.get_symbols()
    bank, neighbour = symbols
    bank.position = Vector2.from_xy_mm(20, 20)
    neighbour.position = Vector2.from_xy_mm(35, 20)
    bank.transform.orientation = 0
    neighbour.transform.orientation = 90
    for field, x in ((bank.reference_field, 24), (bank.value_field, 28)):
        field.text.position = Vector2.from_xy_mm(x, 22.54)
        field.text = oriented_field_text(field.text, 0, 0, "left")
    bank.value = "100nF 50V X7R"
    if vertical:
        for symbol in symbols:
            symbol.position = Vector2(symbol.position.y, -symbol.position.x)
            symbol.transform.orientation += 90
            for field in (symbol.reference_field, symbol.value_field):
                field.text.position = Vector2(field.text.position.y, -field.text.position.x)
                field.text = oriented_field_text(
                    field.text, symbol.transform.orientation, 90, "left")
    editor.update_items(symbols)
    before = [(s.position, s.transform.orientation) for s in symbols]
    pair_delta = (bank.value_field.text.position.x - bank.reference_field.text.position.x,
                  bank.value_field.text.position.y - bank.reference_field.text.position.y)
    groups = {s.id: "circuit" for s in symbols}
    raw = next(s for s in editor.document.symbols if s.uuid == neighbour.id)
    pins = [PlacedEndpoint(p, placed_pin_sides(editor.document, raw)[n])
            for n, p in placed_pin_positions(editor.document, raw).items()]
    def boxes():
        return [envelope_from_points(text_envelope(f.text, bank.transform.orientation))
                for f in (bank.reference_field, bank.value_field)]
    assert any(not envelopes_do_not_overlap(b, pin_contact_obstacle(p), 250_000)
               for b in boxes() for p in pins)
    _clear_bank_caption_pin_exits(editor, symbols, groups)
    assert all(envelopes_do_not_overlap(b, pin_contact_obstacle(p), 250_000)
               for b in boxes() for p in pins)
    assert before == [(s.position, s.transform.orientation) for s in symbols]
    assert pair_delta == (bank.value_field.text.position.x - bank.reference_field.text.position.x,
                          bank.value_field.text.position.y - bank.reference_field.text.position.y)
    editor.update_items(symbols)
    obstacles = label_layout_obstacles(editor, groups)["circuit"]
    # Once reserved, the moved captions no longer make a real terminal open.
    from schemer.native.routing import _clear_pin_escape
    assert all(_clear_pin_escape(p, obstacles, []) for p in pins)


@pytest.mark.parametrize("same_group", [True, False])
def test_dense_vertical_bank_uses_readable_caption_columns_without_moving_parts(same_group):
    _, editor = _support_fixture(None)
    symbols = editor.get_symbols()
    for symbol, x in zip(symbols, (20.32, 25.4), strict=True):
        symbol.transform.orientation = 90
        symbol.position = Vector2.from_xy_mm(x, 30)
        symbol.value = "100nF 50V X7R"
    editor.update_items(symbols)
    symbols = position_component_fields(editor, symbols)
    before = [(s.position, s.transform.orientation) for s in symbols]
    sizes = [(s.reference_field.text.attributes.size, s.value_field.text.attributes.size)
             for s in symbols]
    groups = {s.id: "bank" if same_group else str(i) for i, s in enumerate(symbols)}
    position_bank_fields(editor, symbols, {}, component_groups=groups)
    assert [(s.position, s.transform.orientation) for s in symbols] == before
    assert [(s.reference_field.text.attributes.size, s.value_field.text.attributes.size)
            for s in symbols] == sizes
    for s in symbols:
        a, b = s.reference_field.text, s.value_field.text
        if same_group:
            assert a.position.x == b.position.x
            assert a.position.y > b.position.y
            assert field_draw_angle(a.attributes.angle, s.transform.orientation) == 90
        else:
            assert field_draw_angle(a.attributes.angle, s.transform.orientation) == 0


@pytest.mark.parametrize("pitch", [2.54, 5.08, 10.16])
def test_bank_reserves_caption_width_without_changing_its_pin_rows(pitch):
    _, editor = _support_fixture(None)
    bank = editor.get_symbols()
    for symbol, y in zip(bank, (25.4, 25.4 + pitch), strict=True):
        symbol.transform.orientation = 0
        symbol.position = Vector2.from_xy_mm(30.48, y)
    editor.update_items(bank)
    raw = editor.document.symbols[0].expression
    source = editor.get_as_string()
    owner = source[raw.start:raw.end].replace("11111111", "bbbbbbbb")
    owner = owner.replace('"R1"', '"R3"').replace(
        "(at 30.48 25.4 0)", "(at 35.56 25.4 0)",
    )
    editor = FileSchematic.from_text(source[:source.rfind(")")] + owner + ")")
    symbols = position_component_fields(editor, list(editor.get_symbols()))

    position_bank_fields(editor, symbols, {s.id: ("bank", "", "", "") for s in bank})

    moved = symbols[:2]
    assert moved[0].position.x == moved[1].position.x < 30_480_000
    assert [s.position.y for s in moved] == [25_400_000, round((25.4 + pitch) * 1_000_000)]
    assert symbols[2].position == Vector2.from_xy_mm(35.56, 25.4)
    right = text_envelope(moved[0].value_field.text, moved[0].transform.orientation)[1].x
    assert right + 635_000 == 33_020_000


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("separation", [0, 500])
def test_connected_composition_clears_terminals_and_compacts_only_opted_in_circuits(
    connected, separation,
):
    from schemer.analysis.circuits import direct_circuit_groups
    from schemer.kicad.geometry.envelopes import envelope_from_points, envelopes_do_not_overlap
    from schemer.native.reuse import compact_connected_circuits

    schematic, editor = _support_fixture("shunt")
    if connected:
        schematic["instances"]["root"]["kind"] = "Module"
        schematic["instances"]["root"]["attributes"] = {
            "schematic_properties": {"Json": {"representation": "connected-circuit"}}}
    a, b = editor.get_symbols()
    b.position = Vector2(a.position.x + separation * 1_000_000, a.position.y)
    editor.update_items(b)
    editor.update_items(position_component_fields(editor, list(editor.get_symbols())))
    before = editor.get_as_string()
    groups, _ = component_layout_groups(schematic)
    compact_connected_circuits(schematic, editor, groups, direct_circuit_groups(schematic))
    if not connected:
        assert editor.get_as_string() == before
        return
    boxes = [envelope_from_points([
        *placed_symbol_body_positions(editor.document, s),
        *placed_pin_positions(editor.document, s).values(),
    ]) for s in editor.document.symbols]
    assert envelopes_do_not_overlap(*boxes, 2_540_000)
    assert max(b.max_x for b in boxes) - min(b.min_x for b in boxes) < 100_000_000


def test_independent_compaction_preserves_separate_origins_before_caption_fitting():
    from schemer.kicad.items import place_symbol
    from schemer.native.reuse import compact_connected_circuits

    schematic, editor = _support_fixture(None)
    schematic["instances"]["root.BIAS"]["attributes"] = {}
    a, b = editor.get_symbols()
    place_symbol(b, Vector2(a.position.x + 200_000_000, a.position.y),
                      a.transform.orientation)
    editor.update_items(b)
    editor.update_items(position_component_fields(editor, list(editor.get_symbols())))
    before = {s.reference: s.position for s in editor.get_symbols()}
    groups = {"root.OWNER": "first", "root.BIAS": "second"}
    compact_connected_circuits(schematic, editor, groups, frozenset(groups.values()))
    after = {s.reference: s.position for s in editor.get_symbols()}
    assert after["R2"].x - after["R1"].x == before["R2"].x - before["R1"].x
    assert after["R2"].y == after["R1"].y


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_vertical_two_terminal_caption_is_stacked_beside_the_body(rotation) -> None:
    text_size = Vector2.from_xy_mm(1.27, 1.27)
    symbol = SchematicSymbolInstance(
        id="symbol",
        zener_path="BLOCK.D1",
        library_id="Device:D_Schottky",
        unit=1,
        position=Vector2.from_xy_mm(20, 20),
        transform=SchematicSymbolTransform(rotation),
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
    assert symbol.reference_field.text.attributes.angle == (-rotation) % 180
    assert symbol.value_field.text.attributes.angle == (-rotation) % 180
    # Stored justification flips; the effective caption still starts here.
    for field in (symbol.reference_field, symbol.value_field):
        low, high = text_envelope(field.text, rotation)
        assert low.x == field.text.position.x
        assert high.x > low.x
