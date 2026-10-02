from __future__ import annotations

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.content import completed_group_envelopes, content_envelope
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelopes_do_not_overlap,
)
from schemer.kicad.items import (
    PageSettings,
    SchematicLine,
    Vector2,
)
from schemer.native.packing import (
    pack_completed_groups,
    packed_group_deltas,
    page_settings_for_content,
    standard_page_for_bounds,
)
from tests.support.schematic import SCHEMATIC


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

    envelope = content_envelope(editor)
    assert envelope == completed_group_envelopes(editor, {symbol.id: "block"})["block"]
    settings = page_settings_for_content(editor)
    assert settings == PageSettings("A3", "landscape")
    assert editor.get_as_string() == before
    editor.set_page_settings(settings)
    assert editor.get_as_string() == before.replace('(paper "A1")', '(paper "A3")')


@pytest.mark.parametrize("initial_page", ["A0", "A4"])
def test_sheet_selection_ignores_seed_page_and_preserves_packed_items(initial_page):
    editor = FileSchematic.from_text(SCHEMATIC.replace('(paper "A1")', f'(paper "{initial_page}")'))
    symbol = editor.get_symbols()[0]
    editor.remove_items([item for item in editor.get_items() if item.id != symbol.id])
    pack_completed_groups(editor, {symbol.id: "block"}, "block")
    bounds = content_envelope(editor)
    assert bounds.min_x > 20_000_000 and bounds.min_y > 20_000_000
    assert bounds.max_x < 277_000_000 and bounds.max_y < 158_000_000
    assert bounds.center_x == pytest.approx(148_500_000, abs=1000)
    assert bounds.center_y == pytest.approx(105_000_000, abs=1000)
    before = editor.get_as_string()

    settings = page_settings_for_content(editor)
    assert settings == PageSettings("A4", "landscape")
    editor.set_page_settings(settings)
    assert editor.get_as_string() == before.replace(f'(paper "{initial_page}")', '(paper "A4")')


def test_final_sheet_framing_reserves_the_default_title_block():
    editor = FileSchematic.from_text(SCHEMATIC)
    editor.remove_items(editor.get_items())
    wires = editor.create_items([
        SchematicLine(id="", start=Vector2(0, 0), end=Vector2.from_xy_mm(250, 0)),
        SchematicLine(id="", start=Vector2.from_xy_mm(250, 0), end=Vector2.from_xy_mm(250, 160)),
    ])
    # This fits within the A4 frame, but its lower-right corner would
    # overlap the title block. Framing must account for that fixed content.
    page = pack_completed_groups(editor, {w.id: "block" for w in wires}, "block")
    assert page == PageSettings("A3", "landscape")
    bounds = content_envelope(editor)
    assert bounds.max_y < 245_000_000


def test_caption_bounds_do_not_give_electrical_blocks_fractional_micrometre_offsets():
    editor = FileSchematic.from_text(SCHEMATIC)
    part = editor.get_symbols()[0]
    editor.remove_items([s for s in editor.get_symbols() if s.id != part.id])
    part.value_field.text.position = Vector2(-100_000_750, -100_000_750)
    editor.update_items(part)
    before = {s.id: s.position for s in editor.get_symbols()}

    pack_completed_groups(editor, {item.id: "block" for item in editor.get_items()}, "block")

    for symbol in editor.get_symbols():
        assert (symbol.position.x - before[symbol.id].x) % 1000 == 0
        assert (symbol.position.y - before[symbol.id].y) % 1000 == 0


def test_top_level_blocks_pack_to_landscape_without_resizing_or_seed_constraints() -> None:
    mm = 1_000_000
    envelopes = {
        "input": Envelope(0, 0, 10 * mm, 20 * mm),
        "controller": Envelope(100 * mm, 40 * mm, 140 * mm, 100 * mm),
        "audio": Envelope(300 * mm, -80 * mm, 350 * mm, -30 * mm),
        "usb": Envelope(320 * mm, 240 * mm, 360 * mm, 270 * mm),
    }

    deltas = packed_group_deltas(envelopes, "controller", clearance_mm=20)
    packed = {group: envelope.translated(deltas[group]) for group, envelope in envelopes.items()}

    for name, box in packed.items():
        assert (box.width, box.height) == (envelopes[name].width, envelopes[name].height)
        assert all(envelopes_do_not_overlap(box, other, 20 * mm)
                   for peer, other in packed.items() if peer != name)
    width = max(box.max_x for box in packed.values()) - min(box.min_x for box in packed.values())
    height = max(box.max_y for box in packed.values()) - min(box.min_y for box in packed.values())
    assert 1.2 <= width / height <= 1.6
    shifted = {name: box.translated(Vector2(-500 * mm, 200 * mm))
               for name, box in envelopes.items()}
    shifted_deltas = packed_group_deltas(shifted, "controller", clearance_mm=20)
    assert packed == {name: box.translated(shifted_deltas[name]) for name, box in shifted.items()}


