from __future__ import annotations

import pytest

from schemer.cli.parser import build_parser
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.document import KiCadSchematicDocument
from schemer.kicad.editor import FileSchematic
from schemer.kicad.items import (
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
from tests.support.schematic import SCHEMATIC


def test_page_settings_round_trip_through_kicad_11_shaped_adapter() -> None:
    editor = FileSchematic.from_text(SCHEMATIC)

    assert editor.get_page_settings() == PageSettings("A1", "landscape")

    editor.set_page_settings(PageSettings("A3", "landscape"))

    assert editor.get_page_settings() == PageSettings("A3", "landscape")
    assert '(paper "A3")' in editor.get_as_string()
    assert 'property "Path" "FILTER.R_INPUT.R"' in editor.get_as_string()


def test_symbol_field_can_be_hidden_without_changing_its_value() -> None:
    editor = FileSchematic.from_text(SCHEMATIC)
    symbol = next(item for item in editor.get_symbols() if item.zener_path == "BUFFER.U1")
    reference = symbol.reference_field
    reference.visible = False

    editor.update_items(symbol)

    updated = next(item for item in editor.get_symbols() if item.zener_path == "BUFFER.U1")
    assert updated.reference == "U1"
    assert updated.reference_field.visible is False


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
