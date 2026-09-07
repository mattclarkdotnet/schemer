"""Presentation-only filtering before a schematic is sent to the viewer."""

from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from typing import Any

from schemer.symbol_geometry import _NON_OWNER_TYPES, _balanced_blocks, _is_rail_net
from schemer.toolchain import ToolchainError

_NET_SYMBOL_ID = re.compile(r"^sym:(.+)#(\d+)$")
_GENERIC_PIN_NAME_BASES = {"p", "pad", "pin"}
_GENERIC_PIN_NAME = re.compile(r"(?:pin|pad|p)?[_-]?[a-z]?\d+", re.IGNORECASE)
_NON_SIGNAL_PIN_NAMES = {"", "~", "nc", "n/c"}


def _string_attribute(attributes: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = attributes.get(name)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("String"), str):
            return value["String"]
    return None


def _set_string_attribute(attributes: dict[str, Any], name: str, value: str) -> None:
    current = attributes.get(name)
    if isinstance(current, dict) and "String" in current:
        attributes[name] = {**current, "String": value}
    else:
        attributes[name] = value


def _shortest_unique_net_names(names: list[str]) -> dict[str, str]:
    """Return the shortest dot-delimited suffix that identifies each net."""

    segments = {name: name.split(".") for name in names}
    result: dict[str, str] = {}
    for name, parts in segments.items():
        for length in range(1, len(parts) + 1):
            candidate = ".".join(parts[-length:])
            matches = sum(
                ".".join(other_parts[-length:]) == candidate
                for other_parts in segments.values()
                if len(other_parts) >= length
            )
            if matches == 1:
                result[name] = candidate
                break
        else:  # pragma: no cover - dictionary net names are unique by construction
            result[name] = name
    return result


def _datasheet_pin_name_map(source: str) -> dict[str, str]:
    """Map raw symbol pin names to their painted functional names."""

    pins: list[tuple[str, str | None]] = []
    for block in _balanced_blocks(source, "pin"):
        name_match = re.search(r'\(name\s+"([^"\\]*)"', block)
        number_match = re.search(r'\(number\s+"([^"\\]*)"', block)
        if name_match is None or number_match is None:
            continue
        name = name_match.group(1)
        number = number_match.group(1)
        suffix = f"_{number}"
        base = name[: -len(suffix)] if name.endswith(suffix) else None
        pins.append((name, base))

    repeated = Counter(base for _, base in pins if base)
    return {
        name: (
            base
            if base is not None
            and base.casefold() not in _GENERIC_PIN_NAME_BASES
            and repeated[base] >= 2
            else name
        )
        for name, base in pins
    }


def _datasheet_pin_names(source: str) -> str:
    """Remove repeated physical-number suffixes from painted pin names."""

    display_names = _datasheet_pin_name_map(source)
    for block in _balanced_blocks(source, "pin"):
        name_match = re.search(r'\(name\s+"([^"\\]*)"', block)
        if name_match is None:
            continue
        name = name_match.group(1)
        display_name = display_names.get(name, name)
        if display_name == name:
            continue
        changed = block[: name_match.start(1)] + display_name + block[name_match.end(1) :]
        source = source.replace(block, changed, 1)
    return source


def _is_informative_pin_name(name: str) -> bool:
    """Return whether a pin name expresses function rather than package position."""

    compact = name.strip()
    return (
        len(compact) >= 2
        and compact.casefold() not in _NON_SIGNAL_PIN_NAMES
        and _GENERIC_PIN_NAME.fullmatch(compact) is None
    )


def _functional_pin_names_by_port(instances: dict[str, Any]) -> dict[str, str]:
    """Index active-device ports by their presentation pin name."""

    result: dict[str, str] = {}
    for instance_ref, instance in instances.items():
        if not isinstance(instance_ref, str) or not isinstance(instance, dict):
            continue
        if instance.get("kind") != "Component":
            continue
        attributes = instance.get("attributes")
        children = instance.get("children")
        if not isinstance(attributes, dict) or not isinstance(children, dict):
            continue
        component_type = _string_attribute(attributes, "type") or ""
        if component_type.casefold() in _NON_OWNER_TYPES:
            continue
        symbol = _string_attribute(attributes, "__symbol_value")
        if symbol is None:
            continue
        pin_names = _datasheet_pin_name_map(symbol)
        for raw_name in children:
            if not isinstance(raw_name, str):
                continue
            display_name = pin_names.get(raw_name)
            if display_name is not None and _is_informative_pin_name(display_name):
                result[f"{instance_ref}.{raw_name}"] = display_name
    return result


