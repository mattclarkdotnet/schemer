from __future__ import annotations

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import Envelope, envelope_from_points, union
from schemer.kicad.geometry.library import placed_pin_positions, placed_symbol_body_positions
from schemer.kicad.geometry.text import label_envelope, text_envelope
from schemer.kicad.items import (
    BaseLabel,
    Junction,
    NoConnectMarker,
    SchematicLine,
    SchematicSymbolInstance,
    Vector2,
)


def completed_group_envelopes(
    editor: FileSchematic,
    groups: dict[str, str],
) -> dict[str, Envelope]:
    """Measure bodies, annotations and local wiring owned by each block."""

    raw_by_id = {symbol.uuid: symbol for symbol in editor.document.symbols}
    envelopes: dict[str, Envelope] = {}
    for item in editor.get_items():
        group = groups.get(item.id)
        if group is None:
            continue
        points: list[Vector2] = []
        if isinstance(item, SchematicSymbolInstance):
            raw = raw_by_id[item.id]
            points.extend(placed_symbol_body_positions(editor.document, raw))
            points.extend(placed_pin_positions(editor.document, raw).values())
            for field in item.fields:
                if field.visible and field.name in {"Reference", "Value"}:
                    points.extend(
                        text_envelope(field.text, item.transform.orientation)
                    )
            if not points:
                points.append(item.position)
        elif isinstance(item, SchematicLine):
            points.extend((item.start, item.end))
        elif isinstance(item, BaseLabel):
            points.extend(label_envelope(item.text))
        elif isinstance(item, (Junction, NoConnectMarker)):
            points.append(item.position)
        if not points:
            continue
        envelope = envelope_from_points(points)
        previous = envelopes.get(group)
        envelopes[group] = envelope if previous is None else union(previous, envelope)
    return envelopes


def content_envelope(editor: FileSchematic) -> Envelope:
    """Use the same completed-drawing bounds for packing and sheet selection."""

    envelopes = completed_group_envelopes(
        editor, {item.id: "drawing" for item in editor.get_items()},
    )
    if not envelopes:
        raise KiCadSchematicError("native layout contains no visible items")
    return envelopes["drawing"]
