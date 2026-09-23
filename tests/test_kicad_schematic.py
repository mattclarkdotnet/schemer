from __future__ import annotations

import pytest

from schemer.kicad_api import (
    FileSchematic,
    GlobalLabel,
    Junction,
    LocalLabel,
    NoConnectMarker,
    PageSettings,
    SchematicLine,
    Text,
    TextAttributes,
    Vector2,
)
from schemer.kicad_bridge import associate_components
from schemer.kicad_geometry import (
    placed_pin_positions,
    placed_pin_segments,
    placed_pin_sides,
    placed_symbol_body_positions,
    symbol_library_pins,
)
from schemer.kicad_layout import (
    _align_perpendicular_branches,
    _align_same_face_terminal_exits,
    _align_single_pin_attachments,
    _clear_final_label_wires,
    _clear_rail_attachment_exits,
    _compact_inline_connections,
    _completed_group_envelopes,
    _component_net_endpoints,
    _content_envelope,
    _NetSymbolTarget,
    _pack_completed_groups,
    _page_settings_for_content,
    _pin_number_envelopes,
    _pin_stroke_envelopes,
    _place_net_symbols,
    _place_owned_pin_networks,
    _place_owned_shunt_banks,
    _place_top_pin_bypasses,
    _place_vertical_pin_bias_branches,
    _PlacedEndpoint,
    _position_bank_fields,
    _position_component_fields,
    _resolve_symbol_field_overlaps,
    _route_group,
    _segment_hits_box,
    _space_labeled_pin_connections,
    _text_envelope,
    _validate_owned_networks,
)
from schemer.kicad_schematic import KiCadSchematicDocument, KiCadSchematicError, _descendant

SCHEMATIC = """(kicad_sch
  (version 20260306)
  (generator "eeschema")
  (paper "A1")
  (lib_symbols
    (symbol "Device:R"
      (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
      (symbol "R_1_1"
        (rectangle (start -1.27 -1.27) (end 1.27 1.27)
          (stroke (width 0.254) (type default)) (fill (type none)))
        (pin passive line (at -2.54 0 0) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "1" (effects (font (size 1.27 1.27)))))
        (pin passive line (at 2.54 0 180) (length 1.27)
          (name "~" (effects (font (size 1.27 1.27))))
          (number "2" (effects (font (size 1.27 1.27))))))))
  (symbol
    (lib_id "Device:R")
    (at 20.32 30.48 90)
    (unit 1)
    (uuid "11111111-1111-1111-1111-111111111111")
    (property "Path" "FILTER.R_INPUT.R" (at 20.32 30.48 0) (hide yes)
      (effects (font (size 1.27 1.27))))
    (property "Reference" "R1" (at 21.59 29.21 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 21.59 31.75 0)
      (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "11111111-1111-1111-1111-111111111101"))
    (pin "2" (uuid "11111111-1111-1111-1111-111111111102")))
  (symbol
    (lib_id "74xx:74HC14")
    (at 40.64 30.48 0)
    (unit 2)
    (uuid "22222222-2222-2222-2222-222222222222")
    (property "Path" "BUFFER.U1" (at 40.64 30.48 0) (hide yes)
      (effects (font (size 1.27 1.27))))
    (property "Reference" "U1" (at 40.64 27.94 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "74HC14" (at 40.64 33.02 0)
      (effects (font (size 1.27 1.27)))))
  (symbol
    (lib_id "power:VCC")
    (at 40.64 20.32 0)
    (unit 1)
    (uuid "77777777-7777-7777-7777-777777777777")
    (property "Reference" "#PWR01" (at 40.64 24.13 0) (hide yes)
      (effects (font (size 1.27 1.27))))
    (property "Value" "VCC" (at 40.64 22.86 0)
      (effects (font (size 1.27 1.27)))))
  (wire
    (pts (xy 20.32 30.48) (xy 40.64 30.48))
    (stroke (width 0) (type default))
    (uuid "33333333-3333-3333-3333-333333333333"))
  (label "FILTER_OUT" (at 30.48 30.48 0)
    (effects (font (size 1.27 1.27)))
    (uuid "44444444-4444-4444-4444-444444444444"))
  (global_label "VCC" (shape input) (at 40.64 20.32 90)
    (effects (font (size 1.27 1.27)))
    (uuid "55555555-5555-5555-5555-555555555555"))
  (junction (at 40.64 30.48)
    (diameter 0)
    (color 0 0 0 0)
    (uuid "88888888-8888-8888-8888-888888888888"))
  (no_connect (at 45.72 30.48) (uuid "66666666-6666-6666-6666-666666666666")))
"""


def test_page_settings_round_trip_through_kicad_11_shaped_adapter() -> None:
    editor = FileSchematic.from_text(SCHEMATIC)

    assert editor.get_page_settings() == PageSettings("A1", "landscape")

    editor.set_page_settings(PageSettings("A3", "landscape"))

    assert editor.get_page_settings() == PageSettings("A3", "landscape")
    assert '(paper "A3")' in editor.get_as_string()
    assert 'property "Path" "FILTER.R_INPUT.R"' in editor.get_as_string()


@pytest.mark.parametrize("position,rotation,text_angle", [
    ((268, 30), 0, 0),
    ((20, 180), 0, 270),
    ((20, 180), 90, 180),
])
def test_sheet_selection_contains_final_aligned_and_rotated_captions(
    position, rotation, text_angle,
):
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.transform.orientation = rotation
    symbol.value_field.text.value = "V" * 20
    symbol.value_field.text.position = Vector2.from_xy_mm(*position)
    symbol.value_field.text.attributes.angle = text_angle
    symbol.value_field.text.attributes.horizontal_alignment = "left"
    editor.update_items(symbol)
    before = editor.get_as_string()

    envelope = _content_envelope(editor)
    assert envelope == _completed_group_envelopes(editor, {symbol.id: "block"})["block"]
    settings = _page_settings_for_content(editor)
    assert settings == PageSettings("A3", "landscape")
    assert editor.get_as_string() == before
    editor.set_page_settings(settings)
    assert editor.get_as_string() == before.replace('(paper "A1")', '(paper "A3")')


@pytest.mark.parametrize("initial_page", ["A0", "A4"])
def test_sheet_selection_ignores_seed_page_and_preserves_packed_items(initial_page):
    editor = FileSchematic.from_text(SCHEMATIC.replace('(paper "A1")', f'(paper "{initial_page}")'))
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    _pack_completed_groups(editor, {symbol.id: "block"}, "block")
    before = editor.get_as_string()

    settings = _page_settings_for_content(editor)
    assert settings == PageSettings("A4", "landscape")
    editor.set_page_settings(settings)
    assert editor.get_as_string() == before.replace(f'(paper "{initial_page}")', '(paper "A4")')


def test_symbol_field_can_be_hidden_without_changing_its_value() -> None:
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = next(item for item in editor.get_symbols() if item.zener_path == "BUFFER.U1")
    reference = symbol.reference_field
    reference.visible = False

    editor.update_items(symbol)

    updated = next(item for item in editor.get_symbols() if item.zener_path == "BUFFER.U1")
    assert updated.reference == "U1"
    assert updated.reference_field.visible is False


