"""Generic package-level presentation for separately drawn multi-unit ICs."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from statistics import median
from typing import Any

from schemer.layout import LayoutPlan, ModuleLayout, Position
from schemer.shadow import find_workspace_root
from schemer.symbol_geometry import (
    symbol_pin_electrical_types,
    symbol_pin_numbers,
    symbol_pin_offsets,
)
from schemer.toolchain import ToolchainError


@dataclass(frozen=True)
class PackageProjection:
    """A layout plus shadow-only symbol files for package-body presentation."""

    plan: LayoutPlan
    file_overrides: dict[Path, str]
    collapsed_component_refs: tuple[str, ...]


def _attribute_string(instance: dict[str, Any], name: str) -> str | None:
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return None
    value = attributes.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("String"), str):
        return value["String"]
    return None


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)
    )


def _terminal_names(component_ref: str, schematic: dict[str, Any]) -> tuple[str, ...]:
    nets = schematic.get("nets")
    if not isinstance(nets, dict):
        raise ToolchainError("schematic nets must be an object")
    prefix = component_ref + "."
    terminals: set[str] = set()
    for net in nets.values():
        ports = net.get("ports") if isinstance(net, dict) else None
        if not isinstance(ports, list):
            continue
        for port_ref in ports:
            if not isinstance(port_ref, str) or not port_ref.startswith(prefix):
                continue
            terminal = port_ref.removeprefix(prefix)
            if "." not in terminal:
                terminals.add(terminal)
    return tuple(sorted(terminals, key=_natural_key))


def _terminal_net_kind(component_ref: str, terminal: str, schematic: dict[str, Any]) -> str:
    port_ref = f"{component_ref}.{terminal}"
    nets = schematic.get("nets", {})
    for net in nets.values() if isinstance(nets, dict) else ():
        if isinstance(net, dict) and port_ref in net.get("ports", []):
            return str(net.get("kind", "")).casefold()
    return ""


def _physical_pin_number(
    terminal: str,
    offsets: dict[str, tuple[float, float]],
    named_numbers: dict[str, str],
) -> str:
    if terminal.isdigit():
        return terminal
    if terminal in named_numbers:
        return named_numbers[terminal]
    offset = offsets.get(terminal)
    numeric_aliases = sorted(
        (name for name, candidate in offsets.items() if name.isdigit() and candidate == offset),
        key=lambda name: int(name),
    )
    if not numeric_aliases:
        raise ToolchainError(f"multi-unit terminal {terminal!r} has no physical pin-number alias")
    return numeric_aliases[0]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _functional_caption_lines(
    instance: dict[str, Any], *, max_lines: int = 2
) -> tuple[str, ...]:
    """Wrap an evidenced semantic type, without inventing a device function."""
    component_type = _attribute_string(instance, "type")
    if component_type is None:
        return ()
    words = component_type.replace("_", " ").replace("-", " ").split()
    if not words or any(len(word) > 12 for word in words):
        return ()
    lines: list[str] = []
    for word in words:
        candidate = f"{lines[-1]} {word}" if lines else word
        if lines and len(candidate) <= 12:
            lines[-1] = candidate
        else:
            lines.append(word)
    return tuple(lines) if len(lines) <= max_lines else ()


def _package_body_symbol(
    symbol_name: str,
    component_ref: str,
    instance: dict[str, Any],
    schematic: dict[str, Any],
) -> str:
    """Generate one conventional DIP-style body without changing pin identity."""

    offsets = symbol_pin_offsets(instance)
    named_numbers = symbol_pin_numbers(instance)
    terminals = _terminal_names(component_ref, schematic)
    if len(terminals) < 6:
        raise ToolchainError(f"multi-unit package has too few evidenced terminals: {component_ref}")
    electrical_types = symbol_pin_electrical_types(instance)
    pins = sorted(
        (
            (
                _physical_pin_number(terminal, offsets, named_numbers),
                terminal,
                electrical_types.get(terminal)
                or electrical_types.get(_physical_pin_number(terminal, offsets, named_numbers))
                or "passive",
            )
            for terminal in terminals
        ),
        key=lambda item: (int(item[0]) if item[0].isdigit() else 10**9, _natural_key(item[0])),
    )
    if len({number for number, _, _ in pins}) != len(pins):
        raise ToolchainError(
            f"multi-unit package has duplicate physical pin aliases: {component_ref}"
        )

    top = [
        pin
        for pin in pins
        if pin[2].startswith("power")
        and _terminal_net_kind(component_ref, pin[1], schematic) != "ground"
    ]
    bottom = [
        pin
        for pin in pins
        if pin[2].startswith("power")
        and _terminal_net_kind(component_ref, pin[1], schematic) == "ground"
    ]
    left = [pin for pin in pins if pin[2] in {"input", "open_collector", "open_emitter"}]
    right = [pin for pin in pins if pin[2] in {"output", "tri_state"}]
    assigned = {number for group in (top, bottom, left, right) for number, _, _ in group}
    remainder = [pin for pin in pins if pin[0] not in assigned]
    for pin in remainder:
        (left if len(left) <= len(right) else right).append(pin)

    left_count = len(left)
    right_count = len(right)
    # Keep pin rows on a conventional imperial schematic grid.
    # An arbitrary 9 mm pitch makes the viewer route exits off their pin axes,
    # creating little doglegs even when our attached-part coordinates agree.
    # 400 mil leaves annotation room and keeps half-pitch rows on the lattice.
    pitch = 10.16
    half_width = 7.62
    half_height = max(10.16, (max(left_count, right_count) - 1) * pitch / 2 + pitch)

    pin_blocks: list[str] = []
    placements: list[tuple[tuple[str, str, str], float, float, int]] = []
    for index, pin in enumerate(left):
        x = -(half_width + 5.08)
        y = (left_count - 1) * pitch / 2 - index * pitch
        angle = 0
        placements.append((pin, x, y, angle))
    for right_index, pin in enumerate(right):
        x = half_width + 5.08
        y = (right_count - 1) * pitch / 2 - right_index * pitch
        angle = 180
        placements.append((pin, x, y, angle))
    for index, pin in enumerate(top):
        x = (index - (len(top) - 1) / 2) * pitch
        y = half_height + 5.08
        placements.append((pin, x, y, 270))
    for index, pin in enumerate(bottom):
        x = (index - (len(bottom) - 1) / 2) * pitch
        y = -(half_height + 5.08)
        placements.append((pin, x, y, 90))

    valid_pin_types = {
        "bidirectional",
        "input",
        "no_connect",
        "open_collector",
        "open_emitter",
        "output",
        "passive",
        "power_in",
        "power_out",
        "tri_state",
        "unspecified",
    }
    for (number, terminal, electrical_type), x, y, angle in placements:
        pin_type = electrical_type if electrical_type in valid_pin_types else "passive"
        pin_blocks.append(
            f'''      (pin {pin_type} line (at {x:.2f} {y:.2f} {angle}) (length 5.08)
        (name "{_escape(terminal)}" (effects (font (size 1.27 1.27))))
        (number "{_escape(number)}" (effects (font (size 1.27 1.27))))
      )'''
        )

    caption_lines = _functional_caption_lines(instance, max_lines=3)
    # Use only the free centre between numeric side-pin labels. Meaningful
    # functional pin names must not be obscured by a guessed text envelope.
    side_pins = [item for item in placements if item[3] in {0, 180}]
    caption_half_height = ((len(caption_lines) - 1) * 1.905 + 1.27) / 2
    caption_fits = (
        bool(side_pins)
        and all(terminal == number for (number, terminal, _), _, _, _ in side_pins)
        and all(abs(y) >= caption_half_height + 1.27 for _, _, y, _ in side_pins)
    )
    if not caption_fits:
        caption_lines = ()
    caption_blocks = [
        f'''      (text "{_escape(line)}"
        (at 0 {(len(caption_lines) - 1) * 1.905 / 2 - index * 1.905:.4f} 0)
        (effects (font (size 1.27 1.27))))'''
        for index, line in enumerate(caption_lines)
    ]
    escaped_name = _escape(symbol_name)
    return f'''(kicad_symbol_lib (version 20220914) (generator schemer)
  (symbol "{escaped_name}" (pin_names (offset 1.016)) (in_bom yes) (on_board yes)
    (property "Reference" "U" (at {-half_width:.2f} {-half_height - 2.54:.2f} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{escaped_name}" (at 0 {half_height + 2.54:.2f} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (symbol "{escaped_name}_1_1"
{chr(10).join(caption_blocks)}
      (rectangle
        (start {-half_width:.2f} {half_height:.2f})
        (end {half_width:.2f} {-half_height:.2f})
        (stroke (width 0.1524) (type solid)) (fill (type none)))
{chr(10).join(pin_blocks)}
    )
  )
)
'''


def _symbol_source_path(instance: dict[str, Any], workspace: Path) -> Path:
    symbol_path = _attribute_string(instance, "symbol_path")
    if symbol_path is None or not symbol_path.startswith("package://"):
        raise ToolchainError("multi-unit package symbol is not a workspace package path")
    relative = Path(symbol_path.removeprefix("package://"))
    if relative.parts and relative.parts[0] == "workspace":
        relative = Path(*relative.parts[1:])
    source = (workspace / relative).resolve()
    if not source.is_relative_to(workspace) or not source.is_file():
        raise ToolchainError(f"multi-unit package symbol path is invalid: {symbol_path}")
    return source


def collapse_multi_unit_ic_packages(
    schematic: dict[str, Any], plan: LayoutPlan, entrypoint: Path
) -> PackageProjection:
    """Collapse any physical IC drawn as three or more independent units.

    Detection uses evaluated physical-component identity and the viewer's unit
    position suffixes. The generated shadow symbol preserves every evidenced
    physical terminal and never changes the board's source component package.
    """

    instances = schematic.get("instances")
    if not isinstance(instances, dict):
        raise ToolchainError("schematic instances must be an object")
    workspace = find_workspace_root(entrypoint)
    all_symbol_users: dict[Path, set[str]] = {}
    for instance_ref, instance in instances.items():
        if not isinstance(instance_ref, str) or not isinstance(instance, dict):
            continue
        if instance.get("kind") != "Component":
            continue
        symbol_path = _attribute_string(instance, "symbol_path")
        if not symbol_path or not symbol_path.startswith("package://"):
            continue
        relative = Path(symbol_path.removeprefix("package://"))
        if relative.parts and relative.parts[0] == "workspace":
            relative = Path(*relative.parts[1:])
        source = (workspace / relative).resolve()
        all_symbol_users.setdefault(source, set()).add(instance_ref)

    modules: list[ModuleLayout] = []
    overrides: dict[Path, str] = {}
    collapsed_refs: list[str] = []
    for module in plan.modules:
        grouped: dict[str, list[tuple[str, Position]]] = {}
        for symbol_id, position in module.positions.items():
            if not symbol_id.startswith("comp:") or "@" not in symbol_id:
                continue
            base_id = symbol_id.split("@", 1)[0]
            grouped.setdefault(base_id, []).append((symbol_id, position))

        updated = dict(module.positions)
        for base_id, units in sorted(grouped.items()):
            if len(units) < 3:
                continue
            local_ref = base_id.removeprefix("comp:")
            component_ref = f"{module.instance_ref}.{local_ref}"
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            if len(symbol_pin_offsets(component)) < 6:
                continue
            source_path = _symbol_source_path(component, workspace)
            users = all_symbol_users.get(source_path, set())
            if users != {component_ref}:
                raise ToolchainError(
                    "cannot replace a shared symbol library with a package-body projection: "
                    f"{source_path} is used by {len(users)} components"
                )
            symbol_name = _attribute_string(component, "symbol_name")
            if symbol_name is None:
                raise ToolchainError(f"multi-unit package has no symbol name: {component_ref}")
            overrides[source_path] = _package_body_symbol(
                symbol_name, component_ref, component, schematic
            )
            for symbol_id, _ in units:
                updated.pop(symbol_id)
            updated[base_id] = Position(
                x=float(median(position.x for _, position in units)),
                y=float(median(position.y for _, position in units)),
                rotation=0,
            )
            collapsed_refs.append(component_ref)
        modules.append(replace(module, positions=updated))

    return PackageProjection(
        plan=replace(plan, modules=tuple(modules)),
        file_overrides=overrides,
        collapsed_component_refs=tuple(sorted(collapsed_refs)),
    )
