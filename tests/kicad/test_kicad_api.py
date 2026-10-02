from __future__ import annotations

from copy import deepcopy

import pytest

from schemer.kicad.editor import FileSchematic
from schemer.kicad.items import (
    BaseLabel,
    GlobalLabel,
    HierarchicalLabel,
    Junction,
    LocalLabel,
    NoConnectMarker,
    SchematicLine,
    SchematicSymbolInstance,
    Text,
    TextAttributes,
    Vector2,
    translate_item,
)
from tests.support.schematic import SCHEMATIC


@pytest.mark.parametrize("kind", ["symbol", "line", LocalLabel, GlobalLabel,
                                 HierarchicalLabel, Junction, NoConnectMarker])
def test_translation_moves_all_coordinates_but_preserves_identity_and_style(kind):
    point = Vector2(10_000, 20_000)
    if kind == "symbol":
        item = FileSchematic.from_text(SCHEMATIC).get_symbols()[0]
        item.transform.mirror_x = True
    elif kind == "line":
        item = SchematicLine(id="item", start=point, end=Vector2(40_000, 50_000))
    elif issubclass(kind, BaseLabel):
        item = kind(id="item", position=point, text=Text(
            "NET", deepcopy(point), TextAttributes(Vector2(1_270_000, 1_270_000), 90)))
    else:
        item = kind(id="item", position=point)
    before = deepcopy(item)

    translate_item(item, Vector2(7_000, -3_000))
    if isinstance(item, SchematicLine):
        pairs = [(item.start, before.start), (item.end, before.end)]
    else:
        pairs = [(item.position, before.position)]
        if isinstance(item, SchematicSymbolInstance):
            assert item.transform == before.transform
            pairs.extend((a.text.position, b.text.position)
                         for a, b in zip(item.fields, before.fields, strict=True))
        elif isinstance(item, BaseLabel):
            pairs.append((item.text.position, before.text.position))
    assert all(a == Vector2(b.x + 7_000, b.y - 3_000) for a, b in pairs)
    translate_item(item, Vector2(-7_000, 3_000))
    assert item == before
