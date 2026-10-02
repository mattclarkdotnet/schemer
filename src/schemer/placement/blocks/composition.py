from __future__ import annotations

from dataclasses import replace
from statistics import median

from schemer.core.errors import ToolchainError
from schemer.core.layout import Position
from schemer.placement.blocks.model import (
    BlockItem,
    BlockLink,
    BlockPort,
    BlockRegion,
    BlockSide,
    LayoutBlock,
    PlacedBlock,
    Rect,
)


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
