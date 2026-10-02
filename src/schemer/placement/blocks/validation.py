from __future__ import annotations

from schemer.placement.blocks.model import (
    BlockFinding,
    LayoutBlock,
    PortRef,
    RegionKind,
    port_point,
)


def local_findings(
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
        if port_point(block, port) is None:
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
        findings.extend(local_findings(child.block, path=child_path, owners=owners))

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
