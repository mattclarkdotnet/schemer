"""Generate selected functional modules as deterministic layout blocks."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from schemer.blocks import BlockPlan
from schemer.heuristic_block import (
    functional_ic_block_from_zero,
    multi_active_interface_block_from_zero,
    primary_ic_block_from_zero,
)
from schemer.hints import Hint, HintSet
from schemer.layout import LayoutPlan, ModuleLayout
from schemer.toolchain import ToolchainError


@dataclass(frozen=True)
class BlockCompositionResult:
    """A layout plan plus the block trees that generated selected modules."""

    plan: LayoutPlan
    module_blocks: tuple[tuple[str, BlockPlan], ...]
    applied_hints: tuple[tuple[str, str], ...] = ()
    sheet_hints: tuple[Hint, ...] = ()

    def preserve_completed_modules(self, candidate: LayoutPlan) -> LayoutPlan:
        """Keep legacy sheet refinements from tearing completed blocks apart."""
        completed_refs = {ref for ref, _ in self.module_blocks}
        completed = {
            module.instance_ref: module
            for module in self.plan.modules
            if module.instance_ref in completed_refs
        }
        return replace(candidate, modules=tuple(
            completed.get(module.instance_ref, module) for module in candidate.modules
        ))

    def as_manifest(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for module_ref, block_plan in self.module_blocks:
            bounds = block_plan.block_bounds()
            modules.append(
                {
                    "block_count": len(bounds),
                    "blocks": {
                        path: {
                            "height": rectangle.height,
                            "width": rectangle.width,
                            "x": rectangle.x,
                            "y": rectangle.y,
                        }
                        for path, rectangle in sorted(bounds.items())
                    },
                    "finding_count": len(block_plan.findings()),
                    "module_ref": module_ref,
                    "symbol_count": len(block_plan.positions()),
                }
            )
        return {
            "modules": modules,
            "schema_version": "schemer-block-composition-v1",
            "hints": [
                {"module_ref": module, "id": hint_id, "status": "applied"}
                for module, hint_id in self.applied_hints
            ],
        }


def generate_functional_ic_blocks(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    padding: float = 40.0,
    allow_experimental_hints: bool = False,
) -> BlockCompositionResult:
    """Rebuild eligible connector/IC modules from topology and pin geometry.

    Existing positions do not participate. Unsupported modules pass through
    unchanged. Selection never uses a board name, reference designator, source
    path, or part number.
    """

    if padding < 0:
        raise ToolchainError("block composition padding must be non-negative")

    modules: list[ModuleLayout] = []
    generated: list[tuple[str, BlockPlan]] = []
    applied: list[tuple[str, str]] = []
    sheet_hints: tuple[Hint, ...] = ()
    for module in plan.modules:
        hints = HintSet.from_source(module.source_path, allow_experimental=allow_experimental_hints)
        if module.instance_ref == schematic.get("root_ref"):
            sheet_hints = tuple(hint for hint in hints.hints if hint.kind == "right-of")
            hints = HintSet(tuple(hint for hint in hints.hints if hint.kind != "right-of"))
        block = multi_active_interface_block_from_zero(
            schematic, module, padding=padding, hints=hints
        )
        if block is None:
            block = functional_ic_block_from_zero(schematic, module, padding=padding)
        if block is None:
            block = primary_ic_block_from_zero(schematic, module, padding=padding)
        if block is None:
            hints.require_all_applied()
            modules.append(module)
            continue
        hints.require_all_applied()
        applied.extend((module.instance_ref, hint.id) for hint in hints.hints)
        modules.append(replace(module, positions=block.positions()))
        generated.append((module.instance_ref, block))
    return BlockCompositionResult(
        replace(plan, modules=tuple(modules)), tuple(generated), tuple(applied), sheet_hints
    )
