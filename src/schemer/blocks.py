"""Composable, locally validated schematic layout blocks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from math import hypot
from pathlib import Path
from statistics import median

from schemer.layout import LayoutPlan, ModuleLayout, Position
from schemer.toolchain import ToolchainError


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


def _port_point(block: LayoutBlock, port: BlockPort) -> tuple[float, float] | None:
    if port.side in {BlockSide.LEFT, BlockSide.RIGHT}:
        if not 0 <= port.offset <= block.height:
            return None
        return (0.0 if port.side == BlockSide.LEFT else block.width, port.offset)
    if not 0 <= port.offset <= block.width:
        return None
    return (port.offset, 0.0 if port.side == BlockSide.TOP else block.height)


def _local_findings(
    block: LayoutBlock,
    *,
    path: str,
    owners: dict[str, str],
) -> list[BlockFinding]:
    findings: list[BlockFinding] = []
    if block.width <= 0 or block.height <= 0:
        findings.append(
            BlockFinding(
                "invalid-block-size",
                path,
                (block.block_id,),
                "block width and height must both be positive",
            )
        )
        return findings
    if block.minimum_child_spacing < 0:
        findings.append(
            BlockFinding(
                "invalid-child-spacing",
                path,
                (block.block_id,),
                "minimum child spacing cannot be negative",
            )
        )

    container = block.bounds
    local_item_ids: set[str] = set()
    for item in block.items:
        if item.symbol_id in local_item_ids:
            findings.append(
                BlockFinding(
                    "duplicate-symbol-owner",
                    path,
                    (item.symbol_id,),
                    "one block owns the same symbol more than once",
                )
            )
        local_item_ids.add(item.symbol_id)
        previous_owner = owners.setdefault(item.symbol_id, path)
        if previous_owner != path:
            findings.append(
                BlockFinding(
                    "duplicate-symbol-owner",
                    path,
                    (item.symbol_id, previous_owner),
                    "a symbol may be owned by exactly one block",
                )
            )
        if item.occupied is None:
            continue
        if item.occupied.width <= 0 or item.occupied.height <= 0:
            findings.append(
                BlockFinding(
                    "invalid-item-envelope",
                    path,
                    (item.symbol_id,),
                    "occupied item envelopes must have positive area",
                )
            )
        elif not container.contains(item.occupied):
            findings.append(
                BlockFinding(
                    "item-outside-block",
                    path,
                    (item.symbol_id,),
                    "owned item envelope lies outside its block",
                )
            )

    for index, first in enumerate(block.items):
        if first.occupied is None:
            continue
        for second in block.items[index + 1 :]:
            if second.occupied is not None and first.occupied.overlaps(second.occupied):
                findings.append(
                    BlockFinding(
                        "item-body-overlap",
                        path,
                        (first.symbol_id, second.symbol_id),
                        "component bodies within one block have positive-area overlap",
                    )
                )

    port_names: set[str] = set()
    for port in block.ports:
        if port.name in port_names:
            findings.append(
                BlockFinding(
                    "duplicate-block-port",
                    path,
                    (port.name,),
                    "block port names must be unique within a block",
                )
            )
        port_names.add(port.name)
        if _port_point(block, port) is None:
            findings.append(
                BlockFinding(
                    "port-off-boundary",
                    path,
                    (port.name,),
                    "block port offset does not lie on its declared boundary side",
                )
            )

    region_names: set[str] = set()
    for region in block.regions:
        if region.name in region_names:
            findings.append(
                BlockFinding(
                    "duplicate-block-region",
                    path,
                    (region.name,),
                    "block region names must be unique within a block",
                )
            )
        region_names.add(region.name)
        if region.bounds.width <= 0 or region.bounds.height <= 0:
            findings.append(
                BlockFinding(
                    "invalid-region-envelope",
                    path,
                    (region.name,),
                    "reserved regions must have positive area",
                )
            )
            continue
        if not container.contains(region.bounds):
            findings.append(
                BlockFinding(
                    "region-outside-block",
                    path,
                    (region.name,),
                    "reserved region lies outside its block",
                )
            )
        for item in block.items:
            if item.occupied is not None and region.bounds.overlaps(item.occupied):
                findings.append(
                    BlockFinding(
                        "region-item-overlap",
                        path,
                        (region.name, item.symbol_id),
                        "reserved wiring or annotation area crosses a component body",
                    )
                )

    for index, first in enumerate(block.regions):
        for second in block.regions[index + 1 :]:
            if not first.bounds.overlaps(second.bounds):
                continue
            if RegionKind.ANNOTATION in {first.kind, second.kind}:
                code = "annotation-region-overlap"
                message = "an annotation area overlaps another reserved local corridor"
            elif first.net is not None and first.net == second.net:
                continue
            else:
                code = "wire-region-crossing"
                message = "wire corridors for different or unknown nets overlap"
            findings.append(BlockFinding(code, path, (first.name, second.name), message))

    child_ids: set[str] = set()
    children_by_id: dict[str, LayoutBlock] = {}
    for child in block.children:
        child_path = f"{path}/{child.block.block_id}"
        if child.block.block_id in child_ids:
            findings.append(
                BlockFinding(
                    "duplicate-child-block",
                    path,
                    (child.block.block_id,),
                    "sibling block identifiers must be unique",
                )
            )
        child_ids.add(child.block.block_id)
        children_by_id.setdefault(child.block.block_id, child.block)
        if not container.contains(child.bounds):
            findings.append(
                BlockFinding(
                    "child-outside-block",
                    path,
                    (child.block.block_id,),
                    "child block lies outside its parent",
                )
            )
        for item in block.items:
            if item.occupied is not None and child.bounds.overlaps(item.occupied):
                findings.append(
                    BlockFinding(
                        "item-child-overlap",
                        path,
                        (item.symbol_id, child.block.block_id),
                        "a parent-owned component body overlaps a child block",
                    )
                )
        for region in block.regions:
            if child.bounds.overlaps(region.bounds):
                findings.append(
                    BlockFinding(
                        "region-child-overlap",
                        path,
                        (region.name, child.block.block_id),
                        "a parent corridor intrudes into a child block",
                    )
                )
        findings.extend(_local_findings(child.block, path=child_path, owners=owners))

    link_names: set[str] = set()
    linked_endpoints: dict[PortRef, str] = {}
    for link in block.links:
        if link.name in link_names:
            findings.append(
                BlockFinding(
                    "duplicate-block-link",
                    path,
                    (link.name,),
                    "block link names must be unique within a parent",
                )
            )
        link_names.add(link.name)
        if len(link.endpoints) < 2:
            findings.append(
                BlockFinding(
                    "incomplete-block-link",
                    path,
                    (link.name,),
                    "a block link requires at least two child-port endpoints",
                )
            )
        for endpoint in link.endpoints:
            child = children_by_id.get(endpoint.child_id)
            port = (
                next(
                    (
                        candidate
                        for candidate in child.ports
                        if candidate.name == endpoint.port_name
                    ),
                    None,
                )
                if child is not None
                else None
            )
            if port is None:
                findings.append(
                    BlockFinding(
                        "unknown-link-port",
                        path,
                        (link.name, endpoint.child_id, endpoint.port_name),
                        "block link endpoint does not resolve to a direct-child port",
                    )
                )
                continue
            if link.net is not None and port.net is not None and link.net != port.net:
                findings.append(
                    BlockFinding(
                        "link-net-mismatch",
                        path,
                        (link.name, endpoint.child_id, endpoint.port_name),
                        "parent link net disagrees with the child port net",
                    )
                )
            previous_link = linked_endpoints.setdefault(endpoint, link.name)
            if previous_link != link.name:
                findings.append(
                    BlockFinding(
                        "multiply-linked-port",
                        path,
                        (endpoint.child_id, endpoint.port_name, previous_link, link.name),
                        "one child port cannot belong to multiple parent links",
                    )
                )

    for index, first in enumerate(block.children):
        for second in block.children[index + 1 :]:
            if first.bounds.overlaps(second.bounds):
                findings.append(
                    BlockFinding(
                        "child-block-overlap",
                        path,
                        (first.block.block_id, second.block.block_id),
                        "sibling blocks have positive-area overlap",
                    )
                )
                continue
            clearance = first.bounds.distance_to(second.bounds)
            if clearance + 1e-9 < block.minimum_child_spacing:
                findings.append(
                    BlockFinding(
                        "child-block-spacing",
                        path,
                        (first.block.block_id, second.block.block_id),
                        f"sibling clearance {clearance:.4f} is below "
                        f"{block.minimum_child_spacing:.4f}",
                    )
                )
    return findings


@dataclass(frozen=True)
class BlockPlan:
    """A complete nested block composition ready to flatten into a layout."""

    root: LayoutBlock
    origin_x: float = 0.0
    origin_y: float = 0.0

    def findings(self) -> tuple[BlockFinding, ...]:
        return tuple(_local_findings(self.root, path=self.root.block_id, owners={}))

    def validate(self) -> None:
        findings = self.findings()
        if not findings:
            return
        summary = "; ".join(
            f"{finding.code} at {finding.block_path}: {finding.message}" for finding in findings
        )
        raise ToolchainError(f"invalid schematic block plan: {summary}")

    def positions(self) -> dict[str, Position]:
        self.validate()
        result: dict[str, Position] = {}

        def visit(block: LayoutBlock, offset_x: float, offset_y: float) -> None:
            for item in block.items:
                result[item.symbol_id] = replace(
                    item.position,
                    x=item.position.x + offset_x,
                    y=item.position.y + offset_y,
                )
            for child in block.children:
                visit(child.block, offset_x + child.x, offset_y + child.y)

        visit(self.root, self.origin_x, self.origin_y)
        return result

    def block_bounds(self) -> dict[str, Rect]:
        """Return deterministic absolute envelopes for inspection and review."""

        self.validate()
        result: dict[str, Rect] = {}

        def visit(block: LayoutBlock, path: str, offset_x: float, offset_y: float) -> None:
            result[path] = Rect(offset_x, offset_y, block.width, block.height)
            for child in block.children:
                child_path = f"{path}/{child.block.block_id}"
                visit(child.block, child_path, offset_x + child.x, offset_y + child.y)

        visit(self.root, self.root.block_id, self.origin_x, self.origin_y)
        return result

    def to_layout_plan(self, instance_ref: str, source_path: Path) -> LayoutPlan:
        return LayoutPlan((ModuleLayout(instance_ref, source_path, self.positions()),))


def compose_row(
    block_id: str,
    children: tuple[LayoutBlock, ...],
    *,
    gap: float = 100.0,
    padding: float = 0.0,
    alignment: str = "center",
) -> LayoutBlock:
    """Compose child blocks left-to-right with deterministic whitespace."""

    if not children:
        raise ToolchainError("a block row requires at least one child")
    if gap < 0 or padding < 0:
        raise ToolchainError("block row gap and padding cannot be negative")
    if alignment not in {"start", "center", "end"}:
        raise ToolchainError(f"unsupported row alignment: {alignment}")
    content_height = max(child.height for child in children)
    placed: list[PlacedBlock] = []
    cursor_x = padding
    for child in children:
        if alignment == "start":
            y = padding
        elif alignment == "end":
            y = padding + content_height - child.height
        else:
            y = padding + (content_height - child.height) / 2
        placed.append(PlacedBlock(child, cursor_x, y))
        cursor_x += child.width + gap
    width = 2 * padding + sum(child.width for child in children) + gap * (len(children) - 1)
    return LayoutBlock(
        block_id,
        width,
        content_height + 2 * padding,
        children=tuple(placed),
        minimum_child_spacing=gap,
    )


def compose_linked_row(
    block_id: str,
    children: tuple[LayoutBlock, ...],
    links: tuple[BlockLink, ...],
    *,
    gap: float = 100.0,
    padding: float = 0.0,
) -> LayoutBlock:
    """Compose a row while aligning linked left/right boundary ports.

    Each child after the first is translated to the median alignment requested
    by links to already placed children. This makes interface geometry, rather
    than arbitrary rectangle centres, control vertical placement.
    """

    if not children:
        raise ToolchainError("a linked block row requires at least one child")
    if gap < 0 or padding < 0:
        raise ToolchainError("linked block row gap and padding cannot be negative")
    children_by_id = {child.block_id: child for child in children}
    if len(children_by_id) != len(children):
        raise ToolchainError("linked block row child identifiers must be unique")

    port_offsets = {
        (child.block_id, port.name): port.offset
        for child in children
        for port in child.ports
        if port.side in {BlockSide.LEFT, BlockSide.RIGHT}
    }
    raw_y: dict[str, float] = {}
    cursor_x = padding
    raw_placements: list[tuple[LayoutBlock, float, float]] = []
    placed_ids: set[str] = set()
    for child in children:
        requested_y: list[float] = []
        for link in links:
            current = [
                endpoint for endpoint in link.endpoints if endpoint.child_id == child.block_id
            ]
            previous = [endpoint for endpoint in link.endpoints if endpoint.child_id in placed_ids]
            for current_endpoint in current:
                current_offset = port_offsets.get(
                    (current_endpoint.child_id, current_endpoint.port_name)
                )
                if current_offset is None:
                    continue
                for previous_endpoint in previous:
                    previous_offset = port_offsets.get(
                        (previous_endpoint.child_id, previous_endpoint.port_name)
                    )
                    if previous_offset is not None:
                        requested_y.append(
                            raw_y[previous_endpoint.child_id] + previous_offset - current_offset
                        )
        y = float(median(requested_y)) if requested_y else 0.0
        raw_y[child.block_id] = y
        raw_placements.append((child, cursor_x, y))
        placed_ids.add(child.block_id)
        cursor_x += child.width + gap

    minimum_y = min(y for _, _, y in raw_placements)
    maximum_y = max(y + child.height for child, _, y in raw_placements)
    y_shift = padding - minimum_y
    placed = tuple(PlacedBlock(child, x, y + y_shift) for child, x, y in raw_placements)
    width = 2 * padding + sum(child.width for child in children) + gap * (len(children) - 1)
    return LayoutBlock(
        block_id,
        width,
        maximum_y - minimum_y + 2 * padding,
        children=placed,
        links=links,
        minimum_child_spacing=gap,
    )


def compose_column(
    block_id: str,
    children: tuple[LayoutBlock, ...],
    *,
    gap: float = 100.0,
    padding: float = 0.0,
    alignment: str = "center",
) -> LayoutBlock:
    """Compose child blocks top-to-bottom with deterministic whitespace."""

    if not children:
        raise ToolchainError("a block column requires at least one child")
    if gap < 0 or padding < 0:
        raise ToolchainError("block column gap and padding cannot be negative")
    if alignment not in {"start", "center", "end"}:
        raise ToolchainError(f"unsupported column alignment: {alignment}")
    content_width = max(child.width for child in children)
    placed: list[PlacedBlock] = []
    cursor_y = padding
    for child in children:
        if alignment == "start":
            x = padding
        elif alignment == "end":
            x = padding + content_width - child.width
        else:
            x = padding + (content_width - child.width) / 2
        placed.append(PlacedBlock(child, x, cursor_y))
        cursor_y += child.height + gap
    height = 2 * padding + sum(child.height for child in children) + gap * (len(children) - 1)
    return LayoutBlock(
        block_id,
        content_width + 2 * padding,
        height,
        children=tuple(placed),
        minimum_child_spacing=gap,
    )


def block_from_positions(
    block_id: str,
    positions: dict[str, Position],
    *,
    occupied: dict[str, Rect] | None = None,
    content_bounds: Rect | None = None,
    padding: float = 40.0,
    ports: tuple[BlockPort, ...] = (),
    regions: tuple[BlockRegion, ...] = (),
) -> LayoutBlock:
    """Normalize existing same-space placements into one local leaf block.

    This is the migration bridge from legacy module positions to block-local
    geometry. Component envelopes participate in sizing. Callers can include
    pins, net glyphs and annotation allowances in content_bounds without
    confusing those drawing extents with component-body collision rectangles.
    Without content_bounds, body-less symbols contribute only their anchors.
    """

    if not positions:
        raise ToolchainError("a generated leaf block requires at least one positioned symbol")
    if padding < 0:
        raise ToolchainError("leaf block padding cannot be negative")
    occupied = occupied or {}
    unknown_envelopes = set(occupied) - set(positions)
    if unknown_envelopes:
        raise ToolchainError(
            "leaf block envelopes reference unowned symbols: "
            + ", ".join(sorted(unknown_envelopes))
        )

    minimum_x = min(
        [position.x for position in positions.values()] + [bounds.x for bounds in occupied.values()]
    )
    minimum_y = min(
        [position.y for position in positions.values()] + [bounds.y for bounds in occupied.values()]
    )
    maximum_x = max(
        [position.x for position in positions.values()]
        + [bounds.right for bounds in occupied.values()]
    )
    maximum_y = max(
        [position.y for position in positions.values()]
        + [bounds.bottom for bounds in occupied.values()]
    )
    if content_bounds is not None:
        minimum_x = min(minimum_x, content_bounds.x)
        minimum_y = min(minimum_y, content_bounds.y)
        maximum_x = max(maximum_x, content_bounds.right)
        maximum_y = max(maximum_y, content_bounds.bottom)
    origin_x = minimum_x - padding
    origin_y = minimum_y - padding
    items = tuple(
        BlockItem(
            symbol_id,
            replace(position, x=position.x - origin_x, y=position.y - origin_y),
            (
                bounds.translated(-origin_x, -origin_y)
                if (bounds := occupied.get(symbol_id)) is not None
                else None
            ),
        )
        for symbol_id, position in sorted(positions.items())
    )
    return LayoutBlock(
        block_id,
        maximum_x - minimum_x + 2 * padding,
        maximum_y - minimum_y + 2 * padding,
        items=items,
        ports=ports,
        regions=regions,
    )


def place_boundary_annotations(
    positions: dict[str, Position],
    occupied: dict[str, Rect],
    sides: dict[str, BlockSide],
    *,
    along_order: dict[str, float] | None = None,
    clearance: float = 80.0,
    pitch: float = 100.0,
) -> dict[str, Position]:
    """Place body-less symbols in ordered lanes around their block bodies.

    Legacy net-symbol coordinates must not inflate a migrated block or preserve
    arbitrary whitespace from an earlier sheet layout. The caller classifies
    each annotation's semantic side; this primitive assigns deterministic
    block-local lanes while preserving rotation and mirror state.
    """

    if clearance < 0 or pitch <= 0:
        raise ToolchainError("annotation clearance must be non-negative and pitch positive")
    if not occupied:
        return dict(positions)
    along_order = along_order or {}
    unknown = (set(sides) | set(along_order)) - (set(positions) - set(occupied))
    if unknown:
        raise ToolchainError(
            "annotation sides reference absent or body-owning symbols: "
            + ", ".join(sorted(unknown))
        )

    minimum_x = min(bounds.x for bounds in occupied.values())
    minimum_y = min(bounds.y for bounds in occupied.values())
    maximum_x = max(bounds.right for bounds in occupied.values())
    maximum_y = max(bounds.bottom for bounds in occupied.values())
    center_x = (minimum_x + maximum_x) / 2
    center_y = (minimum_y + maximum_y) / 2
    result = dict(positions)
    for side in BlockSide:
        symbol_ids = sorted(
            (symbol_id for symbol_id, candidate in sides.items() if candidate == side),
            key=lambda symbol_id: (
                along_order.get(
                    symbol_id,
                    (
                        positions[symbol_id].x
                        if side in {BlockSide.TOP, BlockSide.BOTTOM}
                        else positions[symbol_id].y
                    ),
                ),
                symbol_id,
            ),
        )
        middle = (len(symbol_ids) - 1) / 2
        for index, symbol_id in enumerate(symbol_ids):
            position = positions[symbol_id]
            along = (index - middle) * pitch
            if side == BlockSide.TOP:
                x, y = center_x + along, minimum_y - clearance
            elif side == BlockSide.BOTTOM:
                x, y = center_x + along, maximum_y + clearance
            elif side == BlockSide.LEFT:
                x, y = minimum_x - clearance, center_y + along
            else:
                x, y = maximum_x + clearance, center_y + along
            result[symbol_id] = replace(position, x=x, y=y)
    return result