def test_inventory_reads_only_top_level_schematic_objects() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)

    assert document.version == "20260306"
    assert len(document.symbols) == 3
    assert len(document.wires) == 1
    assert len(document.labels) == 2
    assert len(document.junctions) == 1
    assert len(document.no_connects) == 1
    assert document.symbols[0].path == "FILTER.R_INPUT.R"
    assert document.symbols[0].reference == "R1"
    assert document.symbols[0].position == (20.32, 30.48)
    assert document.symbols[0].rotation == 90
    assert document.symbols[1].unit == 2
    assert document.symbols[2].path is None
    assert document.wires[0].points == ((20.32, 30.48), (40.64, 30.48))
    assert [(label.kind, label.text) for label in document.labels] == [
        ("label", "FILTER_OUT"),
        ("global_label", "VCC"),
    ]


def test_summary_distinguishes_component_units_from_power_symbols() -> None:
    summary = KiCadSchematicDocument.from_text(SCHEMATIC).summary()

    assert summary["symbol_count"] == 3
    assert summary["component_symbol_count"] == 2
    assert summary["component_path_count"] == 2
    assert summary["power_symbol_count"] == 1


def test_embedded_library_pin_geometry_resolves_placed_pin_positions() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)
    symbol = document.symbols[0]

    assert set(symbol_library_pins(document, symbol)) == {"1", "2"}
    assert placed_pin_positions(document, symbol) == {
        "1": Vector2.from_xy_mm(20.32, 33.02),
        "2": Vector2.from_xy_mm(20.32, 27.94),
    }
    assert placed_pin_sides(document, symbol) == {"1": "bottom", "2": "top"}


def test_embedded_library_body_geometry_is_not_reduced_to_pin_axis() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)
    symbol = document.symbols[0]

    body = placed_symbol_body_positions(document, symbol)

    assert min(point.x for point in body) == Vector2.from_xy_mm(19.05, 0).x
    assert max(point.x for point in body) == Vector2.from_xy_mm(21.59, 0).x
    assert min(point.y for point in body) == Vector2.from_xy_mm(0, 29.21).y
    assert max(point.y for point in body) == Vector2.from_xy_mm(0, 31.75).y


@pytest.mark.parametrize("rotation,mirror,expected", [
    (0, "", ((17.78, 30.48), (19.05, 30.48))),
    (90, "", ((20.32, 33.02), (20.32, 31.75))),
    (180, "", ((22.86, 30.48), (21.59, 30.48))),
    (270, "", ((20.32, 27.94), (20.32, 29.21))),
    (0, "(mirror y)", ((22.86, 30.48), (21.59, 30.48))),
])
def test_pin_strokes_include_the_authored_length_after_rotation_and_mirroring(
    rotation, mirror, expected,
):
    source = SCHEMATIC.replace('(at 20.32 30.48 90)', f'(at 20.32 30.48 {rotation}) {mirror}')
    document = KiCadSchematicDocument.from_text(source)
    assert placed_pin_segments(document, document.symbols[0])["1"] == tuple(
        Vector2.from_xy_mm(*point) for point in expected
    )


def test_foreign_route_cannot_cross_a_pin_stroke_away_from_its_endpoint():
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.document.symbols[0]
    stroke = _pin_stroke_envelopes(editor, symbol)["1"]
    endpoints = [_PlacedEndpoint(Vector2.from_xy_mm(10, 32.4), "right"),
                 _PlacedEndpoint(Vector2.from_xy_mm(30, 32.4), "left")]

    wires = [w for w in _route_group(endpoints, obstacles=[stroke])
             if isinstance(w, SchematicLine)]

    assert wires
    assert all(not _segment_hits_box(w.start, w.end, stroke) for w in wires)
    assert all(any(p.position in (w.start, w.end) for w in wires) for p in endpoints)


def test_component_endpoints_carry_the_full_pin_stroke_to_routing():
    schematic, editor = _support_fixture(None)
    endpoints = _component_net_endpoints(schematic, editor)
    for pins in endpoints.values():
        for pin in pins:
            assert pin.stroke is not None
            assert max(pin.stroke.width, pin.stroke.height) == 1_770_000


@pytest.mark.parametrize("rotation, first", [
    (0, (17.78, 29.21)), (90, (19.05, 33.02)),
    (180, (22.86, 31.75)), (270, (21.59, 27.94)),
])
def test_asymmetric_pin_follows_kicad_counterclockwise_rotation(rotation, first):
    source = SCHEMATIC.replace("(at 20.32 30.48 90)", f"(at 20.32 30.48 {rotation})")
    source = source.replace("(at -2.54 0 0)", "(at -2.54 1.27 0)")
    document = KiCadSchematicDocument.from_text(source)
    assert placed_pin_positions(document, document.symbols[0])["1"] == Vector2.from_xy_mm(*first)


def test_caption_bounds_do_not_give_electrical_blocks_fractional_micrometre_offsets():
    editor = FileSchematic.from_text(SCHEMATIC)
    part = editor.get_symbols()[0]
    editor.remove_items([s for s in editor.get_symbols() if s.id != part.id])
    part.value_field.text.position = Vector2(-100_000_750, -100_000_750)
    editor.update_items(part)
    before = {s.id: s.position for s in editor.get_symbols()}

    _pack_completed_groups(editor, {item.id: "block" for item in editor.get_items()}, "block")

    for symbol in editor.get_symbols():
        assert (symbol.position.x - before[symbol.id].x) % 1000 == 0
        assert (symbol.position.y - before[symbol.id].y) % 1000 == 0


def test_clear_captions_above_and_below_a_part_do_not_move_as_a_filled_box():
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        '(symbol "Device:R"', '(symbol "Device:R" (pin_numbers hide)',
    ))
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.transform.orientation = 0
    for field, y in [(symbol.reference_field, 27.94), (symbol.value_field, 33.02)]:
        field.text.position = Vector2.from_xy_mm(20.32, y)
        field.text.attributes.angle = 0
        field.text.attributes.horizontal_alignment = "center"
    editor.update_items(symbol)
    resolved = _resolve_symbol_field_overlaps(editor, [symbol])[0]
    assert resolved.reference_field.text.position == symbol.reference_field.text.position
    assert resolved.value_field.text.position == symbol.value_field.text.position


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

    positioned = _position_component_fields(editor, [symbol])[0]

    reference = positioned.reference_field.text
    value = positioned.value_field.text
    assert reference.position.x == value.position.x == symbol.position.x
    assert value.position.y - reference.position.y == 2 * value.attributes.size.y
    body_top = min(
        p.y for p in placed_symbol_body_positions(editor.document, editor.document.symbols[0])
    )
    assert _text_envelope(value)[1].y < body_top


