from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from schemer.analysis.connectivity import expected_pin_nets
from schemer.analysis.roles import schematic_properties
from schemer.core.attributes import attribute_string


@dataclass(frozen=True)
class RepeatedStructure:
    id: str
    kind: str
    members: tuple[dict[str, str], ...]


@dataclass(frozen=True)
class StructuralInventory:
    families: tuple[RepeatedStructure, ...]
    references: dict[str, str]
    unmatched: tuple[dict[str, Any], ...]


def structural_inventory(schematic: dict[str, Any]) -> StructuralInventory:
    """Match authored owner blocks/groups; separately list same-symbol cohorts.

    Coordinates, numbering, net spelling and values do not establish matches.
    Ambiguous slots are reported, not paired by ordering. This bounded inventory
    is not exhaustive subgraph mining or permission to choose a representation.
    """
    instances = schematic["instances"]
    physical = {ref: item for ref, item in sorted(instances.items())
                if item.get("reference_designator")}
    references = {ref: item["reference_designator"] for ref, item in physical.items()}
    by_designator = {name: ref for ref, name in references.items()}
    props = {ref: next((p for source in (ref, ref.rsplit(".", 1)[0])
                       if (p := schematic_properties(instances.get(source, {})))
                       and "role" in p), {}) for ref in physical}
    shapes = {ref: attribute_string(item, "__symbol_value") for ref, item in physical.items()}
    local_pins: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for (reference, pin), net in expected_pin_nets(schematic).items():
        if reference in by_designator:
            local_pins[net].append((by_designator[reference], pin))
    kinds = {net["name"]: net.get("kind", "Net") for net in schematic["nets"].values()}
    unmatched = []
    families = []

    def role_key(ref: str) -> str:
        p = props[ref]
        return json.dumps([sha256((shapes[ref] or "").encode()).hexdigest(),
                           *[p.get(key) for key in
                             ("role", "pin", "other_pin", "return_pin", "order")]])

    def signature(slots: dict[str, str]) -> tuple:
        nets, at = [], []
        for net, pins in local_pins.items():
            local = tuple(sorted((slots[ref], pin) for ref, pin in pins if ref in slots))
            if local:
                nets.append((kinds.get(net, "NotConnected"),
                             any(ref not in slots for ref, _ in pins), local))
                for ref in slots:
                    name = props[ref].get("at")
                    if (name and (net == name or net.endswith("." + name))
                            and any(r == ref for r, _ in pins)):
                        at.append((slots[ref], local))
        partitions: dict[str, list[str]] = defaultdict(list)
        for ref in slots:
            if group := props[ref].get("group"):
                partitions[group].append(slots[ref])
        return (tuple(sorted((slot, shapes[ref], role_key(ref)) for ref, slot in slots.items())),
                tuple(sorted(nets)), tuple(sorted(at)),
                tuple(sorted(tuple(sorted(items)) for items in partitions.values())))

    def collect(kind: str, candidates: list[dict[str, str]]) -> None:
        matches: dict[tuple, list[dict[str, str]]] = defaultdict(list)
        for slots in candidates:
            reason = ("ambiguous correspondence" if len(set(slots.values())) != len(slots)
                      else "missing source symbol" if any(not shapes[r] for r in slots)
                      else None)
            for ref in slots:
                name = props[ref].get("at")
                if name and len([net for net, pins in local_pins.items()
                                 if (net == name or net.endswith("." + name))
                                 and any(r == ref for r, _ in pins)]) != 1:
                    reason = "unresolved authored net attachment"
            if reason:
                unmatched.append({"kind": kind, "components": sorted(slots), "reason": reason})
                continue
            matches[signature(slots)].append({slot: ref for ref, slot in slots.items()})
        for members in matches.values():
            if len(members) > 1:
                families.append(RepeatedStructure(f"repeat-{len(families) + 1:03d}", kind,
                                                   tuple(members)))

    owners: dict[str, list[str]] = defaultdict(list)

    def root(ref: str, visiting: frozenset[str] = frozenset()) -> str | None:
        if ref in visiting:
            return None
        if name := props[ref].get("owner"):
            owner = by_designator.get(name)
            return root(owner, visiting | {ref}) if owner else None
        return ref

    for ref in physical:
        anchor = root(ref)
        if anchor is None:
            unmatched.append({"kind": "owner-block", "components": [ref],
                              "reason": "unresolved or cyclic authored ownership"})
        else:
            owners[anchor].append(ref)
    candidates = []
    for anchor, refs in owners.items():
        if len(refs) < 2:
            continue
        slots = {anchor: "anchor"}

        def slot(ref: str) -> str:
            if ref not in slots:
                slots[ref] = json.dumps([slot(by_designator[props[ref]["owner"]]), role_key(ref)])
            return slots[ref]

        candidates.append({ref: slot(ref) for ref in refs})
    collect("owner-block", candidates)

    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for ref, p in props.items():
        if p.get("group") and not p.get("owner"):
            scope = ref.rsplit(".", 1)[0]
            if not schematic_properties(instances[ref]):
                scope = scope.rsplit(".", 1)[0]
            groups[scope, p["group"]].append(ref)
    collect("authored-group", [{ref: role_key(ref) for ref in refs}
                               for refs in groups.values() if len(refs) > 1])

    symbols: dict[str, list[str]] = defaultdict(list)
    for ref, shape in shapes.items():
        if shape:
            symbols[shape].append(ref)
    for refs in symbols.values():
        if len(refs) > 1:
            families.append(RepeatedStructure(f"repeat-{len(families) + 1:03d}", "same-symbol",
                                               tuple({"component": ref} for ref in refs)))
    return StructuralInventory(tuple(families), references, tuple(unmatched))


def inventory_markdown(inventory: StructuralInventory) -> str:
    """Produce a judge worklist, not an acceptance verdict."""
    lines = ["# Structural review inventory", "",
             "Exact matches ignore values and net names; compare actual values and external",
             "connections before judging presentation. Same-symbol cohorts are NOT circuit",
             "equivalences. This inventory is not exhaustive and does not authorize a new",
             "representation. Inspect the whole sheet as well as these correspondences.", ""]
    for family in inventory.families:
        lines.extend([f"## {family.id}: {family.kind}", ""])
        for member in family.members:
            lines.append("- " + ", ".join(inventory.references[ref] for ref in member.values()))
        if family.kind != "same-symbol":
            lines.extend(["", "Corresponding slots (same order across instances):", ""])
            for slot in family.members[0]:
                lines.append("- " + " / ".join(inventory.references[m[slot]]
                                                 for m in family.members))
        lines.append("")
    if inventory.unmatched:
        lines.extend(["## Unresolved correspondence", ""])
        for item in inventory.unmatched:
            lines.append("- " + ", ".join(inventory.references[r] for r in item["components"])
                         + ": " + item["reason"])
    return "\n".join(lines) + "\n"
