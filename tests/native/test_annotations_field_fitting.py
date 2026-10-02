from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelopes_do_not_overlap,
)
from schemer.kicad.geometry.library import (
    placed_symbol_body_positions,
)
from schemer.kicad.geometry.text import (
    field_draw_angle,
    text_envelope,
)
from schemer.kicad.items import (
    Junction,
    NoConnectMarker,
    SchematicLine,
    Vector2,
)
from schemer.kicad.syntax import descendant
from schemer.native.annotations.field_fitting import (
    _rail_caption_offsets,
    resolve_symbol_field_overlaps,
)
from schemer.native.annotations.fields import (
    position_bank_fields,
    position_component_fields,
)
from schemer.native.packing import (
    pack_completed_groups,
)
from schemer.native.routing_paths import segment_hits_box
from tests.support.schematic import SCHEMATIC, _support_fixture


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
    resolved = resolve_symbol_field_overlaps(editor, [symbol])[0]
    assert resolved.reference_field.text.position == symbol.reference_field.text.position
    assert resolved.value_field.text.position == symbol.value_field.text.position


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

    resolved = resolve_symbol_field_overlaps(editor, [])[0]

    low, high = text_envelope(resolved.value_field.text, resolved.transform.orientation)
    assert high.x < line.start.x or low.x > line.start.x
    assert (
        resolved.position, resolved.transform.orientation, line.start, line.end
    ) == original_geometry
    assert abs(resolved.value_field.text.position.x - symbol.position.x) > 5_080_000


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

    resolved = resolve_symbol_field_overlaps(editor, [])[0]

    assert resolved.value_field.text.position.y == symbol.position.y
    low, _ = text_envelope(resolved.value_field.text, resolved.transform.orientation)
    body_right = max(p.x for p in placed_symbol_body_positions(
        editor.document, editor.document.symbols[0],
    ))
    assert low.x == body_right + 1_270_000
    assert resolved.position == symbol.position


def test_failed_caption_placement_is_reported(monkeypatch, caplog):
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    monkeypatch.setattr(
        "schemer.native.annotations.field_fitting._rail_caption_offsets", lambda *args: []
    )
    resolve_symbol_field_overlaps(editor, [])
    assert "No clear caption position for R1" in caplog.text


@pytest.mark.parametrize("remaining", [0, 1, 2])
def test_caption_retry_only_accepts_fewer_failures_and_reports_chosen_result(
    monkeypatch, remaining,
):
    from types import SimpleNamespace

    import schemer.native.annotations.field_fitting as layout
    from schemer.core.diagnostics import capture_layout_issues

    failures = [SimpleNamespace(id=str(i), reference=f"P{i}", value="RAIL") for i in range(2)]
    calls = []

    def fit(editor, components, grid, clearance, priority_ids=frozenset()):
        calls.append(priority_ids)
        return (["retry"], failures[:remaining]) if priority_ids else (["initial"], failures)

    monkeypatch.setattr(layout, "_fit_symbol_fields", fit)
    with capture_layout_issues(True) as issues:
        result = resolve_symbol_field_overlaps(None, [])
    assert calls == [frozenset(), frozenset({"0", "1"})]
    assert result == (["retry"] if remaining < 2 else ["initial"])
    assert len(issues) == remaining


def test_caption_retry_promotes_newly_stranded_names_only_while_improving(monkeypatch):
    from types import SimpleNamespace

    import schemer.native.annotations.field_fitting as layout
    from schemer.core.diagnostics import capture_layout_issues

    names = [SimpleNamespace(id=str(i), reference=f"P{i}", value="RAIL") for i in range(3)]
    priorities = []

    def fit(editor, components, grid, clearance, priority_ids=frozenset()):
        priorities.append(priority_ids)
        failed = names[:2] if not priority_ids else names[2:] if len(priority_ids) == 2 else []
        return names, failed

    monkeypatch.setattr(layout, "_fit_symbol_fields", fit)
    with capture_layout_issues(True) as issues:
        assert resolve_symbol_field_overlaps(None, []) == names
    assert priorities == [frozenset(), frozenset({"0", "1"}), frozenset({"0", "1", "2"})]
    assert issues == []


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_crowded_rail_caption_rotates_without_moving_its_symbol_or_wires(caplog, rotation):
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        '(symbol "Device:R"', '(symbol "Device:R" (pin_numbers hide)',
    ))
    symbol = editor.get_symbols()[0]
    symbol.transform.orientation = rotation
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.reference_field.visible = False
    symbol.value_field.text.value = "LONG_SUPPLY_RAIL"
    symbol.value_field.text.attributes.angle = (-symbol.transform.orientation) % 180
    size = symbol.value_field.text.attributes.size
    editor.update_items(symbol)
    wires = [SchematicLine(id="", start=Vector2.from_xy_mm(x, 0),
                           end=Vector2.from_xy_mm(x, 80)) for x in (15, 25)]
    editor.create_items(wires)
    resolved = resolve_symbol_field_overlaps(editor, [])[0]
    assert "No clear caption" not in caplog.text
    assert field_draw_angle(resolved.value_field.text.attributes.angle,
                            resolved.transform.orientation) == 90
    assert resolved.position == symbol.position
    assert resolved.value_field.text.attributes.size == size