def test_stacked_pin_axis_parts_get_captions_on_their_own_rows():
    _, editor = _support_fixture(None)
    symbols = editor.get_symbols()
    for symbol, y in zip(symbols, (25.4, 27.94), strict=True):
        symbol.transform.orientation = 0
        symbol.position = Vector2.from_xy_mm(20.32, y)
    editor.update_items(symbols)
    symbols = _position_component_fields(editor, symbols)

    _position_bank_fields(editor, symbols, set())

    for symbol in symbols:
        reference = symbol.reference_field.text
        value = symbol.value_field.text
        assert reference.position.y == value.position.y == symbol.position.y - 1_270_000
        assert reference.position.x < value.position.x
    assert (
        symbols[1].reference_field.text.position.y - symbols[0].reference_field.text.position.y
        == 2_540_000
    )


def test_long_power_caption_clears_a_continuing_wire_without_moving_geometry():
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.reference_field.visible = False
    symbol.value_field.text.value = "LONG_SUPPLY_RAIL"
    symbol.value_field.text.position = Vector2.from_xy_mm(20.32, 25.4)
    symbol.value_field.text.attributes.angle = 270
    symbol.value_field.text.attributes.horizontal_alignment = "center"
    editor.update_items(symbol)
    line = SchematicLine(
        id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        start=Vector2.from_xy_mm(20.32, 10.16),
        end=Vector2.from_xy_mm(20.32, 50.8),
    )
    editor.create_items(line)
    original_geometry = (symbol.position, symbol.transform.orientation, line.start, line.end)

    resolved = _resolve_symbol_field_overlaps(editor, [])[0]

    low, high = _text_envelope(resolved.value_field.text, resolved.transform.orientation)
    assert high.x < line.start.x or low.x > line.start.x
    assert (
        resolved.position, resolved.transform.orientation, line.start, line.end
    ) == original_geometry
    assert abs(resolved.value_field.text.position.x - symbol.position.x) > 5_080_000


def test_pin_number_clearance_uses_font_height_and_respects_hidden_numbers():
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.document.symbols[0]
    boxes = _pin_number_envelopes(editor, symbol)
    # Rotated numbers paint almost 2 mm to the left of a vertical pin,
    # outside the old fixed 1 mm corridor.
    assert boxes and min(box.min_x for box in boxes) < 20_320_000 - 1_900_000
    hidden = FileSchematic.from_text(SCHEMATIC.replace(
        '(symbol "Device:R"', '(symbol "Device:R" (pin_numbers (hide yes))',
    ))
    assert _pin_number_envelopes(hidden, hidden.document.symbols[0]) == []


def test_clear_but_detached_rail_caption_returns_to_its_own_glyph():
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        '(symbol "Device:R"', '(symbol "Device:R" (pin_numbers hide)',
    ))
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.reference_field.visible = False
    symbol.value_field.text.value = "SUPPLY_RAIL"
    symbol.value_field.text.position = Vector2.from_xy_mm(25.4, 20.32)
    symbol.value_field.text.attributes.angle = 270
    editor.update_items(symbol)

    resolved = _resolve_symbol_field_overlaps(editor, [])[0]

    assert resolved.value_field.text.position.y == symbol.position.y
    low, _ = _text_envelope(resolved.value_field.text, resolved.transform.orientation)
    body_right = max(p.x for p in placed_symbol_body_positions(
        editor.document, editor.document.symbols[0],
    ))
    assert low.x == body_right + 1_270_000
    assert resolved.position == symbol.position


def test_failed_caption_placement_is_reported(monkeypatch, caplog):
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    monkeypatch.setattr("schemer.kicad_layout._rail_caption_offsets", lambda *args: [])
    _resolve_symbol_field_overlaps(editor, [])
    assert "No clear caption position for R1" in caplog.text


def test_two_pin_caption_tries_actual_obstacle_edges_beyond_the_grid_search(caplog):
    editor = FileSchematic.from_text(SCHEMATIC)
    part = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != part.id])
    part.transform.orientation = 0
    part.value = "LONG_CAPACITOR_VALUE_123456"
    editor.update_items(part)
    part = _position_component_fields(editor, [part])[0]
    editor.update_items(part)
    wires = [SchematicLine(id=f"trunk-{x}", start=Vector2.from_xy_mm(x, 0),
                           end=Vector2.from_xy_mm(x, 80)) for x in (10, 30)]
    editor.create_items(wires)

    resolved = _resolve_symbol_field_overlaps(editor, [part])[0]

    assert "No clear caption position" not in caplog.text
    assert resolved.position == part.position
    for field in (resolved.reference_field, resolved.value_field):
        low, high = _text_envelope(field.text, resolved.transform.orientation)
        assert all(high.x < w.start.x - 250_000 or low.x > w.start.x + 250_000
                   for w in wires)


def test_long_caption_bounds_clear_a_body_corner_at_full_character_pitch():
    editor = FileSchematic.from_text(SCHEMATIC)
    part = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != part.id])
    part.transform.orientation = 0
    part.value = "XQX12-XYZ034-99"
    # Put the value across the body's upper-right corner. The old 0.9-pitch
    # estimate incorrectly reported clear space to the right of the body.
    size = part.value_field.text.attributes.size.x
    x = 21_590_000 + round(len(part.value) * size * 0.47)
    part.value_field.text.position = Vector2(x, 29_210_000)
    part.reference_field.text.position = Vector2(x, 26_670_000)
    for field in (part.reference_field, part.value_field):
        field.text.attributes.angle = 0
        field.text.attributes.horizontal_alignment = "center"
    editor.update_items(part)

    resolved = _resolve_symbol_field_overlaps(editor, [part])[0]

    assert resolved.value_field.text.position != part.value_field.text.position
    assert resolved.position == part.position
    body = placed_symbol_body_positions(editor.document, editor.document.symbols[0])
    for field in (resolved.reference_field, resolved.value_field):
        low, high = _text_envelope(field.text)
        assert (
            low.x >= max(p.x for p in body) + 250_000
            or high.x <= min(p.x for p in body) - 250_000
            or low.y >= max(p.y for p in body) + 250_000
            or high.y <= min(p.y for p in body) - 250_000
        )


def test_wire_label_vertical_alignment_round_trips_and_bounds_stay_above_wire():
    editor = FileSchematic.from_text(SCHEMATIC)
    label = LocalLabel(id="", position=Vector2.from_xy_mm(15, 20), text=Text(
        "DATA", Vector2.from_xy_mm(15, 20),
        TextAttributes(Vector2.from_xy_mm(1.27, 1.27), 0, "left", "bottom"),
    ))
    created = editor.create_items(label)[0]
    saved = next(item for item in editor.get_labels() if item.id == created.id)
    assert saved.text.attributes.vertical_alignment == "bottom"
    low, high = _text_envelope(saved.text)
    assert low.y == 18_730_000
    assert high.y == saved.position.y
    saved.text.attributes.horizontal_alignment = "right"
    editor.update_items(saved)
    saved = next(item for item in editor.get_labels() if item.id == created.id)
    assert saved.text.attributes.horizontal_alignment == "right"
    assert saved.text.attributes.vertical_alignment == "bottom"
    saved.text.attributes.vertical_alignment = "top"
    editor.update_items(saved)
    saved = next(item for item in editor.get_labels() if item.id == created.id)
    assert _text_envelope(saved.text)[0].y == saved.position.y


