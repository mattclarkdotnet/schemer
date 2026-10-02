from __future__ import annotations

from schemer.analysis.connections import NON_OWNER_TYPES, is_rail_net, is_return_net
from schemer.analysis.roles import component_roles
from schemer.core.errors import ToolchainError
from schemer.placement.circuits.model import Component, PassiveChain


def passive_chains(
    members: tuple[Component, ...] | list[Component],
) -> tuple[PassiveChain, ...]:
    """Find unbranched resistor paths without relying on names or references.

    A net joins two path edges when it connects exactly two resistors and at
    most one other local component. Rails are boundaries, never chain joins.
    The latter condition keeps a shared return bus from absorbing unrelated
    shunts while still allowing an IC sense pin to tap a divider midpoint.
    """

    resistors = {
        component.ref: component
        for component in members
        if component.component_type == "resistor"
        and len(component.terminals) == 2
        and len({net_ref for net_ref, _ in component.terminals.values()}) == 2
    }
    if len(resistors) < 2:
        return ()
    refs_by_net: dict[str, set[str]] = {}
    nets_by_ref: dict[str, dict] = {}
    for component in members:
        for net_ref, net in component.terminals.values():
            refs_by_net.setdefault(net_ref, set()).add(component.ref)
            nets_by_ref[net_ref] = net
    resistor_ends: dict[str, list[str]] = {}
    for component in resistors.values():
        for net_ref, _ in component.terminals.values():
            resistor_ends.setdefault(net_ref, []).append(component.ref)

    adjacency: dict[str, list[tuple[str, str]]] = {ref: [] for ref in resistors}
    for net_ref, endpoint_refs in resistor_ends.items():
        if len(endpoint_refs) != 2 or is_rail_net(net_ref, nets_by_ref[net_ref]):
            continue
        external = refs_by_net[net_ref] - set(endpoint_refs)
        if len(external) > 1:
            continue
        first, second = endpoint_refs
        adjacency[first].append((second, net_ref))
        adjacency[second].append((first, net_ref))

    result: list[PassiveChain] = []
    unseen = set(resistors)
    while unseen:
        seed = min(unseen)
        stack = [seed]
        connected: set[str] = set()
        while stack:
            ref = stack.pop()
            if ref in connected:
                continue
            connected.add(ref)
            stack.extend(other for other, _ in adjacency[ref])
        unseen -= connected
        if len(connected) < 2:
            continue
        if any(len(adjacency[ref]) > 2 for ref in connected):
            continue
        edge_count = sum(len(adjacency[ref]) for ref in connected) // 2
        endpoints = sorted(ref for ref in connected if len(adjacency[ref]) == 1)
        if edge_count != len(connected) - 1 or len(endpoints) != 2:
            continue

        ordered_refs: list[str] = []
        previous: str | None = None
        current = endpoints[0]
        while True:
            ordered_refs.append(current)
            following = sorted(other for other, _ in adjacency[current] if other != previous)
            if not following:
                break
            previous, current = current, following[0]
        shared_nets = []
        for first, second in zip(ordered_refs, ordered_refs[1:]):
            shared = {net for other, net in adjacency[first] if other == second}
            if len(shared) != 1:
                break
            shared_nets.append(shared.pop())
        else:
            def terminal_for(component: Component, net_ref: str) -> str:
                return next(
                    terminal for terminal, (candidate, _) in component.terminals.items()
                    if candidate == net_ref
                )

            first_component = resistors[ordered_refs[0]]
            last_component = resistors[ordered_refs[-1]]
            first_net = next(
                net_ref for net_ref, _ in first_component.terminals.values()
                if net_ref != shared_nets[0]
            )
            last_net = next(
                net_ref for net_ref, _ in last_component.terminals.values()
                if net_ref != shared_nets[-1]
            )
            net_order = [first_net, *shared_nets, last_net]

            def orientation_score(nets: list[str]) -> tuple[int, int]:
                first_ref, last_ref = nets[0], nets[-1]
                first, last = nets_by_ref[first_ref], nets_by_ref[last_ref]
                first_supply = (
                    is_rail_net(first_ref, first) and not is_return_net(first_ref, first)
                )
                last_return = is_return_net(last_ref, last)
                first_return = is_return_net(first_ref, first)
                last_supply = is_rail_net(last_ref, last) and not is_return_net(last_ref, last)
                return (int(first_supply) + int(last_return),
                        -int(first_return) - int(last_supply))

            if orientation_score(list(reversed(net_order))) > orientation_score(net_order):
                ordered_refs.reverse()
                net_order.reverse()
            entries = tuple(
                (
                    resistors[ref],
                    terminal_for(resistors[ref], net_order[index]),
                    terminal_for(resistors[ref], net_order[index + 1]),
                )
                for index, ref in enumerate(ordered_refs)
            )
            result.append(PassiveChain(entries, tuple(net_order)))
    return tuple(sorted(result, key=lambda chain: tuple(entry[0].ref for entry in chain.entries)))


