"""Explicit named wire endpoints for separated interface blocks.

This is presentation metadata on existing nets, never additional components or
split electrical nets. The neutral one-pin drawing has no power/ground arrow.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from schemer.layout import LayoutPlan, resolve_module_position_ids
from schemer.symbol_geometry import _is_rail_net
from schemer.toolchain import ToolchainError

SYMBOL_NAME = "SignalTermination"
# Both bounds must be finite: a zero-width line makes the installed router
# detour around an otherwise collinear endpoint. The tiny end cap preserves
# a neutral wire-end appearance without a supply/ground arrow.
SYMBOL = '''(symbol "SignalTermination"
  (pin_numbers hide) (pin_names (offset 0) hide)
  (property "Reference" "#NET" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
  (property "Value" "" (at 0 0 0) (effects (font (size 1.27 1.27))))
  (symbol "SignalTermination_0_1"
    (polyline (pts (xy 0 0) (xy 0 1.27) (xy -0.127 1.27) (xy 0.127 1.27))
      (stroke (width 0) (type default))
      (fill (type none))))
  (symbol "SignalTermination_1_1"
    (pin power_in line (at 0 0 90) (length 0)
      (name "~" (effects (font (size 1.27 1.27))))
      (number "1" (effects (font (size 1.27 1.27)))))))'''
LIBRARY = f'(kicad_symbol_lib (version 20231120) (generator "schemer")\n{SYMBOL}\n)\n'
FILENAME = "SchemerSignalTermination.kicad_sym"


def with_signal_termination_symbols(schematic: dict[str, Any], plan: LayoutPlan) -> dict[str, Any]:
    """Preview the same symbol metadata that the source projection persists."""
    result = deepcopy(schematic)
    for module in plan.modules:
        counts = Counter(
            key[4:].rsplit("#", 1)[0]
            for key in resolve_module_position_ids(module, schematic) if key.startswith("sym:")
        )
        for name, count in counts.items():
            net = result["nets"][name]
            if count >= 2 and not _is_rail_net(name, net):
                properties = net.setdefault("properties", {})
                if not properties.get("__symbol_value"):
                    properties.update({"__symbol_value": SYMBOL, "symbol_name": SYMBOL_NAME})
    return result


def annotate_net_calls(source: str, names: set[str]) -> str:
    """Add a symbol to supported local Net constructors, retaining arguments.

    Parse, don't execute. Do not guess at imported aliases, returned interfaces
    or user-authored net symbols. Unsupported bindings fail explicitly.
    """
    lines = source.splitlines(keepends=True)
    edits = []
    found = set()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in names:
            continue
        call = node.value
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "Net"):
            raise ToolchainError(f"signal termination needs a local Net constructor: {target.id}")
        found.add(target.id)
        if any(keyword.arg == "symbol" for keyword in call.keywords):
            continue
        end = sum(len(line) for line in lines[:call.end_lineno - 1])
        end += len(lines[call.end_lineno - 1].encode()[:call.end_col_offset].decode()) - 1
        start = sum(len(line) for line in lines[:call.lineno - 1]) + call.col_offset
        # Ignore comments/newlines when checking for a trailing comma.
        tokens = list(tokenize.generate_tokens(io.StringIO(source[start:end] + ")").readline))
        meaningful = [token.string for token in tokens[:-1] if token.type not in {
            tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT,
        }]
        separator = "" if meaningful[-2] in {"(", ","} else ", "
        edits.append((end, f'{separator}symbol=Symbol("{FILENAME}")'))
    if found != names:
        raise ToolchainError(
            f"signal termination has no local Net binding: {sorted(names - found)}"
        )
    for end, text in sorted(edits, reverse=True):
        source = source[:end] + text + source[end:]
    return source


def signal_termination_sources(
    schematic: dict[str, Any], plan: LayoutPlan, updates: dict[Path, str],
) -> dict[Path, str]:
    """Persist explicit multiple plain-net endpoints in a proposal shadow."""
    overrides = {}
    for module in plan.modules:
        resolved = resolve_module_position_ids(module, schematic)
        counts = Counter(key[4:].rsplit("#", 1)[0] for key in resolved if key.startswith("sym:"))
        names = set()
        for name, count in counts.items():
            net = schematic["nets"].get(name, {})
            if count >= 2 and not _is_rail_net(name, net) and not net.get("properties", {}).get(
                "__symbol_value"
            ):
                names.add(name.rsplit(".", 1)[-1])
        if names:
            source = updates.get(module.source_path, module.source_path.read_text())
            overrides[module.source_path] = annotate_net_calls(source, names)
            overrides[module.source_path.parent / FILENAME] = LIBRARY
    return overrides