def test_two_pin_caption_tries_actual_obstacle_edges_beyond_the_grid_search(caplog):
    editor = FileSchematic.from_text(SCHEMATIC)
    part = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != part.id])
    part.transform.orientation = 0
    part.value = "LONG_CAPACITOR_VALUE_123456"
    editor.update_items(part)
    part = position_component_fields(editor, [part])[0]
    editor.update_items(part)
    wires = [SchematicLine(id=f"trunk-{x}", start=Vector2.from_xy_mm(x, 0),
                           end=Vector2.from_xy_mm(x, 80)) for x in (10, 30)]
    editor.create_items(wires)

    resolved = resolve_symbol_field_overlaps(editor, [part])[0]

    assert "No clear caption position" not in caplog.text
    assert resolved.position == part.position
    for field in (resolved.reference_field, resolved.value_field):
        low, high = text_envelope(field.text, resolved.transform.orientation)
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

    resolved = resolve_symbol_field_overlaps(editor, [part])[0]

    assert resolved.value_field.text.position != part.value_field.text.position
    assert resolved.position == part.position
    body = placed_symbol_body_positions(editor.document, editor.document.symbols[0])
    for field in (resolved.reference_field, resolved.value_field):
        low, high = text_envelope(field.text)
        assert (
            low.x >= max(p.x for p in body) + 250_000
            or high.x <= min(p.x for p in body) - 250_000
            or low.y >= max(p.y for p in body) + 250_000
            or high.y <= min(p.y for p in body) - 250_000
        )


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

    resolved = resolve_symbol_field_overlaps(editor, [symbol])[0]

    low, high = text_envelope(resolved.value_field.text, resolved.transform.orientation)
    assert (high.x < marker.position.x - 635_000 or low.x > marker.position.x + 635_000
            or high.y < marker.position.y - 635_000 or low.y > marker.position.y + 635_000)


def test_bank_caption_can_slide_less_than_a_grid_step_to_clear_a_marker(caplog):
    editor = FileSchematic.from_text(SCHEMATIC.replace(
        '(symbol "Device:R"', '(symbol "Device:R" (pin_numbers hide)',
    ))
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    symbol.transform.orientation = 0
    position_bank_fields(editor, [symbol], {symbol.id: ("bank", "", "", "")})
    editor.update_items(symbol)
    value_right = text_envelope(symbol.value_field.text)[1].x
    marker = NoConnectMarker(id="adjacent-pin-nc", position=Vector2(
        value_right + 635_000, symbol.value_field.text.position.y - 1_270_000,
    ))
    wire = SchematicLine(id="bank-wire", start=Vector2.from_xy_mm(22.86, 30.48),
                         end=Vector2(marker.position.x, symbol.position.y))
    editor.create_items([marker, wire])

    resolved = resolve_symbol_field_overlaps(editor, [symbol])[0]

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

    resolved = resolve_symbol_field_overlaps(editor, [symbol])[0]

    low, high = text_envelope(resolved.value_field.text, resolved.transform.orientation)
    radius = round(painted_diameter * 500_000)
    assert (high.x + 250_000 <= junction.position.x - radius
            or low.x - 250_000 >= junction.position.x + radius
            or high.y + 250_000 <= junction.position.y - radius
            or low.y - 250_000 >= junction.position.y + radius)
    if diameter == 0.4:
        assert resolved.value_field.text.position == symbol.value_field.text.position


def test_row_constrained_caption_gets_space_before_movable_device_title():
    _, editor = _support_fixture(None)
    title, bank = editor.get_symbols()
    for symbol, x in ((title, 20.32), (bank, 50.8)):
        symbol.transform.orientation = 0
        symbol.position = Vector2.from_xy_mm(x, 30.48)
    for field, x in ((bank.reference_field, 52), (bank.value_field, 56)):
        field.text.position = Vector2.from_xy_mm(x, 26)
        field.text.attributes.angle = 0
        field.text.attributes.horizontal_alignment = "left"
    title.reference_field.text.position = Vector2.from_xy_mm(52, 26)
    title.value_field.text.position = Vector2.from_xy_mm(52, 23)
    editor.update_items([title, bank])
    resolved = {s.id: s for s in resolve_symbol_field_overlaps(editor, [title, bank])}
    for before, after in ((bank.reference_field, resolved[bank.id].reference_field),
                          (bank.value_field, resolved[bank.id].value_field)):
        assert before.text.position == after.text.position
    assert resolved[title.id].reference_field.text.position != title.reference_field.text.position


