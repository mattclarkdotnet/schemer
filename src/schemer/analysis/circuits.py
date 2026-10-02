from __future__ import annotations

from collections import defaultdict
from typing import Any

from schemer.analysis.connectivity import expected_pin_nets
from schemer.analysis.roles import (
    RoleSource,
    component_roles,
    module_representations,
    schematic_properties,
)
from schemer.analysis.visibility import boolean_attribute
from schemer.core.errors import KiCadSchematicError
from schemer.symbols.library import symbol_pin_electrical_types, symbol_pin_number_groups


def component_properties(schematic: dict[str, Any], ref: str) -> dict[str, Any]:
    return next((props for source in (ref, ref.rsplit(".", 1)[0])
                 if (props := schematic_properties(schematic["instances"].get(source, {})))
                 and "role" in props), {})


def power_flow_edges(schematic: dict[str, Any]) -> set[tuple[str, str]]:
    """Read source-to-consumer edges from authoritative electrical pin types."""

    physical = {ref: item for ref, item in schematic["instances"].items()
                if item.get("reference_designator")}
    types = {ref: symbol_pin_electrical_types(item) for ref, item in physical.items()}
    result = set()
    for net in schematic.get("nets", {}).values():
        if net.get("kind") != "Power":
            continue
        pins = [(ref, port[len(ref) + 1:]) for port in net.get("ports", [])
                for ref in physical if port.startswith(ref + ".")]
        outputs = {ref for ref, pin in pins if types[ref].get(pin) == "power_out"}
        inputs = {ref for ref, pin in pins if types[ref].get(pin) == "power_in"}
        for source in outputs:
            for sink in inputs - {source}:
                # Only compose stages within their existing module boundary.
                prefix = schematic["root_ref"] + "."
                if (source.removeprefix(prefix).split(".")[0]
                        == sink.removeprefix(prefix).split(".")[0]):
                    result.add((source, sink))
    return result


