"""Shadow-only symbol repair for the automatically selected primary IC."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schemer.layout import LayoutPlan
from schemer.layout_metrics import primary_component
from schemer.shadow import find_workspace_root
from schemer.symbol_geometry import symbol_pin_electrical_types, symbol_pin_numbers
from schemer.toolchain import ToolchainError

_GENERIC_TERMINAL = re.compile(r"(?:pin|pad|p)?[_-]?(\d+)", re.IGNORECASE)
_VALID_PIN_TYPES = {
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


@dataclass(frozen=True)
class PrimaryProjection:
    """One selected primary component and any repaired symbol-library source."""

    component_ref: str
    file_overrides: dict[Path, str]
    ordered_perimeter: bool


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


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def ordered_perimeter_symbol(instance: dict[str, Any], symbol_name: str) -> str | None:
    """Replace a generic sequential-pin box with a physical perimeter order.

    Eligible components expose an even, contiguous numeric pin range and only
    generic terminal names such as ``Pin_1``. The first half runs down the left
    edge; the second half runs up the right edge. Components with meaningful
    functional pin names retain their authored symbol.
    """

    children = instance.get("children")
    if not isinstance(children, dict):
        return None
    named_numbers = symbol_pin_numbers(instance)
    electrical_types = symbol_pin_electrical_types(instance)
    pins: list[tuple[int, str, str]] = []
    for terminal in children:
        if not isinstance(terminal, str):
            return None
        number = named_numbers.get(terminal)
        terminal_match = _GENERIC_TERMINAL.fullmatch(terminal)
        if number is None or not number.isdigit() or terminal_match is None:
            return None
        if int(terminal_match.group(1)) != int(number):
            return None
        electrical_type = electrical_types.get(terminal) or electrical_types.get(number)
        pins.append(
            (
                int(number),
                terminal,
                electrical_type if electrical_type in _VALID_PIN_TYPES else "passive",
            )
        )

    pins.sort()
    if len(pins) < 8 or len(pins) % 2 or [pin[0] for pin in pins] != list(range(1, len(pins) + 1)):
        return None

    side_count = len(pins) // 2
    # A two-row module is an attachment surface, not a compact PCB footprint.
    # Five millimetres per row leaves room for ordinary reference/value text
    # and local branches without changing the eventual output scale.
    pitch = 5.08
    pin_length = 5.08
    half_width = 12.7
    row_half_span = (side_count - 1) * pitch / 2
    half_height = row_half_span + pitch
    left = pins[:side_count]
    right = pins[side_count:]
    placements: list[tuple[tuple[int, str, str], float, float, int]] = []
    for index, pin in enumerate(left):
        placements.append((pin, -(half_width + pin_length), row_half_span - index * pitch, 0))
    for index, pin in enumerate(right):
        placements.append((pin, half_width + pin_length, -row_half_span + index * pitch, 180))

    pin_blocks = [
        f'''      (pin {pin_type} line (at {x:.2f} {y:.2f} {angle}) (length {pin_length:.2f})
        (name "{_escape(terminal)}" (effects (font (size 1.27 1.27))))
        (number "{number}" (effects (font (size 1.27 1.27))))
      )'''
        for (number, terminal, pin_type), x, y, angle in placements
    ]
    escaped_name = _escape(symbol_name)
    reference = _escape(_attribute_string(instance, "prefix") or "U")
    value = _escape(_attribute_string(instance, "value") or symbol_name)
    return f'''(kicad_symbol_lib (version 20220914) (generator schemer)
  (symbol "{escaped_name}" (pin_names (offset 1.016) (hide yes))
    (in_bom yes) (on_board yes)
    (property "Reference" "{reference}" (at {-half_width:.2f} {-half_height - 2.54:.2f} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{value}" (at 0 {half_height + 2.54:.2f} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (symbol "{escaped_name}_1_1"
      (rectangle
        (start {-half_width:.2f} {half_height:.2f})
        (end {half_width:.2f} {-half_height:.2f})
        (stroke (width 0.254) (type solid)) (fill (type background)))
{chr(10).join(pin_blocks)}
    )
  )
)
'''


def project_primary_ic_symbol(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    entrypoint: Path,
) -> PrimaryProjection:
    """Repair a generic primary symbol without imposing an output scale.

    Primary selection depends only on component structure. Authored functional
    symbols pass through unchanged; an eligible generic sequential-pin symbol
    receives a coherent perimeter ordering in the proposal shadow.
    """

    proposed = plan.apply_to_schematic(schematic)
    primary = primary_component(proposed)
    instances = schematic.get("instances")
    if not isinstance(instances, dict):
        raise ToolchainError("schematic instances must be an object")
    component = instances.get(primary.instance_ref)
    if not isinstance(component, dict):
        raise ToolchainError(f"selected primary IC is absent: {primary.instance_ref}")

    symbol_name = _attribute_string(component, "symbol_name")
    replacement = (
        ordered_perimeter_symbol(component, symbol_name) if symbol_name is not None else None
    )
    if replacement is None:
        return PrimaryProjection(
            component_ref=primary.instance_ref,
            file_overrides={},
            ordered_perimeter=False,
        )

    workspace = find_workspace_root(entrypoint)
    symbol_path = _attribute_string(component, "symbol_path")
    if symbol_path is None or not symbol_path.startswith("package://"):
        raise ToolchainError("selected primary IC symbol is not a workspace package path")
    relative = Path(symbol_path.removeprefix("package://"))
    if relative.parts and relative.parts[0] == "workspace":
        relative = Path(*relative.parts[1:])
    source_path = (workspace / relative).resolve()
    if not source_path.is_relative_to(workspace) or not source_path.is_file():
        raise ToolchainError(f"selected primary IC symbol path is invalid: {symbol_path}")

    users = {
        instance_ref
        for instance_ref, instance in instances.items()
        if isinstance(instance_ref, str)
        and isinstance(instance, dict)
        and _attribute_string(instance, "symbol_path") == symbol_path
    }
    if users != {primary.instance_ref}:
        raise ToolchainError(
            "cannot replace a shared symbol library for the primary presentation: "
            f"{source_path} is used by {len(users)} components"
        )
    return PrimaryProjection(
        component_ref=primary.instance_ref,
        file_overrides={source_path: replacement},
        ordered_perimeter=True,
    )