def test_caption_can_slide_past_a_wire_without_moving_the_part():
    editor = FileSchematic.from_text(SCHEMATIC)
    part = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != part.id])
    part.value = "A_LONG_NATIVE_CAPTION"
    editor.update_items(part)
    part, = position_component_fields(editor, [part])
    editor.update_items(part)
    editor.create_items([
        SchematicLine(id="", start=Vector2.from_xy_mm(24, -100),
                      end=Vector2.from_xy_mm(24, 100)),
        SchematicLine(id="", start=Vector2.from_xy_mm(-40, 29),
                      end=Vector2.from_xy_mm(18, 29)),
    ])
    resolved, = resolve_symbol_field_overlaps(editor, [part])
    assert resolved.position == part.position
    assert resolved.value_field.text.position != part.value_field.text.position
    from schemer.kicad.geometry.envelopes import envelope_from_points

    for field in (resolved.reference_field, resolved.value_field):
        box = envelope_from_points(text_envelope(field.text, resolved.transform.orientation))
        assert all(not segment_hits_box(w.start, w.end, box) for w in editor.get_lines())


def test_clear_caption_prefers_its_own_body_over_a_neighbour():
    from schemer.kicad.geometry.envelopes import box_distance, envelope_from_points
    from schemer.kicad.items import place_symbol

    _, editor = _support_fixture(None)
    a, b = editor.get_symbols()
    place_symbol(a, Vector2.from_xy_mm(20, 20), 0)
    place_symbol(b, Vector2.from_xy_mm(40, 20), 0)
    editor.update_items([a, b])
    a, b = position_component_fields(editor, [a, b])
    a.reference_field.text.position = Vector2.from_xy_mm(40, 11)
    a.value_field.text.position = Vector2.from_xy_mm(40, 14)
    editor.update_items([a, b])
    bodies = {s.uuid: envelope_from_points(placed_symbol_body_positions(editor.document, s))
              for s in editor.document.symbols}
    resolved = {s.id: s for s in resolve_symbol_field_overlaps(editor, [a, b])}
    caption = envelope_from_points(text_envelope(
        resolved[a.id].reference_field.text, resolved[a.id].transform.orientation))
    assert box_distance(caption, bodies[a.id]) < box_distance(caption, bodies[b.id])
    assert resolved[a.id].position == a.position


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
                font = descendant(field.expression, ("effects", "font"))
                fonts[symbol.uuid, field.name] = document.source[font.start:font.end]
        return document.source[library.start:library.end], fonts

    before = presentation(editor.document)
    symbols = list(editor.get_symbols())
    symbols = position_component_fields(editor, symbols)
    editor.update_items(symbols)
    editor.update_items(resolve_symbol_field_overlaps(editor, symbols))
    groups = {symbol.id: str(index) for index, symbol in enumerate(symbols)}
    pack_completed_groups(editor, groups, "0")

    assert presentation(editor.document) == before


def test_rail_caption_can_slide_down_beside_its_glyph_to_clear_the_approach_wire():
    glyph = Envelope(-1_270_000, 0, 1_270_000, 2_540_000)
    original = Envelope(-6_286_500, 3_175_000, 6_286_500, 4_445_000)
    wires = [Envelope(-5_230_000, -150_000, -4_930_000, 10_000_000),
             Envelope(-150_000, -150_000, 2_690_000, 150_000)]
    clear = [original.translated(offset)
             for offset in _rail_caption_offsets(original, glyph, 1_270_000)
             if all(envelopes_do_not_overlap(original.translated(offset), wire, 952_500)
                    for wire in wires)]
    assert clear
    assert clear[0].min_x > glyph.max_x
    assert glyph.min_y <= clear[0].center_y <= glyph.max_y


def test_rail_caption_uses_obstacle_edges_between_fixed_candidate_rows():
    glyph = Envelope(-762_000, -2_540_000, 762_000, 0)
    original = Envelope(-4_000_000, -4_445_000, 4_000_000, -3_175_000)
    obstacles = (
        (Envelope(-2_690_000, -2_690_000, 150_000, -2_390_000), 952_500),
        (Envelope(-6_000_000, -300_000, -1_500_000, 300_000), 250_000),
        (Envelope(1_500_000, -10_000_000, 10_000_000, 10_000_000), 250_000),
    )
    original_choices = _rail_caption_offsets(original, glyph, 1_270_000)
    choices = _rail_caption_offsets(original, glyph, 1_270_000, obstacles)
    clear = [offset for offset in choices if offset not in original_choices
             and all(envelopes_do_not_overlap(original.translated(offset), box, gap)
                     for box, gap in obstacles)]
    assert clear
