from __future__ import annotations

from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    is_rail_net,
    is_return_net,
    module_boundary_net_names,
)
from schemer.analysis.roles import component_roles
from schemer.core.errors import ToolchainError
from schemer.core.layout import ModuleLayout
from schemer.placement.builders.primary.model import PrimaryCircuit
from schemer.placement.circuits.model import Component
from schemer.placement.circuits.queries import (
    collect_components,
    is_boundary_net,
    net_components,
)


def analyze_primary_circuit(
    schematic: dict[str, Any], module: ModuleLayout
) -> PrimaryCircuit | None:
    instances, _, components = collect_components(schematic, module)
    if any(component.component_type == "connector" for component in components):
        return None
    active = tuple(
        component
        for component in components
        if component.component_type not in NON_OWNER_TYPES
        and component.component_type != "connector"
    )
    if not active:
        return None
    largest_terminal_count = max(len(component.terminals) for component in active)
    largest = tuple(
        component for component in active if len(component.terminals) == largest_terminal_count
    )
    if len(largest) != 1 or largest_terminal_count < 8:
        return None
    primary = largest[0]
    subordinate = tuple(component for component in active if component != primary)
    component_refs = tuple(component.ref for component in components)
    boundary_net_names = module_boundary_net_names(instances[module.instance_ref])
    primary_nets = {net_ref for net_ref, _ in primary.terminals.values()}
    authored_roles = component_roles(instances, components)
    pullup_roles = {
        role.component_ref: role for role in authored_roles if role.kind == "pullup"
    }
    pulldown_roles = {
        role.component_ref: role for role in authored_roles if role.kind == "pulldown"
    }
    power_feed_roles = {
        role.component_ref: role
        for role in authored_roles
        if role.kind == "power-feed"
    }

    subordinate_links: dict[str, tuple[str, str, str]] = {}
    for component in subordinate:
        shared = [
            (net_ref, primary_terminal, subordinate_terminal)
            for subordinate_terminal, (net_ref, net) in component.terminals.items()
            if not is_rail_net(net_ref, net)
            for primary_terminal, (candidate_ref, _) in primary.terminals.items()
            if candidate_ref == net_ref
        ]
        if len(shared) != 1:
            return None
        subordinate_links[component.ref] = shared[0]

    series: list[tuple[Component, str, str, str]] = []
    shunts: list[tuple[Component, str, str, str, str]] = []
    rail_feeds: list[tuple[Component, str, str, str]] = []
    for component in components:
        if component in active:
            continue
        if len(component.terminals) != 2:
            return None
        terminals = tuple(component.terminals)
        rail_terminals = [
            terminal
            for terminal, (net_ref, net) in component.terminals.items()
            if is_rail_net(net_ref, net)
        ]
        if not rail_terminals:
            primary_ends = [
                terminal
                for terminal, (net_ref, _) in component.terminals.items()
                if net_ref in primary_nets
            ]
            if len(primary_ends) != 1:
                return None
            attached_terminal = primary_ends[0]
            remote_terminal = next(
                terminal for terminal in terminals if terminal != attached_terminal
            )
            remote_net_ref, remote_net = component.terminals[remote_terminal]
            if not is_boundary_net(remote_net_ref, remote_net, boundary_net_names):
                return None
            series.append(
                (
                    component,
                    next(
                        primary_terminal
                        for primary_terminal, (net_ref, _) in primary.terminals.items()
                        if net_ref == component.terminals[attached_terminal][0]
                    ),
                    attached_terminal,
                    remote_terminal,
                )
            )
        elif len(rail_terminals) == 1:
            rail_terminal = rail_terminals[0]
            signal_terminal = next(terminal for terminal in terminals if terminal != rail_terminal)
            signal_net_ref, signal_net = component.terminals[signal_terminal]
            owners = net_components(signal_net, component_refs) & {
                candidate.ref for candidate in active
            }
            if not owners:
                return None
            shunts.append(
                (
                    component,
                    signal_terminal,
                    rail_terminal,
                    signal_net_ref,
                    component.terminals[rail_terminal][0],
                )
            )
        elif len(rail_terminals) == 2:
            primary_ends = [
                terminal
                for terminal in rail_terminals
                if component.terminals[terminal][0] in primary_nets
            ]
            if len(primary_ends) != 1:
                return None
            attached_terminal = primary_ends[0]
            remote_terminal = next(
                terminal for terminal in terminals if terminal != attached_terminal
            )
            remote_net_ref, remote_net = component.terminals[remote_terminal]
            if not is_boundary_net(remote_net_ref, remote_net, boundary_net_names):
                return None
            rail_feeds.append(
                (
                    component,
                    next(
                        primary_terminal
                        for primary_terminal, (net_ref, _) in primary.terminals.items()
                        if net_ref == component.terminals[attached_terminal][0]
                    ),
                    attached_terminal,
                    remote_terminal,
                )
            )
        else:
            return None

    shunts_by_ref = {entry[0].ref: entry for entry in shunts}
    active_by_designator = {
        str(component.instance.get("reference_designator", "")): component
        for component in active
    }
    authored_owner_refs: dict[str, str] = {}
    for role in authored_roles:
        if role.owner is None:
            continue
        owner = active_by_designator.get(role.owner)
        if owner is None:
            raise ToolchainError(
                f"{role.component_ref}: authored owner {role.owner!r} is not a local active device"
            )
        authored_owner_refs[role.component_ref] = owner.ref
    unowned_shunts = sorted(
        branch.ref
        for branch, _, _, _, _ in shunts
        if branch.ref not in authored_owner_refs
    )
    if unowned_shunts:
        raise ToolchainError(
            "primary IC layout requires authored ownership roles for rail branches: "
            + ", ".join(unowned_shunts)
        )
    primary_bias_roles = {
        **pullup_roles,
        **{
            component_ref: role
            for component_ref, role in pulldown_roles.items()
            if authored_owner_refs.get(component_ref) == primary.ref
        },
    }
    for component_ref, role in primary_bias_roles.items():
        entry = shunts_by_ref.get(component_ref)
        owner = active_by_designator.get(role.owner or "")
        if entry is None or owner != primary or role.pin not in primary.terminals:
            raise ToolchainError(
                f"{component_ref}: {role.kind} owner and pin must name the primary IC terminal"
            )
        component, signal_terminal, rail_terminal, signal_net_ref, _ = entry
        owner_net_ref = primary.terminals[role.pin][0]
        rail_net_ref, rail_net = component.terminals[rail_terminal]
        if signal_net_ref != owner_net_ref:
            raise ToolchainError(
                f"{component_ref}: {role.kind} signal does not connect to "
                f"{role.owner}.{role.pin}"
            )
        if role.kind == "pullup" and is_return_net(rail_net_ref, rail_net):
            raise ToolchainError(f"{component_ref}: pullup rail cannot be a return net")
        if role.kind == "pulldown" and not is_return_net(rail_net_ref, rail_net):
            raise ToolchainError(f"{component_ref}: pulldown rail must be a return net")

    rail_feeds_by_ref = {entry[0].ref: entry for entry in rail_feeds}
    for component_ref, role in power_feed_roles.items():
        entry = rail_feeds_by_ref.get(component_ref)
        owner = active_by_designator.get(role.owner or "")
        if entry is None or owner != primary or role.pin not in primary.terminals:
            raise ToolchainError(
                f"{component_ref}: power-feed owner and pin must name the primary IC terminal"
            )
        component, primary_terminal, attached_terminal, remote_terminal = entry
        if role.pin != primary_terminal:
            raise ToolchainError(
                f"{component_ref}: power-feed output does not connect to "
                f"{role.owner}.{role.pin}"
            )
        remote_ref, remote_net = component.terminals[remote_terminal]
        if not is_rail_net(remote_ref, remote_net) or is_return_net(remote_ref, remote_net):
            raise ToolchainError(f"{component_ref}: power-feed input must be a supply rail")

    return PrimaryCircuit(
        components=components,
        primary=primary,
        subordinate=subordinate,
        component_refs=component_refs,
        boundary_net_names=boundary_net_names,
        subordinate_links=subordinate_links,
        series=series,
        shunts=shunts,
        rail_feeds=rail_feeds,
        authored_owner_refs=authored_owner_refs,
        primary_bias_roles=primary_bias_roles,
        shunts_by_ref=shunts_by_ref,
        power_feed_roles=power_feed_roles,
    )