def test_no_connect_cross_is_a_fixed_caption_obstacle():
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.reference_field.visible = False
    symbol.value_field.text.position = Vector2.from_xy_mm(20.32, 20.32)
    symbol.value_field.text.attributes.angle = 270
    editor.update_items(symbol)
    marker = NoConnectMarker(id="caption-nc", position=Vector2.from_xy_mm(20.32, 20.32))
    editor.create_items(marker)

    resolved = _resolve_symbol_field_overlaps(editor, [symbol])[0]

    low, high = _text_envelope(resolved.value_field.text, resolved.transform.orientation)
    assert (high.x < marker.position.x - 635_000 or low.x > marker.position.x + 635_000
            or high.y < marker.position.y - 635_000 or low.y > marker.position.y + 635_000)


def test_bank_caption_can_slide_less_than_a_grid_step_to_clear_a_marker(caplog):
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        '(symbol "Device:R"', '(symbol "Device:R" (pin_numbers hide)',
    ))
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.transform.orientation = 0
    _position_bank_fields(editor, [symbol], {symbol.id})
    editor.update_items(symbol)
    value_right = _text_envelope(symbol.value_field.text)[1].x
    marker = NoConnectMarker(id="adjacent-pin-nc", position=Vector2(
        value_right + 635_000, symbol.value_field.text.position.y - 1_270_000,
    ))
    wire = SchematicLine(id="bank-wire", start=Vector2.from_xy_mm(22.86, 30.48),
                         end=Vector2(marker.position.x, symbol.position.y))
    editor.create_items([marker, wire])

    resolved = _resolve_symbol_field_overlaps(editor, [symbol])[0]

    shift = resolved.reference_field.text.position.x - symbol.reference_field.text.position.x
    assert -1_270_000 < shift < 0
    for before, after in zip(symbol.fields, resolved.fields, strict=True):
        if before.name in {"Reference", "Value"}:
            assert after.text.position == Vector2(
                before.text.position.x + shift, before.text.position.y,
            )
            assert after.text.attributes == before.text.attributes
    assert resolved.position == symbol.position
    assert editor.get_no_connects()[0].position == marker.position
    assert editor.get_lines()[0].start == wire.start
    assert editor.get_lines()[0].end == wire.end
    assert "No clear caption position" not in caplog.text


@pytest.mark.parametrize("diameter, painted_diameter", [(0, 0.9144), (0.4, 0.4), (2.54, 2.54)])
def test_caption_clearance_includes_junction_dot(diameter, painted_diameter):
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.reference_field.visible = False
    symbol.value_field.text.position = Vector2.from_xy_mm(20.32, 20.32)
    symbol.value_field.text.attributes.angle = 270
    editor.update_items(symbol)
    junction = Junction(id="caption-dot", position=Vector2.from_xy_mm(20.32, 21.59))
    editor.create_items(junction)
    editor = FileSchematic.from_text(editor.get_as_string().replace(
        "(diameter 0)", f"(diameter {diameter})",
    ))

    resolved = _resolve_symbol_field_overlaps(editor, [symbol])[0]

    low, high = _text_envelope(resolved.value_field.text, resolved.transform.orientation)
    radius = round(painted_diameter * 500_000)
    assert (high.x + 250_000 <= junction.position.x - radius
            or low.x - 250_000 >= junction.position.x + radius
            or high.y + 250_000 <= junction.position.y - radius
            or low.y - 250_000 >= junction.position.y + radius)
    if diameter == 0.4:
        assert resolved.value_field.text.position == symbol.value_field.text.position


def test_bank_reserves_caption_width_without_changing_its_pin_rows():
    _, editor = _support_fixture(None)
    bank = editor.get_symbols()
    for symbol, y in zip(bank, (25.4, 27.94), strict=True):
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
    symbols = _position_component_fields(editor, list(editor.get_symbols()))

    _position_bank_fields(editor, symbols, {s.id for s in bank})

    moved = symbols[:2]
    assert moved[0].position.x == moved[1].position.x < 30_480_000
    assert [s.position.y for s in moved] == [25_400_000, 27_940_000]
    assert symbols[2].position == Vector2.from_xy_mm(35.56, 25.4)
    right = _text_envelope(moved[0].value_field.text, moved[0].transform.orientation)[1].x
    assert right + 635_000 == 33_020_000


def _support_fixture(role):
    editor = FileSchematic.from_text(SCHEMATIC)
    owner = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != owner.id])
    owner.field("Path").text.value = "OWNER"
    editor.update_items(owner)
    raw = editor.document.symbols[0].expression
    text = editor.get_as_string()
    copy = text[raw.start:raw.end].replace("11111111", "aaaaaaaa")
    copy = copy.replace('"OWNER"', '"BIAS"').replace('"R1"', '"R2"')
    copy = copy.replace("(at 20.32 30.48 90)", "(at 20.32 20.32 0)")
    editor = FileSchematic.from_text(text[:text.rfind(")")] + copy + ")")
    schematic = {
        "root_ref": "root",
        "instances": {
            "root": {"children": {}},
            "root.OWNER": {"reference_designator": "R1", "attributes": {
                "__symbol_value": {"String": '(symbol "P" (pin (name "SIG") (number "2")))'},
            }},
            "root.BIAS": {"reference_designator": "R2", "attributes": {
                "schematic_properties": {"Json": {
                    "role": role, "group": "bias", "owner": "R1", "pin": "SIG",
                }},
            }},
        },
        "nets": {
            "sig": {"name": "SIG", "ports": ["root.OWNER.SIG", "root.BIAS.1"]},
            "gnd": {"name": "GND", "ports": ["root.OWNER.1", "root.BIAS.2"]},
        },
    }

    return schematic, editor


def test_folded_symbol_leaves_outward_space_for_facing_terminal():
    schematic, editor = _support_fixture(None)
    text = editor.get_as_string()
    node = editor.document.root.first_list("lib_symbols").first_list("symbol")
    definition = text[node.start:node.end]
    folded = definition.replace('"Device:R"', '"Device:Folded"')
    folded = folded.replace('(at -2.54 0 0)', '(at -1.27 -2.54 90)')
    folded = folded.replace('(at 2.54 0 180)', '(at 1.27 -2.54 90)')
    text = text.replace(definition, definition + folded)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Device:Folded")', 1)
    text = text.replace('(at 20.32 30.48 90)', '(at 20.32 30.48 0)')
    text = text.replace('(at 20.32 20.32 0)', '(at 40.64 30.48 270)')
    editor = FileSchematic.from_text(text)

    _align_same_face_terminal_exits(schematic, editor)

    source, sink = editor.document.symbols
    a = placed_pin_positions(editor.document, source)["2"]
    b = placed_pin_positions(editor.document, sink)["1"]
    assert placed_pin_sides(editor.document, source)["2"] == "bottom"
    assert placed_pin_sides(editor.document, sink)["1"] == "top"
    assert b.y - a.y == 5_080_000


