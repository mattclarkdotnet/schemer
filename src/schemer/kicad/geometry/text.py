from __future__ import annotations

from dataclasses import replace
from math import cos, radians, sin

from schemer.kicad.items import (
    SchematicSymbolInstance,
    Text,
    Vector2,
)


def text_width(text: Text) -> int:
    """Reserve a full nominal character pitch for caption/label placement."""

    return len(text.value) * text.attributes.size.x


def field_draw_angle(stored_angle: float, parent_angle: float) -> float:
    """Match SCH_FIELD::GetDrawRotation, not an ordinary vector rotation."""
    if parent_angle % 180 == 90:
        return 90 if stored_angle % 180 == 0 else 0
    return stored_angle


def caption_bank_axis(symbol: SchematicSymbolInstance) -> str | None:
    """Axis along which an aligned reference/value pair can move as one caption."""
    fields = [f for f in symbol.fields if f.visible and f.name in {"Reference", "Value"}]
    if len(fields) != 2:
        return None
    a, b = (field.text for field in fields)
    angle = field_draw_angle(a.attributes.angle, symbol.transform.orientation)
    if angle == 0 and a.position.y == b.position.y:
        return "x"
    if angle == 90 and a.position.x == b.position.x:
        return "y"
    return None


def oriented_field_text(
    text: Text, parent_angle: float, draw_angle: float = 0,
    horizontal_alignment: str = "center",
) -> Text:
    """Give generated fields a readable axis and sheet-space justification.

    KiCad transforms a field's bounding box with its symbol, but only toggles
    the drawing axis for a quarter turn. Storing an inverse 180/270 degree
    angle can therefore paint upside-down text. Use 0/90 and flip the stored
    justification when needed to retain the intended sheet-space envelope.
    """
    relative = (draw_angle - parent_angle) % 360
    alignment = horizontal_alignment
    if relative >= 180:
        alignment = {"left": "right", "right": "left", "center": "center"}[alignment]
    return replace(text, attributes=replace(
        text.attributes, angle=relative % 180,
        horizontal_alignment=alignment, vertical_alignment="center",
    ))


def text_envelope(text: Text, parent_angle: float = 0.0) -> tuple[Vector2, Vector2]:
    """Return a conservative painted envelope for one rendered text item."""

    # KiCad's stroke font varies by glyph and plotting backend.  Use a
    # deliberately conservative advance here: a false overlap costs a small
    # amount of whitespace, while an underestimated caption stays unreadable.
    width = text_width(text)
    height = text.attributes.size.y
    if text.attributes.horizontal_alignment == "left":
        min_x, max_x = 0, width
    elif text.attributes.horizontal_alignment == "right":
        min_x, max_x = -width, 0
    else:
        min_x, max_x = -round(width / 2), round(width / 2)
    half_height = round(height / 2)
    min_y, max_y = -half_height, half_height
    if text.attributes.vertical_alignment == "bottom":
        min_y, max_y = -height, 0
    elif text.attributes.vertical_alignment == "top":
        min_y, max_y = 0, height
    # Unlike GetDrawRotation, SCH_FIELD::GetBoundingBox applies the full
    # parent transform to the justified text box. Do not use the draw angle.
    angle = radians((text.attributes.angle + parent_angle) % 360)
    points = [Vector2(text.position.x + round(x * cos(angle) + y * sin(angle)),
                      text.position.y + round(-x * sin(angle) + y * cos(angle)))
              for x in (min_x, max_x) for y in (min_y, max_y)]
    return (
        Vector2(min(p.x for p in points), min(p.y for p in points)),
        Vector2(max(p.x for p in points), max(p.y for p in points)),
    )


def label_envelope(text: Text) -> tuple[Vector2, Vector2]:
    """Reserve the native label-to-wire inset and painted stroke as well as glyphs.

    A local label's electrical anchor is not its glyph baseline. The native
    plot shifts text away from the wire; this offset rotates with the label.
    Half a text height conservatively covers that inset at the native font.
    """

    angle = radians(text.attributes.angle)
    centered = text.attributes.vertical_alignment == "center"
    inset = 0 if centered else text.attributes.size.y / 2
    advance = text.attributes.size.x * (1 if centered else 0.25) * {
        "left": 1, "right": -1, "center": 0,
    }[text.attributes.horizontal_alignment]
    shifted = replace(text, position=Vector2(
        text.position.x + round(advance * cos(angle) - inset * sin(angle)),
        text.position.y - round(advance * sin(angle) + inset * cos(angle)),
    ))
    low, high = text_envelope(shifted)
    stroke = round(text.attributes.size.y * 0.08)
    if centered:
        # Global labels paint an enclosing flag, including its electrical tip,
        # beyond the glyph bounds. Reserve the box, not just the text inside.
        pad = round(text.attributes.size.y * 0.5)
        low = Vector2(min(low.x, text.position.x) - round(abs(sin(angle)) * pad),
                      min(low.y, text.position.y) - round(abs(cos(angle)) * pad))
        high = Vector2(max(high.x, text.position.x) + round(abs(sin(angle)) * pad),
                       max(high.y, text.position.y) + round(abs(cos(angle)) * pad))
    return (Vector2(low.x - stroke, low.y - stroke),
            Vector2(high.x + stroke, high.y + stroke))