def power_stage_order(
    schematic: dict[str, Any], groups: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    edges = power_flow_edges(schematic)
    stages = {ref for edge in edges for ref in edge}
    ordered = []
    pending = set(stages)
    while pending:
        ready = sorted(ref for ref in pending
                       if not any(sink == ref and source in pending for source, sink in edges))
        if not ready:
            raise KiCadSchematicError("power stage dependencies form a cycle")
        ordered.extend(ready)
        pending.difference_update(ready)
    result: dict[str, list[str]] = defaultdict(list)
    for ref in ordered:
        result[groups[ref].split(".", 1)[0]].append(groups[ref])
    return {parent: tuple(dict.fromkeys(children)) for parent, children in result.items()}


def validate_owned_networks(schematic: dict[str, Any]) -> None:
    """Check experimental local-network intent against exact terminal nets."""

    instances = schematic["instances"]
    physical = {item["reference_designator"]: (ref, item) for ref, item in instances.items()
                if item.get("reference_designator")}
    component_roles(instances, tuple(RoleSource(ref, item) for ref, item in physical.values()))
    nets = expected_pin_nets(schematic)
    for designator, (ref, _) in physical.items():
        props = component_properties(schematic, ref)
        if props.get("role") not in {"series", "shunt", "pin-bridge"} or not props.get("owner"):
            continue
        owner = props["owner"]
        if owner not in physical:
            raise KiCadSchematicError(f"{designator}: unknown authored owner {owner}")
        aliases = symbol_pin_number_groups(physical[owner][1])

        def owner_net(key: str) -> str:
            numbers = aliases.get(props.get(key), ())
            values = {nets[(owner, n)] for n in numbers if (owner, n) in nets}
            if len(values) != 1:
                raise KiCadSchematicError(f"{designator}: invalid owner {key} {props.get(key)!r}")
            return values.pop()

        terminals = {net for (part, _), net in nets.items() if part == designator}
        if len(terminals) != 2:
            raise KiCadSchematicError(f"{designator}: owned network member needs two terminal nets")
        if props.get("pin"):
            attached = owner_net("pin")
        else:
            matches = {net for net in terminals if net == props.get("at")
                       or net.rsplit(".", 1)[-1] == props.get("at")}
            if len(matches) != 1:
                raise KiCadSchematicError(
                    f"{designator}: invalid owned attachment {props.get('at')!r}"
                )
            attached = matches.pop()
        if attached not in terminals:
            raise KiCadSchematicError(f"{designator}: authored attachment is not connected")
        for key in ("return_pin", "other_pin"):
            if props.get(key) and terminals != {attached, owner_net(key)}:
                raise KiCadSchematicError(f"{designator}: {key} does not match the other terminal")


def direct_circuit_groups(schematic: dict[str, Any]) -> frozenset[str]:
    """Explicit representations wire *within* their chosen circuit boundaries.

    Independent blocks change the boundaries, not the internal connectivity
    of each authored owner/support assembly. Seed-label heuristics must not
    repartition either accepted representation.
    """
    representations = module_representations(schematic["instances"])
    groups, connectors = component_layout_groups(schematic)
    selected = {f"{module}::connected-circuit" for module, choice in representations.items()
                if choice == "connected-circuit"}
    selected.update(group for ref, group in groups.items()
                    if any(ref.startswith(module + ".")
                           for module, choice in representations.items()
                           if choice == "independent-blocks"))
    return frozenset(selected - connectors)


def is_external_interface(schematic: dict[str, Any], ref: str) -> bool:
    item = schematic["instances"][ref]
    attributes = item.get("attributes", {})
    kind = attributes.get("type", {}).get("String")
    if kind in {"connector", "optical_receiver", "optical_transmitter"}:
        return True
    if component_properties(schematic, ref).get("owner"):
        return False
    if kind == "test_point":
        return True
    # Some compiler packages omit type on bare plated access pads. Their
    # electrical/assembly contract is sufficient; no refdes or symbol-name test.
    numbers = {n for pins in symbol_pin_number_groups(item).values() for n in pins}
    return (len(numbers) == 1 and set(symbol_pin_electrical_types(item).values()) == {"passive"}
            and boolean_attribute(attributes, "skip_bom")
            and boolean_attribute(attributes, "skip_pos"))


def component_layout_groups(schematic: dict[str, Any]) -> tuple[dict[str, str], frozenset[str]]:
    """Keep authored local circuits wired internally and labelled externally.

    Ownership establishes a circuit boundary, not pin count or repetition.
    A supporting transistor (and anything it owns) follows its circuit owner.
    Unassigned parts retain their module grouping; connectivity alone does not
    supply missing semantic membership.
    """

    instances = schematic.get("instances", {})
    physical = {ref: item for ref, item in instances.items()
                if isinstance(item, dict) and item.get("reference_designator")}
    paths = {ref: ref.removeprefix(schematic["root_ref"] + ".") for ref in physical}
    groups = {ref: path.split(".", 1)[0] if "." in path else ""
              for ref, path in paths.items()}
    representations = module_representations(instances)
    independent = {module for module, choice in representations.items()
                   if choice == "independent-blocks"}
    for ref in physical:
        if any(ref.startswith(module + ".") for module in independent):
            groups[ref] = paths[ref]
        props = component_properties(schematic, ref)
        if props.get("group") and not props.get("owner"):
            # Group names are local to the declaring circuit, not global tags.
            # A component wrapper carries the role on behalf of its one part.
            source = (ref if "role" in (schematic_properties(physical[ref]) or {})
                      else ref.rsplit(".", 1)[0])
            scope = source.rsplit(".", 1)[0].removeprefix(schematic["root_ref"] + ".")
            groups[ref] = f"{scope}::group:{props['group']}"
    connectors = {ref for ref in physical if is_external_interface(schematic, ref)}
    by_designator = {item["reference_designator"]: ref for ref, item in physical.items()}
    top_members: dict[str, list[str]] = defaultdict(list)
    for ref, group in groups.items():
        top_members[group].append(ref)
    for ref in connectors:
        # A one-connector module already is a local block; preserve its name
        # for authored inter-block relationships.
        if len(top_members[groups[ref]]) > 1:
            groups[ref] = paths[ref]
    stages = {ref for edge in power_flow_edges(schematic) for ref in edge}
    for ref in stages:
        groups[ref] = paths[ref]
    def root_owner(ref: str, visiting: frozenset[str] = frozenset()) -> str:
        if ref in visiting:
            raise KiCadSchematicError(f"cyclic authored ownership at {ref}")
        owner_name = component_properties(schematic, ref).get("owner")
        if not owner_name:
            return ref
        owner = by_designator.get(owner_name)
        if owner is None:
            raise KiCadSchematicError(f"{ref}: unknown authored owner {owner_name}")
        return root_owner(owner, visiting | {ref})

    roots = {ref: root_owner(ref) for ref in physical}
    assemblies = {owner for ref, owner in roots.items() if ref != owner}
    for owner in assemblies:
        # Preserve the module's identity when it already contains just this
        # circuit. Otherwise give the authored assembly its own wire boundary.
        if (not component_properties(schematic, owner).get("group")
                and any(roots[ref] != owner for ref in top_members[groups[owner]])):
            groups[owner] = paths[owner]
    groups = {ref: groups[roots[ref]] for ref in physical}
    # An accepted multi-device circuit retains its feedback and signal wires.
    # This choice changes boundaries, never electrical ownership or positions.
    # Connector assemblies still retain their independent interface stubs.
    for ref in physical:
        choices = [module for module in representations
                   if roots[ref].startswith(module + ".")]
        if choices and roots[ref] not in connectors:
            module = max(choices, key=len)
            if representations[module] == "connected-circuit":
                groups[ref] = f"{module}::connected-circuit"
    return groups, frozenset(groups[ref] for ref in connectors)
