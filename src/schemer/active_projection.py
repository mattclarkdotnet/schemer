"""Conventional shadow symbols for active devices drawn as raw connectors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schemer.layout import LayoutPlan
from schemer.package_projection import (
    _attribute_string,
    _escape,
    _functional_caption_lines,
    _natural_key,
    _physical_pin_number,
    _symbol_source_path,
    _terminal_names,
    _terminal_net_kind,
)
from schemer.shadow import find_workspace_root
from schemer.symbol_geometry import (
    symbol_pin_electrical_types,
    symbol_pin_numbers,
    symbol_pin_offsets,
)
from schemer.toolchain import ToolchainError

_NON_ACTIVE_TYPES = {
    "capacitor",
    "connector",
    "diode",
    "ferrite_bead",
    "inductor",
    "mechanical",
    "mounting_hole",
    "resistor",
    "test_point",
    "transformer",
}

def pin_name_restates_number(name: str, number: str) -> bool:
    """Return whether a pin name adds no meaning beyond its physical number."""

    normalized_name = "".join(character for character in name.casefold() if character.isalnum())
    normalized_number = "".join(character for character in number.casefold() if character.isalnum())
    return normalized_name in {
        normalized_number,
        f"p{normalized_number}",
        f"pin{normalized_number}",
    }


@dataclass(frozen=True)
class ActiveBlockProjection:
    """Private symbol overrides that make active pin roles visually explicit."""

    file_overrides: dict[Path, str]
    projected_component_refs: tuple[str, ...]


def _active_body_symbol(
    symbol_name: str,
    component_ref: str,
    instance: dict[str, Any],
    schematic: dict[str, Any],
    *,
    forced_left: set[str] | None = None,
    forced_right: set[str] | None = None,
) -> str:
    forced_left = forced_left or set()
    forced_right = forced_right or set()
    offsets = symbol_pin_offsets(instance)
    named_numbers = symbol_pin_numbers(instance)
    electrical_types = symbol_pin_electrical_types(instance)
    pins = []
    for terminal in _terminal_names(component_ref, schematic):
        number = _physical_pin_number(terminal, offsets, named_numbers)
        pins.append(
            (
                number,
                terminal,
                electrical_types.get(terminal) or electrical_types.get(number) or "passive",
                _terminal_net_kind(component_ref, terminal, schematic),
            )
        )
    pins.sort(
        key=lambda item: (
            int(item[0]) if item[0].isdigit() else 10**9,
            _natural_key(item[0]),
        )
    )
    if len({number for number, _, _, _ in pins}) != len(pins):
        raise ToolchainError(f"active block has duplicate physical pins: {component_ref}")
    hide_pin_names = all(
        pin_name_restates_number(terminal, number) for number, terminal, _, _ in pins
    )

    top = [pin for pin in pins if pin[3] == "power"]
    bottom = [pin for pin in pins if pin[3] == "ground"]
    right = [pin for pin in pins if pin[3] == "notconnected" or pin[1] in forced_right]
    assigned = {number for group in (top, bottom, right) for number, _, _, _ in group}
    left = [
        pin for pin in pins if pin[0] not in assigned and (not forced_left or pin[1] in forced_left)
    ]
    assigned.update(number for number, _, _, _ in left)
    left.extend(pin for pin in pins if pin[0] not in assigned)

    pitch = 5.08
    half_width = 7.62
    half_height = max(7.62, (max(len(left), len(right), 1) - 1) * pitch / 2 + pitch)
    placements: list[tuple[tuple[str, str, str, str], float, float, int]] = []
    for index, pin in enumerate(left):
        placements.append(
            (pin, -(half_width + 5.08), (len(left) - 1) * pitch / 2 - index * pitch, 0)
        )
    for index, pin in enumerate(right):
        placements.append(
            (pin, half_width + 5.08, (len(right) - 1) * pitch / 2 - index * pitch, 180)
        )
    for index, pin in enumerate(top):
        placements.append((pin, (index - (len(top) - 1) / 2) * pitch, half_height + 5.08, 270))
    for index, pin in enumerate(bottom):
        placements.append((pin, (index - (len(bottom) - 1) / 2) * pitch, -(half_height + 5.08), 90))

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
    pin_blocks = []
    for (number, terminal, electrical_type, _), x, y, angle in placements:
        pin_type = electrical_type if electrical_type in valid_pin_types else "passive"
        pin_blocks.append(
            f'''      (pin {pin_type} line (at {x:.2f} {y:.2f} {angle}) (length 5.08)
        (name "{_escape(terminal)}" (effects (font (size 1.27 1.27))))
        (number "{_escape(number)}" (effects (font (size 1.27 1.27))))
      )'''
        )

    escaped_name = _escape(symbol_name)
    escaped_reference = _escape(_attribute_string(instance, "prefix") or "U")
    footprint_path = _attribute_string(instance, "footprint") or ""
    escaped_footprint = _escape(Path(footprint_path).stem if footprint_path else "")
    escaped_manufacturer = _escape(_attribute_string(instance, "manufacturer") or "")
    escaped_mpn = _escape(_attribute_string(instance, "mpn") or "")
    # Functional pin names already occupy the body interior.  Add a caption
    # only when those names are hidden because they merely restate numbers.
    caption_lines = _functional_caption_lines(instance) if hide_pin_names else ()
    caption_y = ((len(caption_lines) - 1) * 1.27 / 2) if caption_lines else 0.0
    caption_blocks = [
        f'''      (text "{_escape(line)}" (at 0 {caption_y - index * 1.27:.3f} 0)
        (effects (font (size 1.27 1.27))))'''
        for index, line in enumerate(caption_lines)
    ]
    pin_names = (
        "(pin_names (offset 1.016) (hide yes))" if hide_pin_names else "(pin_names (offset 1.016))"
    )
    return f'''(kicad_symbol_lib (version 20220914) (generator schemer)
  (symbol "{escaped_name}" {pin_names} (in_bom yes) (on_board yes)
    (property "Reference" "{escaped_reference}" (at {-half_width:.2f} {half_height + 2.54:.2f} 0)
      (effects (font (size 1.27 1.27))))
    (property "Value" "{escaped_name}" (at {-half_width:.2f} {-half_height - 2.54:.2f} 0)
      (effects (font (size 1.27 1.27))))
    (property "Footprint" "{escaped_footprint}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Manufacturer" "{escaped_manufacturer}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "MPN" "{escaped_mpn}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Manufacturer_Name" "{escaped_manufacturer}" (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Manufacturer_Part_Number" "{escaped_mpn}" (at 0 0 0)
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


def _terminal_net(
    component_ref: str,
    terminal: str,
    schematic: dict[str, Any],
) -> dict[str, Any] | None:
    nets = schematic.get("nets")
    if not isinstance(nets, dict):
        return None
    port_ref = f"{component_ref}.{terminal}"
    return next(
        (
            net
            for net in nets.values()
            if isinstance(net, dict) and port_ref in net.get("ports", ())
        ),
        None,
    )


def _component_peers(
    net: dict[str, Any],
    component_refs: set[str],
    subject_ref: str,
) -> set[str]:
    peers: set[str] = set()
    for port_ref in net.get("ports", ()):
        if not isinstance(port_ref, str):
            continue
        matches = [
            component_ref
            for component_ref in component_refs
            if port_ref.startswith(component_ref + ".")
        ]
        if matches:
            peer_ref = max(matches, key=len)
            if peer_ref != subject_ref:
                peers.add(peer_ref)
    return peers


def project_connector_like_active_blocks(
    schematic: dict[str, Any],
    plan: LayoutPlan,
    entrypoint: Path,
) -> ActiveBlockProjection:
    """Replace one-sided active symbols with functional block presentation.

    Eligibility comes from evaluated component type, rail topology and pin
    geometry. Power pins go on top, returns on the bottom, functional pins on
    the left, and explicit NC pins on the right. Shared symbol sources are
    replaced only when every user is eligible and produces the same symbol.
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
        try:
            source = _symbol_source_path(instance, workspace)
        except ToolchainError:
            continue
        all_symbol_users.setdefault(source, set()).add(instance_ref)

    candidates: dict[Path, list[tuple[str, str]]] = {}
    for module in plan.modules:
        module_prefix = module.instance_ref + "."
        module_component_refs = {
            instance_ref
            for instance_ref, instance in instances.items()
            if isinstance(instance_ref, str)
            and instance_ref.startswith(module_prefix)
            and isinstance(instance, dict)
            and instance.get("kind") == "Component"
        }
        terminal_counts = {
            component_ref: len(_terminal_names(component_ref, schematic))
            for component_ref in module_component_refs
        }
        for symbol_id in module.positions:
            if not symbol_id.startswith("comp:") or "@" in symbol_id:
                continue
            component_ref = f"{module.instance_ref}.{symbol_id.removeprefix('comp:')}"
            component = instances.get(component_ref)
            if not isinstance(component, dict) or component.get("kind") != "Component":
                continue
            component_type = (_attribute_string(component, "type") or "").casefold()
            if component_type in _NON_ACTIVE_TYPES:
                continue
            terminals = _terminal_names(component_ref, schematic)
            offsets = symbol_pin_offsets(component)
            terminal_offsets = [offsets[terminal] for terminal in terminals if terminal in offsets]
            if len(terminal_offsets) != len(terminals):
                continue
            if len(terminals) == 3:
                ground = [
                    terminal
                    for terminal in terminals
                    if _terminal_net_kind(component_ref, terminal, schematic) == "ground"
                ]
                signals = [terminal for terminal in terminals if terminal not in ground]
                owner_facing = []
                for terminal in signals:
                    net = _terminal_net(component_ref, terminal, schematic)
                    peers = (
                        _component_peers(net, module_component_refs, component_ref)
                        if net is not None
                        else set()
                    )
                    if any(terminal_counts.get(peer_ref, 0) > len(terminals) for peer_ref in peers):
                        owner_facing.append(terminal)
                if len(ground) == 1 and len(signals) == 2 and len(owner_facing) == 1:
                    source_path = _symbol_source_path(component, workspace)
                    symbol_name = _attribute_string(component, "symbol_name")
                    if symbol_name is None:
                        continue
                    control_facing = next(
                        terminal for terminal in signals if terminal not in owner_facing
                    )
                    generated = _active_body_symbol(
                        symbol_name,
                        component_ref,
                        component,
                        schematic,
                        forced_left=set(owner_facing),
                        forced_right={control_facing},
                    )
                    candidates.setdefault(source_path, []).append((component_ref, generated))
                    continue

            if not component_type or len(terminals) < 4:
                continue
            one_sided = (
                max(point[0] for point in terminal_offsets)
                - min(point[0] for point in terminal_offsets)
                <= 0.1
                or max(point[1] for point in terminal_offsets)
                - min(point[1] for point in terminal_offsets)
                <= 0.1
            )
            net_kinds = {
                _terminal_net_kind(component_ref, terminal, schematic) for terminal in terminals
            }
            if not one_sided or not {"power", "ground"} <= net_kinds:
                continue
            source_path = _symbol_source_path(component, workspace)
            symbol_name = _attribute_string(component, "symbol_name")
            if symbol_name is None:
                continue
            generated = _active_body_symbol(symbol_name, component_ref, component, schematic)
            candidates.setdefault(source_path, []).append((component_ref, generated))

    overrides: dict[Path, str] = {}
    projected_refs: list[str] = []
    for source_path, source_candidates in candidates.items():
        candidate_refs = {component_ref for component_ref, _ in source_candidates}
        generated_sources = {generated for _, generated in source_candidates}
        if all_symbol_users.get(source_path) != candidate_refs or len(generated_sources) != 1:
            continue
        overrides[source_path] = generated_sources.pop()
        projected_refs.extend(candidate_refs)
    return ActiveBlockProjection(
        file_overrides=overrides,
        projected_component_refs=tuple(sorted(projected_refs)),
    )
