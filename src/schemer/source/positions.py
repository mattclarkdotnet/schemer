from __future__ import annotations

import difflib
import re
from pathlib import Path

from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position

_POSITION_PREFIX = "# pcb:sch "


def proposed_sources(plan: LayoutPlan) -> dict[Path, str]:
    updates: dict[Path, str] = {}
    for module in plan.modules:
        content = module.source_path.read_text()
        updates[module.source_path] = replace_position_block(
            content, source_position_ids(module),
        )
    return updates


def source_position_ids(module: ModuleLayout) -> dict[str, Position]:
    """Translate evaluated net names back to the names accepted by a module source."""

    marker = ":<root>."
    module_path = (
        module.instance_ref.split(marker, 1)[1]
        if marker in module.instance_ref
        else ""
    )
    result: dict[str, Position] = {}
    for symbol_id, position in module.positions.items():
        source_id = symbol_id
        if symbol_id.startswith("sym:"):
            net_symbol = symbol_id.removeprefix("sym:")
            net_name, separator, suffix = net_symbol.rpartition("#")
            if not separator or not suffix.isdigit():
                raise ToolchainError(f"invalid schematic net-symbol ID: {symbol_id}")
            source_name = module.source_net_names.get(net_name)
            if source_name is None and module_path and net_name.startswith(module_path + "."):
                source_name = net_name.removeprefix(module_path + ".")
            source_id = f"sym:{source_name or net_name}#{suffix}"
        if source_id in result:
            raise ToolchainError(
                f"multiple evaluated positions resolve to source symbol {source_id}"
            )
        result[source_id] = position
    return result


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)
    )


def symbol_id_to_comment_key(symbol_id: str) -> str:
    """Match pcb-sch's conversion from stable viewer ID to comment key."""

    if symbol_id.startswith("comp:"):
        return symbol_id.removeprefix("comp:")
    if symbol_id.startswith("sym:"):
        return symbol_id.removeprefix("sym:").replace("#", ".")
    raise ToolchainError(f"invalid schematic symbol ID: {symbol_id}")


def position_block_start(content: str) -> int:
    """Find the final contiguous pcb:sch/blank block using pcb-sch semantics."""

    block_start = len(content)
    for line in reversed(content.splitlines(keepends=True)):
        trimmed = line.strip()
        if not trimmed or trimmed.startswith(_POSITION_PREFIX):
            block_start -= len(line)
            continue
        break
    return block_start


def format_position_block(positions: dict[str, Position]) -> str:
    """Format stable viewer IDs as a naturally ordered pcb:sch block."""

    by_comment_key = {
        symbol_id_to_comment_key(symbol_id): position for symbol_id, position in positions.items()
    }
    lines: list[str] = []
    for element_id in sorted(by_comment_key, key=_natural_key):
        position = by_comment_key[element_id]
        mirror = f" mirror={position.mirror}" if position.mirror is not None else ""
        lines.append(
            f"{_POSITION_PREFIX}{element_id} "
            f"x={position.x:.4f} y={position.y:.4f} rot={position.rotation:.0f}{mirror}\n"
        )
    return "".join(lines)


def replace_position_block(content: str, positions: dict[str, Position]) -> str:
    """Replace only the final persisted layout block."""

    prefix = content[: position_block_start(content)]
    block = format_position_block(positions)
    if not block:
        return prefix
    if not prefix:
        return block
    if prefix.endswith("\n\n"):
        separator = ""
    elif prefix.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return prefix + separator + block


def source_diff(path: Path, before: str, after: str) -> str:
    """Return a unified diff for one proposed source edit."""

    if before == after:
        return ""
    label = str(path)
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=label,
            tofile=label,
        )
    )
