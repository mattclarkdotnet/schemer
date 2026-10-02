from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import cos, hypot, radians, sin

from schemer.kicad.items import Vector2


def point_distance(first: Vector2, second: Vector2) -> float:
    return hypot(first.x - second.x, first.y - second.y)


@dataclass(frozen=True)
class Envelope:
    min_x: int
    min_y: int
    max_x: int
    max_y: int

    @property
    def width(self) -> int:
        return self.max_x - self.min_x

    @property
    def height(self) -> int:
        return self.max_y - self.min_y

    @property
    def center_x(self) -> int:
        return round((self.min_x + self.max_x) / 2)

    @property
    def center_y(self) -> int:
        return round((self.min_y + self.max_y) / 2)

    def translated(self, delta: Vector2) -> Envelope:
        return Envelope(
            self.min_x + delta.x,
            self.min_y + delta.y,
            self.max_x + delta.x,
            self.max_y + delta.y,
        )


def box_distance(first: Envelope, second: Envelope) -> int:
    return max(0, first.min_x - second.max_x, second.min_x - first.max_x) + max(
        0, first.min_y - second.max_y, second.min_y - first.max_y,
    )


def envelope_from_points(points: Sequence[Vector2]) -> Envelope:
    return Envelope(min(p.x for p in points), min(p.y for p in points),
                     max(p.x for p in points), max(p.y for p in points))


def union_all(envelopes: list[Envelope]) -> Envelope:
    result = envelopes[0]
    for envelope in envelopes[1:]:
        result = union(result, envelope)
    return result


def envelopes_do_not_overlap(
    first: Envelope,
    second: Envelope,
    clearance: int,
) -> bool:
    return (
        first.max_x + clearance <= second.min_x
        or second.max_x + clearance <= first.min_x
        or first.max_y + clearance <= second.min_y
        or second.max_y + clearance <= first.min_y
    )


def union(first: Envelope, second: Envelope) -> Envelope:
    return Envelope(
        min(first.min_x, second.min_x),
        min(first.min_y, second.min_y),
        max(first.max_x, second.max_x),
        max(first.max_y, second.max_y),
    )


def subtract_envelope(box: Envelope, cut: Envelope) -> list[Envelope]:
    if (box.max_x < cut.min_x or cut.max_x < box.min_x
            or box.max_y < cut.min_y or cut.max_y < box.min_y):
        return [box]
    left, right = max(box.min_x, cut.min_x), min(box.max_x, cut.max_x)
    top, bottom = max(box.min_y, cut.min_y), min(box.max_y, cut.max_y)
    return [part for part in (
        Envelope(box.min_x, box.min_y, left - 1, box.max_y),
        Envelope(right + 1, box.min_y, box.max_x, box.max_y),
        Envelope(left, box.min_y, right, top - 1),
        Envelope(left, bottom + 1, right, box.max_y),
    ) if part.min_x <= part.max_x and part.min_y <= part.max_y]


def rotated_envelope(box: Envelope, angle: float) -> Envelope:
    c, s = cos(radians(angle)), sin(radians(angle))
    return envelope_from_points(tuple(Vector2(round(x * c + y * s), round(-x * s + y * c))
        for x in (box.min_x, box.max_x) for y in (box.min_y, box.max_y)))