def chain_owner(chain: PassiveChain, members) -> Component | None:
    chain_refs = {entry[0].ref for entry in chain.entries}
    candidates = []
    for component in members:
        if component.ref in chain_refs or component.component_type in NON_OWNER_TYPES:
            continue
        shared = {net_ref for net_ref, _ in component.terminals.values()} & set(chain.net_refs)
        candidates.append((len(shared), len(component.terminals), component.ref, component))
    if not candidates:
        return None
    count, _, _, owner = max(candidates, key=lambda item: (item[0], item[1], item[2]))
    return owner if count >= 2 else None


def authored_divider_chains(instances, members):
    """Resolve declared divider order and owner without part-specific knowledge."""

    roles = tuple(
        role for role in component_roles(instances, tuple(members))
        if role.kind == "divider"
    )
    if not roles:
        return ()
    by_ref = {component.ref: component for component in members}
    grouped = {}
    for role in roles:
        grouped.setdefault((role.group, role.owner), []).append(role)
    result = []
    for (group, owner_name), group_roles in sorted(grouped.items()):
        ordered = sorted(group_roles, key=lambda role: role.order or 0)
        if [role.order for role in ordered] != list(range(len(ordered))):
            raise ToolchainError(
                f"divider group {group!r} order must be unique and contiguous from zero"
            )
        components = [by_ref[role.component_ref] for role in ordered]
        if len(components) < 2 or any(
            component.component_type != "resistor" or len(component.terminals) != 2
            for component in components
        ):
            raise ToolchainError(
                f"divider group {group!r} requires at least two two-terminal resistors"
            )
        shared_nets = []
        for first, second in zip(components, components[1:]):
            shared = (
                {net_ref for net_ref, _ in first.terminals.values()}
                & {net_ref for net_ref, _ in second.terminals.values()}
            )
            if len(shared) != 1:
                raise ToolchainError(
                    f"divider group {group!r} has discontinuous neighbours "
                    f"{first.ref} and {second.ref}"
                )
            shared_nets.append(shared.pop())

        first_net = next(
            net_ref for net_ref, _ in components[0].terminals.values()
            if net_ref != shared_nets[0]
        )
        last_net = next(
            net_ref for net_ref, _ in components[-1].terminals.values()
            if net_ref != shared_nets[-1]
        )
        net_order = (first_net, *shared_nets, last_net)
        entries = tuple(
            (
                component,
                next(terminal for terminal, (net_ref, _) in component.terminals.items()
                     if net_ref == net_order[index]),
                next(terminal for terminal, (net_ref, _) in component.terminals.items()
                     if net_ref == net_order[index + 1]),
            )
            for index, component in enumerate(components)
        )
        owner = next(
            (
                component for component in members
                if str(component.instance.get("reference_designator", "")) == owner_name
            ),
            None,
        )
        if owner is None:
            raise ToolchainError(
                f"divider group {group!r} owner {owner_name!r} is not a local component"
            )
        result.append((PassiveChain(entries, net_order), owner))
    return tuple(result)