def _pin_aware_net_names(
    nets: dict[str, Any],
    instances: dict[str, Any],
    fallback_names: dict[str, str],
) -> dict[str, str]:
    """Prefer a sole functional endpoint name without making captions ambiguous."""

    names_by_port = _functional_pin_names_by_port(instances)
    candidates: dict[str, str] = {}
    for net in nets.values():
        if not isinstance(net, dict) or not isinstance(net.get("name"), str):
            continue
        if _is_rail_net(net["name"], net):
            continue
        ports = net.get("ports")
        if not isinstance(ports, list):
            continue
        pin_names = {
            names_by_port[port] for port in ports if isinstance(port, str) and port in names_by_port
        }
        if len(pin_names) == 1:
            candidates[net["name"]] = next(iter(pin_names))

    candidate_counts = Counter(candidates.values())
    fallback_owners = {caption: name for name, caption in fallback_names.items()}
    result = fallback_names.copy()
    for name, candidate in candidates.items():
        fallback_owner = fallback_owners.get(candidate)
        if candidate_counts[candidate] == 1 and fallback_owner in (None, name):
            result[name] = candidate
    return result


def clean_schematic_labels(schematic: dict[str, Any]) -> dict[str, Any]:
    """Remove hierarchy and implementation metadata from a viewer presentation copy.

    Electrical identity remains in the compiler netlist. Net captions use a
    sole unambiguous functional endpoint name when available, then fall back to
    the shortest unambiguous local suffix. Component descriptions remain
    metadata rather than visible annotations. Component package names are also
    hidden: they belong in the BOM and board data, not on the schematic. A
    value consisting of an MPN followed by descriptive prose is reduced to the
    MPN.
    """

    cleaned = deepcopy(schematic)
    instances = cleaned.get("instances")
    nets = cleaned.get("nets")
    if isinstance(nets, dict):
        net_names = [
            net.get("name")
            for net in nets.values()
            if isinstance(net, dict) and isinstance(net.get("name"), str)
        ]
        display_names = _shortest_unique_net_names(net_names)
        if isinstance(instances, dict):
            display_names = _pin_aware_net_names(nets, instances, display_names)
        compact_nets: dict[str, Any] = {}
        for key, net in nets.items():
            if not isinstance(net, dict) or not isinstance(net.get("name"), str):
                compact_nets[key] = net
                continue
            original_name = net["name"]
            display_name = display_names[original_name]
            net["name"] = display_name
            compact_nets[display_name] = net
        cleaned["nets"] = compact_nets
    else:
        display_names = {}

    if not isinstance(instances, dict):
        return cleaned

    for instance in instances.values():
        if not isinstance(instance, dict):
            continue
        attributes = instance.get("attributes")
        if isinstance(attributes, dict):
            symbol = _string_attribute(attributes, "__symbol_value")
            if symbol is not None:
                _set_string_attribute(
                    attributes,
                    "__symbol_value",
                    _datasheet_pin_names(symbol),
                )
            description = _string_attribute(attributes, "description", "Description")
            value_key = next(
                (name for name in ("value", "Value") if name in attributes),
                None,
            )
            value = _string_attribute(attributes, "value", "Value")
            mpn = _string_attribute(attributes, "mpn", "MPN")
            if value_key is not None and value is not None and mpn is not None:
                prefix, separator, suffix = value.partition(" ")
                if separator and prefix.casefold() == mpn.casefold() and suffix.strip():
                    _set_string_attribute(attributes, value_key, mpn)
            if description is not None:
                attributes.pop("description", None)
                attributes.pop("Description", None)
            attributes.pop("package", None)
            attributes.pop("Package", None)

        positions = instance.get("symbol_positions")
        if not isinstance(positions, dict) or not display_names:
            continue
        remapped_positions: dict[str, Any] = {}
        for symbol_id, position in positions.items():
            match = _NET_SYMBOL_ID.fullmatch(symbol_id) if isinstance(symbol_id, str) else None
            if match is None:
                remapped_positions[symbol_id] = position
                continue
            net_name, index = match.groups()
            display_name = display_names.get(net_name, net_name)
            remapped_positions[f"sym:{display_name}#{index}"] = position
        instance["symbol_positions"] = remapped_positions

    return cleaned


def focus_module(schematic: dict[str, Any], instance_ref: str) -> dict[str, Any]:
    """Make one existing module subtree the root of a presentation copy."""

    focused = deepcopy(schematic)
    instances = focused.get("instances")
    if not isinstance(instances, dict) or not isinstance(instances.get(instance_ref), dict):
        raise ToolchainError(f"focus module is absent from schematic: {instance_ref}")

    def belongs_to_focus(value: str) -> bool:
        return value == instance_ref or value.startswith(instance_ref + ".")

    focused["root_ref"] = instance_ref
    focused["instances"] = {
        ref: instance
        for ref, instance in instances.items()
        if isinstance(ref, str) and belongs_to_focus(ref)
    }

    nets = focused.get("nets")
    if isinstance(nets, dict):
        for net in nets.values():
            if not isinstance(net, dict) or not isinstance(net.get("ports"), list):
                continue
            net["ports"] = [
                port for port in net["ports"] if isinstance(port, str) and belongs_to_focus(port)
            ]
    return focused


