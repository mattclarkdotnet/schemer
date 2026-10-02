from __future__ import annotations

import pytest

from schemer.kicad.document import KiCadSchematicDocument
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_segments,
    placed_pin_sides,
    placed_symbol_body_positions,
    symbol_library_pins,
)
from schemer.kicad.items import (
    Vector2,
)
from schemer.native.placement.branches import (
    place_top_pin_bypasses,
    place_vertical_pin_bias_branches,
)
from tests.support.schematic import SCHEMATIC, _support_fixture


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


@pytest.mark.parametrize("rotation, first", [
    (0, (17.78, 29.21)), (90, (19.05, 33.02)),
    (180, (22.86, 31.75)), (270, (21.59, 27.94)),
])
def test_asymmetric_pin_follows_kicad_counterclockwise_rotation(rotation, first):
    source = SCHEMATIC.replace("(at 20.32 30.48 90)", f"(at 20.32 30.48 {rotation})")
    source = source.replace("(at -2.54 0 0)", "(at -2.54 1.27 0)")
    document = KiCadSchematicDocument.from_text(source)
    assert placed_pin_positions(document, document.symbols[0])["1"] == Vector2.from_xy_mm(*first)


@pytest.mark.parametrize("role,place", [
    ("pulldown", place_vertical_pin_bias_branches),
    ("bypass", place_top_pin_bypasses),
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