def test_shallow_wide_bank_does_not_separate_tall_functional_blocks():
    mm = 1_000_000
    envelopes = {name: Envelope(0, 0, w * mm, h * mm) for name, w, h in (
        ("primary", 70, 130), ("a-wide-bank", 160, 15), ("z-amplifier", 110, 160),
        ("ports", 300, 12))}
    deltas = packed_group_deltas(envelopes, "primary", connector_groups=frozenset({"ports"}))
    packed = {name: box.translated(deltas[name]) for name, box in envelopes.items()}
    assert packed["primary"].min_y == packed["z-amplifier"].min_y
    assert packed["z-amplifier"].min_x - packed["primary"].max_x == 20_320_000
    assert packed["a-wide-bank"].min_y >= packed["z-amplifier"].max_y + 20_320_000
    assert packed["ports"].min_y >= packed["a-wide-bank"].max_y + 20_320_000


def test_wide_functional_block_does_not_force_a_panorama():
    mm = 1_000_000
    sizes = {"controller": (130, 104), "power": (400, 115), "filter": (60, 30)}
    sizes.update({f"port-{i}": (45, 40) for i in range(6)})
    envelopes = {name: Envelope(0, 0, w * mm, h * mm) for name, (w, h) in sizes.items()}
    connectors = frozenset(name for name in sizes if name.startswith("port"))
    deltas = packed_group_deltas(envelopes, "controller", connector_groups=connectors)
    packed = {name: box.translated(deltas[name]) for name, box in envelopes.items()}
    width = max(b.max_x for b in packed.values()) - min(b.min_x for b in packed.values())
    height = max(b.max_y for b in packed.values()) - min(b.min_y for b in packed.values())
    assert 1.2 <= width / height <= 1.6
    functional_bottom = max(box.max_y for name, box in packed.items() if name not in connectors)
    assert all(packed[name].min_y >= functional_bottom + 20_320_000 for name in connectors)


def test_connector_bank_wraps_instead_of_widening_an_authored_layout():
    mm = 1_000_000
    envelopes = {"input": Envelope(0, 0, 40 * mm, 60 * mm),
                 "output": Envelope(0, 0, 40 * mm, 60 * mm)}
    connectors = frozenset(f"port-{i}" for i in range(8))
    envelopes.update({name: Envelope(0, 0, 30 * mm, 30 * mm) for name in connectors})
    deltas = packed_group_deltas(envelopes, "input", connector_groups=connectors,
                                 right_of=(("output", "input"),))
    packed = {name: box.translated(deltas[name]) for name, box in envelopes.items()}
    assert len({packed[name].min_y for name in connectors}) > 1
    assert all(packed["input"].min_x <= packed[name].min_x
               and packed[name].max_x <= packed["output"].max_x for name in connectors)


@pytest.mark.parametrize("right_of", [(), (("audio", "controller"),)])
def test_connector_only_groups_are_packed_after_functional_groups(right_of) -> None:
    mm = 1_000_000
    envelopes = {
        "controller": Envelope(100 * mm, 40 * mm, 140 * mm, 100 * mm),
        "audio": Envelope(300 * mm, 20 * mm, 350 * mm, 70 * mm),
        "panel": Envelope(-500 * mm, -300 * mm, -470 * mm, -260 * mm),
    }

    deltas = packed_group_deltas(
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
        "controller": Envelope(100 * mm, 40 * mm, 140 * mm, 100 * mm),
        "audio": Envelope(-300 * mm, -80 * mm, -250 * mm, -30 * mm),
        "usb": Envelope(320 * mm, 240 * mm, 360 * mm, 270 * mm),
        "panel": Envelope(-500 * mm, -300 * mm, -470 * mm, -260 * mm),
    }

    deltas = packed_group_deltas(
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
    (277, 190, "A4"),
    (277.001, 190, "A3"),
    (277, 190.001, "A3"),
    (400, 277, "A3"),
    (400.001, 277, "A2"),
    (400, 277.001, "A2"),
    (574, 400, "A2"),
    (821, 574, "A1"),
    (1169, 821, "A0"),
])
def test_sheet_size_is_selected_after_layout_from_content_bounds(right, bottom, page) -> None:
    mm = 1_000_000

    settings = standard_page_for_bounds(
        Envelope(10 * mm, 10 * mm, round(right * mm), round(bottom * mm)),
    )

    assert settings.page_size == page
    assert settings.orientation == "landscape"
