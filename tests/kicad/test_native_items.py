from __future__ import annotations

from schemer.kicad.items import (
    SchematicField,
    SchematicSymbolInstance,
    SchematicSymbolTransform,
    Text,
    TextAttributes,
    Vector2,
    place_symbol,
)


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

    place_symbol(symbol, Vector2.from_xy_mm(20, 20), 90)

    assert symbol.reference_field.text.position == Vector2.from_xy_mm(20, 18)
    assert symbol.value_field.text.position == Vector2.from_xy_mm(20, 22)
