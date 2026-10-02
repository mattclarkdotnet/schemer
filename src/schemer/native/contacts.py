from __future__ import annotations

from math import cos, radians, sin

from schemer.kicad.geometry.envelopes import (
    envelope_from_points,
    point_distance,
)
from schemer.kicad.geometry.text import label_envelope
from schemer.kicad.items import (
    BaseLabel,
    GlobalLabel,
    SchematicLine,
    Vector2,
)
from schemer.native.routing_paths import segment_hits_box


def wire_hits_label(wire: SchematicLine, label: BaseLabel) -> bool:
    """A flag's connection tip may touch its own wire, not its text/body."""
    start, end = wire.start, wire.end
    if isinstance(label, GlobalLabel) and label.position in (start, end):
        other = end if start == label.position else start
        distance = point_distance(label.position, other)
        if distance == 0:
            return False
        angle = radians(label.text.attributes.angle)
        sign = -1 if label.text.attributes.horizontal_alignment == "left" else 1
        dx, dy = round(sign * cos(angle)), round(-sign * sin(angle))
        offset_x, offset_y = other.x - label.position.x, other.y - label.position.y
        if offset_x * dy != offset_y * dx or offset_x * dx + offset_y * dy <= 0:
            return segment_hits_box(start, end, envelope_from_points(label_envelope(label.text)))
        # The triangular tip occupies the first text-height from the anchor.
        # Exclude just this electrical attachment, not the rest of the route.
        trim = min(distance, label.text.attributes.size.y + 254_000)
        if trim == distance:
            return False
        start = Vector2(round(label.position.x + (other.x - label.position.x) * trim / distance),
                        round(label.position.y + (other.y - label.position.y) * trim / distance))
        end = other
    return segment_hits_box(start, end, envelope_from_points(label_envelope(label.text)))


def wires_intersect(a: SchematicLine, b: SchematicLine) -> bool:
    # Topological intersection is not the router's larger stroke-clearance
    # exclusion. Do not repartition a circuit or move text because two
    # speculative routes merely pass near one another.
    return segment_hits_box(a.start, a.end, envelope_from_points((b.start, b.end)))