@pytest.mark.parametrize("same_group", [True, False])
def test_final_label_clearance_extends_only_its_own_straight_stub(same_group):
    editor = FileSchematic.from_text(SCHEMATIC)
    editor.remove_items(editor.get_items())
    position = Vector2.from_xy_mm(10, 10)
    items = editor.create_items([
        SchematicLine(id="", start=Vector2.from_xy_mm(10, 5), end=position),
        SchematicLine(id="", start=Vector2.from_xy_mm(6, 8.3),
                      end=Vector2.from_xy_mm(9, 8.3)),
        LocalLabel(id="", position=position, text=Text(
            "DATA", position, TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                                              0, "right", "bottom"))),
    ])
    groups = {item.id: "first" for item in items}
    if not same_group:
        groups[items[1].id] = "second"

    _clear_final_label_wires(editor, groups)

    wire, foreign = editor.get_lines()
    label = editor.get_labels()[0]
    assert wire.start == Vector2.from_xy_mm(10, 5)
    assert wire.end == label.position
    assert label.position.x == position.x
    assert (label.position.y > position.y) is same_group
    assert foreign == items[1]


def test_final_label_clearance_does_not_move_a_junction():
    editor = FileSchematic.from_text(SCHEMATIC)
    editor.remove_items(editor.get_items())
    position = Vector2.from_xy_mm(10, 10)
    editor.create_items([
        SchematicLine(id="", start=Vector2.from_xy_mm(10, 5), end=position),
        SchematicLine(id="", start=position, end=Vector2.from_xy_mm(10, 15)),
        SchematicLine(id="", start=Vector2.from_xy_mm(6, 8.3),
                      end=Vector2.from_xy_mm(9, 8.3)),
        LocalLabel(id="", position=position, text=Text(
            "DATA", position, TextAttributes(Vector2.from_xy_mm(1.27, 1.27),
                                              0, "right", "bottom"))),
    ])
    with pytest.raises(KiCadSchematicError, match="label crosses final wiring"):
        _clear_final_label_wires(editor)


def test_owned_parallel_bank_moves_from_remote_seed_to_its_declared_pin():
    from copy import deepcopy

    schematic, editor = _support_fixture("bypass")
    text = editor.get_as_string()
    node = editor.document.symbols[1].expression
    extra = text[node.start:node.end].replace("aaaaaaaa", "bbbbbbbb")
    extra = extra.replace('"R2"', '"R3"').replace('"BIAS"', '"BIAS2"')
    extra = extra.replace('(at 20.32 20.32 0)', '(at 200 200 0)')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + extra + ")")
    schematic["instances"]["root.BIAS2"] = deepcopy(schematic["instances"]["root.BIAS"])
    schematic["instances"]["root.BIAS2"]["reference_designator"] = "R3"
    schematic["nets"]["sig"]["kind"] = "Power"
    schematic["nets"]["sig"]["ports"].append("root.BIAS2.1")
    schematic["nets"]["gnd"]["ports"].append("root.BIAS2.2")

    _place_owned_shunt_banks(schematic, editor)

    first, second = editor.document.symbols[1:]
    a = placed_pin_positions(editor.document, first)["1"]
    b = placed_pin_positions(editor.document, second)["1"]
    assert a.y == b.y
    assert a.x != b.x
    assert b.x < 100_000_000 and b.y < 100_000_000
    assert placed_pin_sides(editor.document, first)["1"] == "top"
    assert placed_pin_sides(editor.document, second)["1"] == "top"


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

    _align_single_pin_attachments(schematic, editor)
    assert before == {s.reference: (s.position, s.transform.orientation)
                      for s in editor.get_symbols()}
    _place_owned_pin_networks(schematic, editor)

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
    _place_owned_pin_networks(schematic, editor)
    assert after == {s.reference: (s.position, s.transform.orientation)
                     for s in editor.get_symbols()}


def test_native_local_labels_preserve_assigned_terminal_membership():
    _, editor = _support_fixture(None)
    pin = _PlacedEndpoint(Vector2.from_xy_mm(10, 10), "right", "part", "block")
    target = _NetSymbolTarget("REF", "REF", Vector2.from_xy_mm(12.54, 10),
                              0, False, False, group="block", members=(pin,))
    anchors = _place_net_symbols(editor, [target])[3]
    assert anchors[0].members == (pin,)


@pytest.mark.parametrize("role,extra", [
    ("shunt", {"return_pin": "1"}),
    ("pin-bridge", {"other_pin": "1"}),
    ("series", {"order": 0}),
])
def test_owned_network_intent_validates_exact_owner_terminal_nets(role, extra):
    schematic, _ = _support_fixture(role)
    schematic["instances"]["root.OWNER"]["attributes"]["__symbol_value"]["String"] = (
        '(symbol "P" (pin (name "SIG") (number "2")) (pin (name "1") (number "1")))'
    )
    props = schematic["instances"]["root.BIAS"]["attributes"]["schematic_properties"]["Json"]
    props.update(extra)
    _validate_owned_networks(schematic)
    props["pin"] = "MISSING"
    with pytest.raises(KiCadSchematicError, match="invalid owner pin"):
        _validate_owned_networks(schematic)


def test_native_field_placement_and_packing_preserve_fonts_and_symbol_scale():
    _, editor = _support_fixture(None)
    source = editor.get_as_string().replace(
        "(font (size 1.27 1.27))",
        '(font (face "Arial") (size 1.27 1.27) (thickness 0.254) (bold yes))',
    )
    editor = FileSchematic.from_text(source)

    def presentation(document):
        library = document.root.first_list("lib_symbols")
        fonts = {}
        for symbol in document.symbols:
            for field in symbol.fields:
                font = _descendant(field.expression, ("effects", "font"))
                fonts[symbol.uuid, field.name] = document.source[font.start:font.end]
        return document.source[library.start:library.end], fonts

    before = presentation(editor.document)
    symbols = list(editor.get_symbols())
    symbols = _position_component_fields(editor, symbols)
    editor.update_items(symbols)
    editor.update_items(_resolve_symbol_field_overlaps(editor, symbols))
    groups = {symbol.id: str(index) for index, symbol in enumerate(symbols)}
    _pack_completed_groups(editor, groups, "0")

    assert presentation(editor.document) == before


