from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import hypot

from schemer.core.layout import Position


class BlockSide(StrEnum):
    """A public connection side of a block."""

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


class RegionKind(StrEnum):
    """The exclusive use reserved for one local rectangular corridor."""

    WIRE = "wire"
    ANNOTATION = "annotation"


@dataclass(frozen=True)
class Rect:
    """An axis-aligned rectangle in block-local viewer units."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def translated(self, x: float, y: float) -> Rect:
        return replace(self, x=self.x + x, y=self.y + y)

    def contains(self, other: Rect) -> bool:
        tolerance = 1e-9
        return (
            self.x - tolerance <= other.x
            and self.y - tolerance <= other.y
            and self.right + tolerance >= other.right
            and self.bottom + tolerance >= other.bottom
        )

    def overlaps(self, other: Rect) -> bool:
        """Return true only for positive-area overlap; edge contact is not overlap."""

        return min(self.right, other.right) > max(self.x, other.x) and min(
            self.bottom, other.bottom
        ) > max(self.y, other.y)

    def distance_to(self, other: Rect) -> float:
        """Return Euclidean edge-to-edge clearance between two rectangles."""

        delta_x = max(other.x - self.right, self.x - other.right, 0.0)
        delta_y = max(other.y - self.bottom, self.y - other.bottom, 0.0)
        return hypot(delta_x, delta_y)


@dataclass(frozen=True)
class BlockItem:
    """One viewer symbol owned and positioned by a block."""

    symbol_id: str
    position: Position
    occupied: Rect | None = None


@dataclass(frozen=True)
class BlockPort:
    """A named net exit on a block boundary.

    Offset is measured from the top for left/right ports and from the left for
    top/bottom ports.
    """

    name: str
    side: BlockSide
    offset: float
    net: str | None = None


@dataclass(frozen=True)
class PortRef:
    """A reference to one direct child's public block port."""

    child_id: str
    port_name: str


@dataclass(frozen=True)
class BlockLink:
    """A parent-owned connection between two or more child ports."""

    name: str
    endpoints: tuple[PortRef, ...]
    net: str | None = None


@dataclass(frozen=True)
class BlockRegion:
    """A local corridor reserved for either wiring or annotations."""

    name: str
    kind: RegionKind
    bounds: Rect
    net: str | None = None


@dataclass(frozen=True)
class PlacedBlock:
    """A child block at a deterministic offset inside its parent."""

    block: LayoutBlock
    x: float
    y: float

    @property
    def bounds(self) -> Rect:
        return Rect(self.x, self.y, self.block.width, self.block.height)


@dataclass(frozen=True)
class LayoutBlock:
    """A locally scoped schematic unit with explicit geometry and corridors.

    Block meaning is deliberately open: a block may represent an IC, a
    connector, one pin breakout, a power branch, a repeated channel bank, or a
    composite of smaller blocks. The contract is geometric rather than tied to
    component names or board identity.
    """

    block_id: str
    width: float
    height: float
    items: tuple[BlockItem, ...] = ()
    ports: tuple[BlockPort, ...] = ()
    regions: tuple[BlockRegion, ...] = ()
    children: tuple[PlacedBlock, ...] = ()
    links: tuple[BlockLink, ...] = ()
    minimum_child_spacing: float = 100.0

    @property
    def bounds(self) -> Rect:
        return Rect(0.0, 0.0, self.width, self.height)


@dataclass(frozen=True)
class BlockFinding:
    """One deterministic structural or local-collision defect."""

    code: str
    block_path: str
    subjects: tuple[str, ...]
    message: str


def port_point(block: LayoutBlock, port: BlockPort) -> tuple[float, float] | None:
    if port.side in {BlockSide.LEFT, BlockSide.RIGHT}:
        if not 0 <= port.offset <= block.height:
            return None
        return (0.0 if port.side == BlockSide.LEFT else block.width, port.offset)
    if not 0 <= port.offset <= block.width:
        return None
    return (port.offset, 0.0 if port.side == BlockSide.TOP else block.height)
