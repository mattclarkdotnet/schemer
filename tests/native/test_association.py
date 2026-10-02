from __future__ import annotations

import pytest

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.document import KiCadSchematicDocument
from schemer.native.association import associate_components
from tests.support.schematic import SCHEMATIC


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
