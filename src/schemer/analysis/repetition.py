from __future__ import annotations

from typing import Any

from schemer.analysis.inventory import StructuralInventory, structural_inventory


def repeated_owner_blocks(
    schematic: dict[str, Any], groups: dict[str, str],
    inventory: StructuralInventory | None = None,
) -> tuple[tuple[dict[str, str], ...], ...]:
    """Select exact local assemblies without changing electrical group membership."""
    inventory = inventory if inventory is not None else structural_inventory(schematic)
    result = []
    for family in inventory.families:
        if family.kind != "owner-block":
            continue
        members = []
        for member in family.members:
            anchor = member["anchor"]
            group = groups.get(anchor)
            if group is not None and all(groups.get(ref) == group for ref in member.values()):
                members.append(member)
        if len(members) > 1:
            result.append(tuple(members))
    return tuple(result)