def hide_root_children(
    schematic: dict[str, Any],
    child_names: set[str],
    *,
    root_symbol_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Remove selected root-owned subtrees from a presentation copy.

    This does not alter the evaluated design or persisted source. It is for
    symbols such as mounting holes and service points that are valid board
    objects but do not help explain the electrical circuit.
    """

    filtered = deepcopy(schematic)
    root_ref = filtered.get("root_ref")
    instances = filtered.get("instances")
    if not isinstance(root_ref, str) or not isinstance(instances, dict):
        raise ToolchainError("schematic root or instances are invalid")
    root = instances.get(root_ref)
    if not isinstance(root, dict) or not isinstance(root.get("children"), dict):
        raise ToolchainError("schematic root children are invalid")

    root_children = root["children"]
    removed_refs = {
        child_ref
        for name, child_ref in root_children.items()
        if name in child_names and isinstance(child_ref, str)
    }
    root["children"] = {
        name: child_ref for name, child_ref in root_children.items() if name not in child_names
    }

    def is_removed_ref(value: str) -> bool:
        return any(value == removed or value.startswith(removed + ".") for removed in removed_refs)

    filtered["instances"] = {
        instance_ref: instance
        for instance_ref, instance in instances.items()
        if not isinstance(instance_ref, str) or not is_removed_ref(instance_ref)
    }

    nets = filtered.get("nets")
    if isinstance(nets, dict):
        for net in nets.values():
            if not isinstance(net, dict) or not isinstance(net.get("ports"), list):
                continue
            net["ports"] = [
                port
                for port in net["ports"]
                if not isinstance(port, str) or not is_removed_ref(port)
            ]

    hidden_ids = root_symbol_ids or set()
    positions = root.get("symbol_positions")
    if isinstance(positions, dict):
        root["symbol_positions"] = {
            symbol_id: position
            for symbol_id, position in positions.items()
            if symbol_id not in hidden_ids
            and not any(symbol_id.startswith(f"comp:{child_name}.") for child_name in child_names)
        }
    return filtered


_NON_EXPLANATORY_COMPONENT_TYPES = {"mechanical", "mounting_hole", "test_point"}


def _boolean_attribute(attributes: dict[str, Any], name: str) -> bool:
    value = attributes.get(name)
    if isinstance(value, bool):
        return value
    return isinstance(value, dict) and value.get("Boolean") is True


def _is_non_explanatory_component(instance: dict[str, Any]) -> bool:
    """Classify service-only parts from semantic component metadata."""

    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return False
    component_type = (_string_attribute(attributes, "type") or "").casefold()
    return component_type in _NON_EXPLANATORY_COMPONENT_TYPES or (
        _boolean_attribute(attributes, "skip_bom") and _boolean_attribute(attributes, "skip_pos")
    )


def electrical_view(schematic: dict[str, Any]) -> dict[str, Any]:
    """Suppress direct-child groups containing only service or mechanical parts.

    Selection is derived from evaluated component type.  Instance names,
    reference designators, source paths, and board identity do not participate.
    Existing root net-symbol copies are removed with their semantically owned
    service group when an ownership assignment is available.
    """

    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    if not isinstance(root_ref, str) or not isinstance(instances, dict):
        raise ToolchainError("schematic root or instances are invalid")
    root = instances.get(root_ref)
    children = root.get("children") if isinstance(root, dict) else None
    if not isinstance(children, dict):
        raise ToolchainError("schematic root children are invalid")

    hidden_children: set[str] = set()
    for child_name, child_ref in children.items():
        if not isinstance(child_name, str) or not isinstance(child_ref, str):
            continue
        descendants = [
            instance
            for instance_ref, instance in instances.items()
            if isinstance(instance_ref, str)
            and (instance_ref == child_ref or instance_ref.startswith(child_ref + "."))
            and isinstance(instance, dict)
            and instance.get("kind") == "Component"
        ]
        if descendants and all(_is_non_explanatory_component(instance) for instance in descendants):
            hidden_children.add(child_name)

    if not hidden_children:
        return deepcopy(schematic)

    hidden_symbols: set[str] = set()
    positions = root.get("symbol_positions") if isinstance(root, dict) else None
    if isinstance(positions, dict) and positions:
        # Imported lazily to keep the presentation helpers independent of the
        # layout-metrics module during normal label cleanup.
        from schemer.layout_metrics import top_level_root_symbol_groups

        assignments = top_level_root_symbol_groups(schematic)
        hidden_symbols = {
            symbol_id
            for symbol_id, group_name in assignments.items()
            if symbol_id.startswith("sym:") and group_name in hidden_children
        }
    return hide_root_children(
        schematic,
        hidden_children,
        root_symbol_ids=hidden_symbols,
    )
