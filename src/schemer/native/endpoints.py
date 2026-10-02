from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from schemer.analysis.circuits import (
    component_layout_groups,
    component_properties,
)
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.annotations import pin_stroke_envelopes
from schemer.kicad.geometry.envelopes import Envelope
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    symbol_library_pins,
)
from schemer.kicad.items import Vector2
from schemer.native.routing_model import PlacedEndpoint
from schemer.symbols.library import symbol_pin_number_groups


def component_net_endpoints(
    schematic: dict[str, Any],
    editor: FileSchematic,
) -> dict[str, list[PlacedEndpoint]]:
    root_ref = schematic["root_ref"]
    instances = schematic["instances"]
    layout_groups, _ = component_layout_groups(schematic)
    physical_refs = sorted(
        (
            ref
            for ref, instance in instances.items()
            if isinstance(ref, str)
            and isinstance(instance, dict)
            and instance.get("reference_designator")
        ),
        key=len,
        reverse=True,
    )
    by_designator = {instances[ref]["reference_designator"]: ref for ref in physical_refs}
    supplied_pins = set()
    for ref in physical_refs:
        properties = component_properties(schematic, ref)
        owner = by_designator.get(properties.get("owner"))
        if properties.get("role") == "bypass" and owner and properties.get("pin"):
            supplied_pins.update((owner, number) for number in
                symbol_pin_number_groups(instances[owner]).get(properties["pin"], ()))
    raw_by_path: dict[str, list[Any]] = defaultdict(list)
    for symbol in editor.document.symbols:
        if symbol.path is not None:
            raw_by_path[symbol.path].append(symbol)
    pin_geometry: dict[str, tuple[Vector2, str, str, Envelope]] = {}
    power_unit_passives: set[tuple[str, str]] = set()
    for component_ref in physical_refs:
        path = component_ref.removeprefix(root_ref + ".")
        instance = instances[component_ref]
        number_by_name = symbol_pin_number_groups(instance)
        for symbol in raw_by_path.get(path, []):
            positions = placed_pin_positions(editor.document, symbol)
            sides = placed_pin_sides(editor.document, symbol)
            library_pins = symbol_library_pins(editor.document, symbol)
            if _is_dedicated_power_unit(
                [pin.electrical_type for pin in library_pins.values()],
                len(raw_by_path[path]),
            ):
                power_unit_passives.update(
                    (component_ref, number) for number, pin in library_pins.items()
                    if pin.electrical_type == "passive")
            strokes = pin_stroke_envelopes(editor, symbol)
            for number, position in positions.items():
                pin_geometry[f"{component_ref}:{number}"] = (
                    position, sides[number], library_pins[number].electrical_type, strokes[number],
                )
        instance["__schemer_pin_numbers"] = number_by_name

    result: dict[str, list[PlacedEndpoint]] = defaultdict(list)
    supply_contacts: set[tuple[str, str, str]] = set()
    for net in schematic["nets"].values():
        if not isinstance(net, dict) or not isinstance(net.get("name"), str):
            continue
        for port in net.get("ports", []):
            if not isinstance(port, str):
                continue
            component_ref = next(
                (ref for ref in physical_refs if port.startswith(ref + ".")),
                None,
            )
            if component_ref is None:
                continue
            terminal = port.removeprefix(component_ref + ".").split(".", 1)[0]
            instance = instances[component_ref]
            number_by_name = instance["__schemer_pin_numbers"]
            numbers = number_by_name.get(terminal)
            if numbers is None and terminal.startswith("Pin_"):
                numbers = (terminal.removeprefix("Pin_"),)
            if numbers is None and terminal.isdigit():
                numbers = (terminal,)
            for number in numbers or (None,):
                geometry = pin_geometry.get(f"{component_ref}:{number}")
                if geometry is None:
                    raise KiCadSchematicError(
                        f"cannot resolve KiCad pin for {port.removeprefix(root_ref + '.')}"
                    )
                position, side, electrical_type, stroke = geometry
                group = layout_groups[component_ref]
                properties = component_properties(schematic, component_ref)
                bank = ((by_designator.get(properties["owner"], properties["owner"]),
                         properties["group"])
                        if properties.get("owner") and properties.get("group")
                        and properties.get("role") in {"bypass", "shunt"} else None)
                if (bank is None and not properties.get("owner") and properties.get("group")
                        and properties.get("role") in {"bypass", "shunt"}):
                    bank = (group, properties["group"])
                rail_class = None
                if net.get("kind") in {"Power", "Ground"}:
                    role = properties.get("role")
                    if (electrical_type in {"power_in", "power_out"} or role == "bypass"
                            or (component_ref, number) in supplied_pins
                            or (component_ref, number) in power_unit_passives):
                        rail_class = "supply"
                    elif electrical_type in {"input", "bidirectional"} or role in {
                        "pullup", "pulldown",
                    }:
                        rail_class = "signal-tie"
                    elif role in {"shunt", "series", "divider", "pin-bridge"}:
                        rail_class = "signal-support"
                    else:
                        # Passive analogue switch contacts are signal pins
                        # too. Absence of a power pin type must not silently
                        # let their hard ties merge into supply/bypass wiring.
                        rail_class = "signal-tie"
                endpoint = PlacedEndpoint(position, side, component_ref, group,
                                           rail_class=rail_class, stroke=stroke, bank=bank)
                if rail_class == "supply":
                    supply_contacts.add((net["name"], component_ref, number))
                if endpoint not in result[net["name"]]:
                    result[net["name"]].append(endpoint)
    for net_name, pins in result.items():
        for index, pin in enumerate(pins):
            props = component_properties(schematic, pin.owner) if pin.owner else {}
            owner = by_designator.get(props.get("owner"))
            attachment = (symbol_pin_number_groups(instances[owner]).get(props.get("pin"), ())
                          if owner else ())
            if pin.rail_class == "signal-support" and any(
                (net_name, owner, number) in supply_contacts for number in attachment
            ):
                # Sharing the owner's ground is insufficient: the authored
                # attachment must itself be a supply contact on this net.
                pins[index] = replace(pin, rail_class="supply")
    return result


def _is_dedicated_power_unit(pin_types: list[str], unit_count: int) -> bool:
    """A library-defined power-only unit may include passive supply contacts.

    Do not extend this to ordinary mixed-function devices: their passive
    switch contacts and hard-tied inputs still need distinct rail wiring.
    """
    return (unit_count > 1 and bool(set(pin_types) & {"power_in", "power_out"})
            and set(pin_types) <= {"power_in", "power_out", "passive"})
