from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from schemer.core.diagnostics import record_draft_issue
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position
from schemer.placement.blocks.model import BlockFinding, LayoutBlock, Rect
from schemer.placement.blocks.validation import local_findings


@dataclass(frozen=True)
class BlockPlan:
    """A complete nested block composition ready to flatten into a layout."""

    root: LayoutBlock
    origin_x: float = 0.0
    origin_y: float = 0.0

    def findings(self) -> tuple[BlockFinding, ...]:
        return tuple(local_findings(self.root, path=self.root.block_id, owners={}))

    def validate(self) -> None:
        findings = self.findings()
        findings = tuple(finding for finding in findings if not (
            finding.code in {"item-body-overlap", "child-block-overlap", "child-block-spacing"}
            and record_draft_issue(finding.code, f"{finding.block_path}: {finding.message}",
                                   finding.subjects)
        ))
        if not findings:
            return
        summary = "; ".join(
            f"{finding.code} at {finding.block_path} ({', '.join(finding.subjects)}): "
            f"{finding.message}" for finding in findings
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
