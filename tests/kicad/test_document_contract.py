from __future__ import annotations

import pytest

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.document import KiCadSchematicDocument
from tests.support.schematic import SCHEMATIC


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