@pytest.mark.parametrize("direction", [-1, 1])
def test_named_local_wire_reserves_space_without_changing_its_pin_row(direction, monkeypatch):
    schematic, editor = _support_fixture(None)
    parts = editor.get_symbols()
    for s, x in zip(parts, (40, 40 + direction * 10), strict=True):
        s.transform.orientation = 0
        s.position = Vector2.from_xy_mm(x, 30.48)
        s.reference_field.visible = s.value_field.visible = False
    editor.update_items(parts)
    geometry = symbol_library_pins
    monkeypatch.setattr("schemer.kicad_layout.symbol_library_pins", lambda doc, s:
                        {"1": None, "2": None, "3": None} if s.reference == "R1"
                        else geometry(doc, s))
    pins = {s.reference: placed_pin_positions(editor.document, s) for s in editor.document.symbols}
    endpoints = {"SIGNAL_NAME": [
        _PlacedEndpoint(pins["R1"]["2" if direction == 1 else "1"],
                        "right" if direction == 1 else "left", "root.OWNER", "IC"),
        _PlacedEndpoint(pins["R2"]["1" if direction == 1 else "2"],
                        "left" if direction == 1 else "right", "root.BIAS", "IC"),
        _PlacedEndpoint(Vector2(0, 0), "left", "root.PORT", "PORT"),
    ]}
    target = _NetSymbolTarget("SIGNAL_NAME", "SIGNAL_NAME", Vector2(0, 0), 0, False, False)
    before = {s.id: (s.position.x, s.position.y) for s in parts}
    assert _space_labeled_pin_connections(schematic, editor, [target], endpoints)
    owner, part = editor.get_symbols()
    assert owner.position == parts[0].position
    assert part.position.y == before[part.id][1]
    assert direction * (part.position.x - before[part.id][0]) > 0


@pytest.mark.parametrize("role,place", [
    ("pulldown", _place_vertical_pin_bias_branches),
    ("bypass", _place_top_pin_bypasses),
])
def test_authored_support_on_top_pin_branches_beside_owner(role, place):
    schematic, editor = _support_fixture(role)
    place(schematic, editor)

    bias = next(symbol for symbol in editor.document.symbols if symbol.reference == "R2")
    pins = placed_pin_positions(editor.document, bias)
    if role == "pulldown":
        assert pins["1"] == Vector2.from_xy_mm(12.7, 25.4)
        assert pins["2"].y > pins["1"].y
    else:
        assert pins["1"] == Vector2.from_xy_mm(25.4, 22.86)
        assert pins["2"].y == pins["1"].y
        assert pins["2"].x > pins["1"].x


@pytest.mark.parametrize("direction", [-1, 1])
def test_side_pin_bypass_clears_a_lower_same_net_logic_tie(direction):
    schematic, editor = _support_fixture("bypass")
    definition = f'''(symbol "Test:IC" (symbol "IC_1_1"
      (pin power_in line (at {direction * 2.54} 0 {180 if direction == 1 else 0})
        (length 1.27) (name "VDD") (number "2"))
      (pin input line (at {direction * 2.54} -2.54 {180 if direction == 1 else 0})
        (length 1.27) (name "EN") (number "1"))))'''
    text = editor.get_as_string().replace("(lib_symbols", "(lib_symbols " + definition, 1)
    text = text.replace('(lib_id "Device:R")', '(lib_id "Test:IC")', 1)
    editor = FileSchematic.from_text(text)
    owner = next(s for s in editor.get_symbols() if s.reference == "R1")
    owner.transform.orientation = 0
    editor.update_items(owner)
    schematic["nets"]["sig"]["ports"].append("root.OWNER.1")
    schematic["nets"]["gnd"]["ports"].remove("root.OWNER.1")
    _place_top_pin_bypasses(schematic, editor)
    owner, cap = [next(s for s in editor.document.symbols if s.reference == ref)
                  for ref in ("R1", "R2")]
    pin = placed_pin_positions(editor.document, owner)["2"]
    cap_pin = placed_pin_positions(editor.document, cap)["1"]
    assert cap_pin == Vector2(pin.x + direction * 5_080_000, pin.y - 5_080_000)


@pytest.mark.parametrize("role", ["series-termination", ""])
def test_clear_inline_wire_shortening_does_not_require_a_role(role):
    schematic, editor = _support_fixture(role)
    for item in editor.get_symbols():
        item.transform.orientation = 0
        if item.reference == "R2":
            item.position = Vector2.from_xy_mm(80, 30.48)
        editor.update_items(item)
    _compact_inline_connections(schematic, editor)
    part = next(item for item in editor.get_symbols() if item.reference == "R2")
    assert part.position == Vector2.from_xy_mm(30.48, 30.48)


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("shared_node", [False, True])
@pytest.mark.parametrize("role", ["series-termination", "series"])
def test_authored_owner_overrides_bridge_ambiguity_and_stale_pin_row(angle, shared_node, role):
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
    _align_single_pin_attachments(schematic, editor)
    if shared_node:
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


@pytest.mark.parametrize("angle,side,rail_x", [(0, "left", 25), (180, "right", 15)])
def test_shared_trunk_preserves_an_outward_attachment_exit(angle, side, rail_x):
    schematic, editor = _support_fixture(None)
    part = next(s for s in editor.get_symbols() if s.reference == "R2")
    part.transform.orientation = angle
    part.position = Vector2.from_xy_mm(20, 10)
    editor.update_items(part)
    raw = next(s for s in editor.document.symbols if s.reference == "R2")
    pin = placed_pin_positions(editor.document, raw)["1"]
    endpoints = {"VDD": [_PlacedEndpoint(pin, side, "root.BIAS")]}
    targets = [_NetSymbolTarget("VDD", "VDD", Vector2.from_xy_mm(rail_x, 0),
                               0, True, False)]

    assert _clear_rail_attachment_exits(schematic, editor, targets, endpoints)

    raw = next(s for s in editor.document.symbols if s.reference == "R2")
    final = placed_pin_positions(editor.document, raw)["1"]
    assert final.y == pin.y
    assert final.x == round((rail_x + (2.54 if side == "left" else -2.54)) * 1_000_000)
    assert raw.rotation == angle


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_straight_attachment_compaction_preserves_all_four_pin_axes(angle):
    schematic, editor = _support_fixture(None)
    symbols = editor.get_symbols()
    for symbol in symbols:
        symbol.transform.orientation = angle
    editor.update_items(symbols)
    owner = next(s for s in editor.document.symbols if s.reference == "R1")
    child = next(s for s in editor.document.symbols if s.reference == "R2")
    a = placed_pin_positions(editor.document, owner)["2"]
    b = placed_pin_positions(editor.document, child)["1"]
    side = placed_pin_sides(editor.document, owner)["2"]
    dx, dy = {"left": (-1, 0), "right": (1, 0),
              "top": (0, -1), "bottom": (0, 1)}[side]
    part = next(s for s in symbols if s.reference == "R2")
    part.position = Vector2(part.position.x + a.x + dx * 60_000_000 - b.x,
                            part.position.y + a.y + dy * 60_000_000 - b.y)
    editor.update_items(part)

    _compact_inline_connections(schematic, editor)

    child = next(s for s in editor.document.symbols if s.reference == "R2")
    b = placed_pin_positions(editor.document, child)["1"]
    assert b == Vector2(a.x + dx * 5_080_000, a.y + dy * 5_080_000)
    assert child.rotation == angle


def test_adjacent_passive_does_not_block_compaction_with_speculative_caption_space():
    schematic, editor = _support_fixture("series-termination")
    text = editor.get_as_string()
    raw = next(s for s in editor.document.symbols if s.reference == "R2")
    expr = raw.expression
    copy = text[expr.start:expr.end].replace("aaaaaaaa", "bbbbbbbb")
    copy = copy.replace('"BIAS"', '"NEIGHBOUR"').replace('"R2"', '"R3"')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + copy + ")")
    for item in editor.get_symbols():
        item.transform.orientation = 0
        if item.reference in {"R2", "R3"}:
            item.position = Vector2.from_xy_mm(80, 30.48 if item.reference == "R2" else 34.29)
        editor.update_items(item)

    _compact_inline_connections(schematic, editor)

    part = next(item for item in editor.get_symbols() if item.reference == "R2")
    assert part.position == Vector2.from_xy_mm(30.48, 30.48)


