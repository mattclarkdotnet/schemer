from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemer.analysis.roles import RoleSource, component_roles, module_functions, module_sheets
from schemer.analysis.visibility import electrical_view
from schemer.core.errors import ToolchainError


@dataclass(frozen=True)
class SheetPort:
    net: str
    kind: str
    inside_components: tuple[str, ...]
    outside_components: tuple[str, ...]


@dataclass(frozen=True)
class PlannedSheet:
    module_ref: str
    name: str
    parent_ref: str | None
    function: str | None
    component_refs: tuple[str, ...]
    child_refs: tuple[str, ...]
    ports: tuple[SheetPort, ...]


@dataclass(frozen=True)
class HierarchyPlan:
    root_ref: str
    sheets: tuple[PlannedSheet, ...]


def plan_sheets(schematic: dict[str, Any]) -> HierarchyPlan:
    """Plan one root and a flat set of named circuit sheets.

    Modules stay inline unless authored sheet membership says otherwise.
    Descendants inherit that membership; modules naming the same sheet share
    it. Owned support follows its owner, even across source modules. Source
    nesting never adds page levels and component count never creates a page.
    """

    visible = electrical_view(schematic)
    root_ref = visible["root_ref"]
    instances = visible["instances"]
    physical = {ref: item for ref, item in instances.items()
                if item.get("reference_designator")}
    by_designator = {item["reference_designator"]: ref for ref, item in physical.items()}
    if len(by_designator) != len(physical):
        raise ToolchainError("sheet planning requires unique physical reference designators")
    if any(not ref.startswith(root_ref + ".") for ref in physical):
        raise ToolchainError("physical component is outside the schematic root")

    if root_ref not in instances:
        raise ToolchainError("sheet planning requires a root module")
    declared = module_sheets(instances)
    root_name = declared.get(root_ref, "Overview")
    representatives = {root_name: root_ref}
    for ref, name in sorted(declared.items()):
        representatives.setdefault(name, ref)
    membership = {ref: representatives[name] for ref, name in declared.items()}
    membership[root_ref] = root_ref
    names = {ref: name for name, ref in representatives.items()}

    roles = component_roles(instances, tuple(
        RoleSource(ref, item) for ref, item in sorted(physical.items())
    ))
    owners = {}
    for role in roles:
        if role.owner is None:
            continue
        owner = by_designator.get(role.owner)
        if owner is None:
            raise ToolchainError(f"{role.component_ref}: missing authored owner {role.owner!r}")
        owners[role.component_ref] = owner

    module_refs = set(membership)
    assigned = {part: membership[_containing_sheet(part, module_refs)] for part in physical}

    def owner_sheet(part: str, visiting: frozenset[str] = frozenset()) -> str:
        if part in visiting:
            raise ToolchainError(f"cyclic authored ownership at {part}")
        if part not in owners:
            return assigned[part]
        return owner_sheet(owners[part], visiting | {part})

    assigned = {part: owner_sheet(part) for part in physical}
    candidates = {root_ref, *assigned.values()}
    functions = {item.instance_ref: item.function for item in module_functions(instances)}
    net_members: list[tuple[str, str, frozenset[str]]] = []
    for net in visible["nets"].values():
        if net.get("kind") == "NotConnected":
            continue
        connected = frozenset(
            part for part in physical
            if any(port.startswith(part + ".") for port in net.get("ports", ()))
        )
        if connected:
            net_members.append((net["name"], net.get("kind", "Net"), connected))

    sheets = []
    for ref in [root_ref, *sorted(candidates - {root_ref}, key=lambda ref: names[ref])]:
        local_parts = frozenset(part for part, sheet in assigned.items() if sheet == ref)
        ports = tuple(
            SheetPort(name, kind, tuple(sorted(connected & local_parts)),
                      tuple(sorted(connected - local_parts)))
            for name, kind, connected in sorted(net_members)
            if connected & local_parts and connected - local_parts
        ) if ref != root_ref else ()
        sheets.append(PlannedSheet(
            module_ref=ref,
            name=names[ref],
            parent_ref=None if ref == root_ref else root_ref,
            function=(functions.get(ref) if list(membership.values()).count(ref) == 1 else None),
            component_refs=tuple(sorted(local_parts)),
            child_refs=tuple(sorted(candidates - {root_ref})) if ref == root_ref else (),
            ports=ports,
        ))
    return HierarchyPlan(root_ref, tuple(sheets))


def _containing_sheet(ref: str, candidates: set[str]) -> str:
    ancestors = [sheet for sheet in candidates if ref.startswith(sheet + ".")]
    if not ancestors:
        raise ToolchainError(f"no containing sheet for {ref!r}")
    return max(ancestors, key=len)
