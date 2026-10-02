from __future__ import annotations

from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
)
from schemer.kicad.items import (
    Vector2,
)


def test_glyph_envelope_includes_every_vertex_regardless_of_drawing_order():
    assert envelope_from_points((Vector2(0, 0), Vector2(0, 1_270_000),
                                 Vector2(1_270_000, 1_270_000), Vector2(0, 2_540_000),
                                 Vector2(-1_270_000, 1_270_000))) == Envelope(
        -1_270_000, 0, 1_270_000, 2_540_000,
    )