@pytest.mark.parametrize("obstacle_x,expected_x", [(55, 30.48), (30.48, 80)])
def test_compaction_checks_destination_not_the_path_of_a_drag(obstacle_x, expected_x):
    schematic, editor = _support_fixture(None)
    text = editor.get_as_string()
    raw = next(s for s in editor.document.symbols if s.reference == "R2")
    copy = text[raw.expression.start:raw.expression.end].replace("aaaaaaaa", "bbbbbbbb")
    copy = copy.replace('"BIAS"', '"OBSTACLE"').replace('"R2"', '"R3"')
    editor = FileSchematic.from_text(text[:text.rfind(")")] + copy + ")")
    for item in editor.get_symbols():
        item.transform.orientation = 0
        if item.reference in {"R2", "R3"}:
            item.position = Vector2.from_xy_mm(80 if item.reference == "R2" else obstacle_x, 30.48)
        editor.update_items(item)

    _compact_inline_connections(schematic, editor)

    part = next(item for item in editor.get_symbols() if item.reference == "R2")
    assert part.position == Vector2.from_xy_mm(expected_x, 30.48)


@pytest.mark.parametrize("role", [None, "series", "divider"])
def test_perpendicular_alignment_preserves_authored_paths(role, monkeypatch):
    schematic, editor = _support_fixture(role)
    for item in editor.get_symbols():
        if item.reference == "R2":
            item.position = Vector2.from_xy_mm(50, 60)
            item.transform.orientation = 90
        else:
            item.transform.orientation = 0
        editor.update_items(item)
    endpoint_sets = {}
    for raw in editor.document.symbols:
        positions = placed_pin_positions(editor.document, raw)
        sides = placed_pin_sides(editor.document, raw)
        endpoint_sets[raw.reference] = [
            _PlacedEndpoint(position, sides[number], "root." + raw.path, "")
            for number, position in positions.items()
        ]
    endpoints = endpoint_sets["R1"] + [endpoint_sets["R2"][0]]
    monkeypatch.setattr("schemer.kicad_layout._component_net_endpoints",
                        lambda *args: {"NODE": endpoints})

    _align_perpendicular_branches(schematic, editor)

    part = next(item for item in editor.get_symbols() if item.reference == "R2")
    assert (part.position == Vector2.from_xy_mm(50, 60)) == (role is not None)


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
    _align_single_pin_attachments(schematic, editor)

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
    _align_single_pin_attachments(schematic, editor)
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
        _align_single_pin_attachments(schematic, editor)
        assert next(s for s in editor.get_symbols() if s.reference == "R2").position == (
            Vector2.from_xy_mm(70, 70)
        )


def test_zener_components_associate_by_stable_path() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)
    schematic = {
        "root_ref": "root",
        "instances": {
            "root": {"reference_designator": None},
            "root.FILTER.R_INPUT.R": {"reference_designator": "R1"},
            "root.BUFFER.U1": {"reference_designator": "U1"},
        },
    }

    associations = associate_components(schematic, document)

    assert [(item.path, len(item.symbols)) for item in associations] == [
        ("BUFFER.U1", 1),
        ("FILTER.R_INPUT.R", 1),
    ]


def test_zener_component_association_rejects_missing_paths() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)
    schematic = {
        "root_ref": "root",
        "instances": {
            "root": {"reference_designator": None},
            "root.MISSING.R": {"reference_designator": "R2"},
        },
    }

    with pytest.raises(KiCadSchematicError, match="component identity mismatch"):
        associate_components(schematic, document)


def test_zener_component_association_can_ignore_legacy_service_symbols() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)
    schematic = {
        "root_ref": "root",
        "instances": {
            "root": {"reference_designator": None},
            "root.FILTER.R_INPUT.R": {"reference_designator": "R1"},
        },
    }

    associations = associate_components(schematic, document, allow_unexpected=True)

    assert [item.path for item in associations] == ["FILTER.R_INPUT.R"]


def test_parse_without_edits_preserves_source_exactly() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)

    assert document.source == SCHEMATIC


def test_field_size_edit_changes_only_target_atoms() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)

    edited = document.with_field_size(
        path="FILTER.R_INPUT.R",
        field_names=("Reference", "Value"),
        size_mm=2.54,
    )

    assert edited.count("(size 2.54 2.54)") == 2
    assert edited.count("(size 1.27 1.27)") == SCHEMATIC.count("(size 1.27 1.27)") - 2
    marker = '  (symbol\n    (lib_id "Device:R")'
    source_library, source_instances = SCHEMATIC.split(marker, 1)
    edited_library, edited_instances = edited.split(marker, 1)
    assert edited_library == source_library
    assert edited_instances.replace("(size 2.54 2.54)", "(size 1.27 1.27)") == source_instances


def test_field_size_edit_rejects_unknown_symbol_path() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)

    with pytest.raises(KiCadSchematicError, match="no symbol has Path"):
        document.with_field_size(
            path="MISSING.U1",
            field_names=("Reference",),
            size_mm=2.54,
        )


def test_field_size_edit_rejects_non_positive_size() -> None:
    document = KiCadSchematicDocument.from_text(SCHEMATIC)

    with pytest.raises(KiCadSchematicError, match="text size must be positive"):
        document.with_field_size(
            path="FILTER.R_INPUT.R",
            field_names=("Reference",),
            size_mm=0,
        )


@pytest.mark.parametrize(
    "source, message",
    [
        ("not-a-list", "expected a parenthesized"),
        ("(kicad_pcb (version 1))", "expected kicad_sch"),
        ("(kicad_sch", "unterminated list"),
    ],
)
def test_malformed_documents_are_rejected(source: str, message: str) -> None:
    with pytest.raises(KiCadSchematicError, match=message):
        KiCadSchematicDocument.from_text(source)


def test_kicad_api_facade_uses_nanometres_and_typed_items() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)

    assert Vector2.from_xy_mm(20.32, 30.48) == Vector2(20_320_000, 30_480_000)
    assert len(schematic.get_symbols()) == 3
    assert schematic.get_symbols()[0].reference == "R1"
    assert schematic.get_symbols()[0].value == "10k"
    assert schematic.get_symbols()[0].position == Vector2(20_320_000, 30_480_000)
    assert isinstance(schematic.get_lines()[0], SchematicLine)
    assert [type(label) for label in schematic.get_labels()] == [LocalLabel, GlobalLabel]
    assert len(schematic.get_junctions()) == 1
    assert len(schematic.get_no_connects()) == 1


