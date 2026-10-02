from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from schemer.analysis.roles import module_functions, module_representations
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout
from schemer.placement.blocks.plan import BlockPlan
from schemer.placement.builders.functional import functional_ic_block_from_zero
from schemer.placement.builders.general import general_local_blocks
from schemer.placement.builders.interface import multi_active_interface_block_from_zero
from schemer.placement.builders.primary.pipeline import primary_ic_block_from_zero
from schemer.source.hints import Hint, HintSet


@dataclass(frozen=True)
class BlockCompositionResult:
    """A layout plan plus the block trees that generated selected modules."""

    plan: LayoutPlan
    module_blocks: tuple[tuple[str, BlockPlan], ...]
    applied_hints: tuple[tuple[str, str], ...] = ()
    sheet_hints: tuple[Hint, ...] = ()


def generate_functional_ic_blocks(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    *,
    padding: float = 40.0,
) -> BlockCompositionResult:
    """Rebuild eligible connector/IC modules from topology and pin geometry.

    Existing positions do not participate. Modules outside the specialised
    motifs use general local blocks. Selection never uses a board name, reference designator, source
    path, or part number.
    """

    if padding < 0:
        raise ToolchainError("block composition padding must be non-negative")

    module_functions(schematic.get("instances", {}))
    module_representations(schematic.get("instances", {}))

    modules: list[ModuleLayout] = []
    generated: list[tuple[str, BlockPlan]] = []
    applied: list[tuple[str, str]] = []
    sheet_hints: tuple[Hint, ...] = ()
    for module in sorted(plan.modules, key=lambda item: item.instance_ref.count("."), reverse=True):
        hints = HintSet.from_source(module.source_path)
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
            block = general_local_blocks(
                schematic, module, padding=padding,
                excluded_modules=tuple(other.instance_ref for other in plan.modules
                                       if other.instance_ref.startswith(module.instance_ref + ".")),
                child_blocks={ref: child for ref, child in generated
                              if f"comp:{ref.removeprefix(module.instance_ref + '.')}"
                              in module.positions},
            )
        if block is None:
            hints.require_all_applied()
            modules.append(module)
            continue
        hints.require_all_applied()
        applied.extend((module.instance_ref, hint.id) for hint in hints.hints)
        modules.append(replace(module, positions=block.positions()))
        generated.append((module.instance_ref, block))
    by_ref = {module.instance_ref: module for module in modules}
    return BlockCompositionResult(
        replace(plan, modules=tuple(by_ref[module.instance_ref] for module in plan.modules)),
        tuple(generated), tuple(applied), sheet_hints,
    )
