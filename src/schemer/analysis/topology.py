from __future__ import annotations

import hashlib
import json
from typing import Any


def bind_component_pin_numbers(schematic: dict[str, Any]) -> None:
    """Expose compiled port-to-pad bindings to instance-local geometry readers.

    Terminal aliases need not match a symbol name or number. This in-memory
    index comes from compiler port objects, never from terminal spelling,
    and is not persisted as source metadata.
    """

    instances = schematic["instances"]
    for instance in instances.values():
        if instance.get("kind") != "Component":
            continue
        bindings = {}
        for terminal, port_ref in instance.get("children", {}).items():
            pads = instances.get(port_ref, {}).get("attributes", {}).get("pads", {})
            numbers = tuple(pad["String"] for pad in pads.get("Array", [])
                            if isinstance(pad, dict) and isinstance(pad.get("String"), str))
            if numbers:
                bindings[terminal] = numbers
        instance["_pin_numbers"] = bindings


def connectivity_digest(schematic: dict[str, Any]) -> str:
    """Hash semantic topology without placement or compiler-local net IDs."""

    root_ref = schematic.get("root_ref")

    def normalize_instance_ref(value: Any) -> Any:
        if not isinstance(value, str) or not isinstance(root_ref, str):
            return value
        if value == root_ref:
            return "<root>"
        if value.startswith(root_ref + "."):
            return "<root>." + value.removeprefix(root_ref + ".")
        return value

    instances: dict[str, Any] = {}
    raw_instances = schematic.get("instances", {})
    if isinstance(raw_instances, dict):
        for ref, instance in raw_instances.items():
            normalized_ref = normalize_instance_ref(ref)
            if not isinstance(instance, dict):
                instances[normalized_ref] = instance
                continue
            children = instance.get("children")
            normalized_children = (
                {name: normalize_instance_ref(child_ref) for name, child_ref in children.items()}
                if isinstance(children, dict)
                else children
            )
            type_ref = instance.get("type_ref")
            normalized_type_ref = (
                {"module_name": type_ref.get("module_name")}
                if isinstance(type_ref, dict)
                else type_ref
            )
            instances[normalized_ref] = {
                "children": normalized_children,
                "kind": instance.get("kind"),
                "reference_designator": instance.get("reference_designator"),
                "type_ref": normalized_type_ref,
            }

    nets: dict[str, Any] = {}
    raw_nets = schematic.get("nets", {})
    if isinstance(raw_nets, dict):
        for ref, net in raw_nets.items():
            if not isinstance(net, dict):
                nets[ref] = net
                continue
            ports = net.get("ports", [])
            nets[ref] = {
                "kind": net.get("kind"),
                "name": net.get("name"),
                "ports": (
                    sorted(normalize_instance_ref(port) for port in ports)
                    if isinstance(ports, list)
                    else ports
                ),
            }

    normalized = {
        "root_ref": normalize_instance_ref(root_ref),
        "instances": instances,
        "nets": nets,
    }

    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def inspect_schematic(schematic: dict[str, Any]) -> dict[str, Any]:
    """Return stable structural facts suitable for a toolchain smoke report."""

    instances = schematic["instances"]
    root_ref = schematic["root_ref"]
    root = instances.get(root_ref, {}) if isinstance(instances, dict) else {}
    children = root.get("children", {}) if isinstance(root, dict) else {}
    positions = root.get("symbol_positions", {}) if isinstance(root, dict) else {}
    physical_components = sum(
        1
        for instance in instances.values()
        if isinstance(instance, dict) and instance.get("reference_designator")
    )
    return {
        "root_ref": root_ref,
        "instance_count": len(instances),
        "physical_component_count": physical_components,
        "net_count": len(schematic["nets"]),
        "root_children": sorted(children),
        "root_position_count": len(positions),
        "connectivity_digest": connectivity_digest(schematic),
    }