def test_kicad_api_get_items_by_id_preserves_requested_order() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)

    items = schematic.get_items_by_id(
        [
            "44444444-4444-4444-4444-444444444444",
            "11111111-1111-1111-1111-111111111111",
        ]
    )

    assert [item.id for item in items] == [
        "44444444-4444-4444-4444-444444444444",
        "11111111-1111-1111-1111-111111111111",
    ]


def test_kicad_api_no_op_update_is_byte_exact() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)

    schematic.update_items(schematic.get_items())

    assert schematic.get_as_string() == SCHEMATIC


def test_kicad_api_updates_layout_objects_by_uuid() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)
    symbol = schematic.get_symbols()[0]
    symbol.position = Vector2.from_xy_mm(22.86, 35.56)
    symbol.reference_field.text.position = Vector2.from_xy_mm(24.13, 34.29)
    symbol.value_field.text.position = Vector2.from_xy_mm(24.13, 36.83)
    symbol.reference_field.text.attributes.size = Vector2.from_xy_mm(2.54, 2.54)
    symbol.value_field.text.attributes.size = Vector2.from_xy_mm(2.54, 2.54)
    line = schematic.get_lines()[0]
    line.start = Vector2.from_xy_mm(22.86, 35.56)
    label = schematic.get_labels()[0]
    label.position = Vector2.from_xy_mm(33.02, 35.56)
    label.text.value = "RENAMED"
    label.text.attributes.size = Vector2.from_xy_mm(1.5, 1.5)
    junction = schematic.get_junctions()[0]
    junction.position = Vector2.from_xy_mm(43.18, 35.56)
    no_connect = schematic.get_no_connects()[0]
    no_connect.position = Vector2.from_xy_mm(48.26, 35.56)

    updated = schematic.update_items([symbol, line, label, junction, no_connect])
    reparsed = KiCadSchematicDocument.from_text(schematic.get_as_string())

    assert [item.id for item in updated] == [
        symbol.id,
        line.id,
        label.id,
        junction.id,
        no_connect.id,
    ]
    assert reparsed.symbols[0].position == (22.86, 35.56)
    assert reparsed.symbols[0].field("Reference").position == (24.13, 34.29)  # type: ignore[union-attr]
    assert reparsed.symbols[0].field("Value").position == (24.13, 36.83)  # type: ignore[union-attr]
    assert reparsed.symbols[0].field("Reference").text_size == (2.54, 2.54)  # type: ignore[union-attr]
    assert reparsed.wires[0].points[0] == (22.86, 35.56)
    assert reparsed.labels[0].position == (33.02, 35.56)
    assert reparsed.labels[0].text == "RENAMED"
    assert reparsed.labels[0].text_size == (1.5, 1.5)
    assert reparsed.junctions[0].position == (43.18, 35.56)
    assert reparsed.no_connects[0].position == (48.26, 35.56)


def test_kicad_api_reference_shortcut_updates_reference_field() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)
    symbol = schematic.get_symbols()[0]

    symbol.reference = "R42"
    returned = schematic.update_items(symbol)[0]

    assert returned.reference == "R42"  # type: ignore[union-attr]
    assert '(property "Reference" "R42"' in schematic.get_as_string()


def test_kicad_api_can_center_an_inherited_left_justified_field() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)
    symbol = schematic.get_symbols()[0]

    assert symbol.reference_field.text.attributes.horizontal_alignment == "left"

    symbol.reference_field.text.attributes.horizontal_alignment = "center"
    schematic.update_items(symbol)

    updated = schematic.get_symbols()[0]
    assert updated.reference_field.text.attributes.horizontal_alignment == "center"
    assert '(effects (font (size 1.27 1.27)) (justify left))' not in schematic.get_as_string()


def test_kicad_api_commit_can_be_dropped_or_pushed() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)
    original = schematic.get_as_string()
    commit = schematic.begin_commit()
    line = schematic.get_lines()[0]
    line.end = Vector2.from_xy_mm(50.8, 30.48)
    schematic.update_items(line)

    schematic.drop_commit(commit)

    assert schematic.get_as_string() == original
    commit = schematic.begin_commit()
    line = schematic.get_lines()[0]
    line.end = Vector2.from_xy_mm(50.8, 30.48)
    schematic.update_items(line)
    schematic.push_commit(commit, "move line")
    assert schematic.get_as_string() != original


def test_kicad_api_save_and_revert_follow_editor_contract(tmp_path) -> None:
    source = tmp_path / "source.kicad_sch"
    copy = tmp_path / "copy.kicad_sch"
    source.write_text(SCHEMATIC)
    schematic = FileSchematic.from_file(source)
    line = schematic.get_lines()[0]
    line.end = Vector2.from_xy_mm(50.8, 30.48)
    schematic.update_items(line)

    schematic.save_as(copy, include_project=False)
    schematic.save()

    assert copy.read_text() == schematic.get_as_string()
    assert source.read_text() == schematic.get_as_string()
    source.write_text(SCHEMATIC)
    schematic.revert()
    assert schematic.get_as_string() == SCHEMATIC


def test_kicad_api_rejects_unknown_id_and_type_change() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)
    line = schematic.get_lines()[0]
    line.id = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(KiCadSchematicError, match="unknown schematic item"):
        schematic.update_items(line)

    symbol = schematic.get_symbols()[0]
    impostor = SchematicLine(id=symbol.id, start=symbol.position, end=symbol.position)
    with pytest.raises(KiCadSchematicError, match="changed type"):
        schematic.update_items(impostor)


def test_kicad_api_creates_and_removes_routing_items() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)
    text = Text(
        value="NEW_NET",
        position=Vector2.from_xy_mm(55.88, 30.48),
        attributes=TextAttributes(Vector2.from_xy_mm(1.27, 1.27)),
    )
    new_items = [
        SchematicLine(
            id="",
            start=Vector2.from_xy_mm(50.8, 30.48),
            end=Vector2.from_xy_mm(55.88, 30.48),
        ),
        LocalLabel(id="", position=text.position, text=text),
        Junction(id="", position=Vector2.from_xy_mm(50.8, 30.48)),
        NoConnectMarker(id="", position=Vector2.from_xy_mm(60.96, 30.48)),
    ]

    created = schematic.create_items(new_items)

    assert all(item.id for item in created)
    assert len(schematic.get_lines()) == 2
    assert len(schematic.get_labels()) == 3
    assert len(schematic.get_junctions()) == 2
    assert len(schematic.get_no_connects()) == 2
    KiCadSchematicDocument.from_text(schematic.get_as_string())

    schematic.remove_items(created)

    assert schematic.get_as_string() == SCHEMATIC


def test_created_local_label_preserves_outward_text_alignment() -> None:
    schematic = FileSchematic.from_text(SCHEMATIC)
    text = Text(
        value="OUTWARD",
        position=Vector2.from_xy_mm(55.88, 30.48),
        attributes=TextAttributes(
            Vector2.from_xy_mm(1.27, 1.27),
            horizontal_alignment="left",
        ),
    )

    created = schematic.create_items(LocalLabel(id="", position=text.position, text=text))[0]

    assert created.text.attributes.horizontal_alignment == "left"
    assert "(justify left)" in schematic.get_as_string()
