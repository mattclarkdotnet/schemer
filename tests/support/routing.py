from __future__ import annotations

from schemer.kicad.items import (
    SchematicLine,
)


def _line_mm(item: SchematicLine) -> tuple[tuple[float, float], tuple[float, float]]:
    return (
        (item.start.x / 1_000_000, item.start.y / 1_000_000),
        (item.end.x / 1_000_000, item.end.y / 1_000_000),
    )
