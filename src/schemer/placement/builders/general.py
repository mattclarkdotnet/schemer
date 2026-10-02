from __future__ import annotations

from math import ceil, sqrt

from schemer.analysis.connections import NON_OWNER_TYPES, is_rail_net, is_return_net
from schemer.analysis.roles import component_roles
from schemer.analysis.visibility import is_non_explanatory_component
from schemer.core.errors import ToolchainError
from schemer.core.layout import ModuleLayout, Position
from schemer.placement.blocks.composition import block_from_positions, compose_column, compose_row
from schemer.placement.blocks.model import Rect
from schemer.placement.blocks.plan import BlockPlan
from schemer.placement.builders.authored import role_directed_series_block
from schemer.placement.circuits.bias import (
    place_authored_pulldown_banks,
    place_authored_pullup_banks,
)
from schemer.placement.circuits.bypasses import component_envelopes
from schemer.placement.circuits.chain_placement import place_tapped_passive_chains
from schemer.placement.circuits.chains import authored_divider_chains
from schemer.placement.circuits.dividers import place_authored_dividers
from schemer.placement.circuits.net_symbols import (
    NetSymbols,
    anchor_net_symbol_attachments,
    attach_net_symbols,
    clear_rail_signal_lanes,
    is_not_connected_net,
    net_symbol_attachment,
    net_symbol_drawing_envelope,
    rail_drawing_envelope,
    single_ended_net_symbol_attachments,
)
from schemer.placement.circuits.orientation import (
    median_point,
    oriented_terminal_vector,
    translate_pin_to,
)
from schemer.placement.circuits.queries import (
    collect_components,
    component_drawing_envelope,
    drawings_overlap,
)
from schemer.placement.circuits.support import (
    place_authored_bypasses,
    place_authored_inline_parts,
    place_authored_power_feeds,
    place_single_rail_shunts,
)
from schemer.symbols.geometry import pin_outward_side, pin_positions
from schemer.symbols.model import Point


def _parallel_capacitor_banks(members, positions):
    """Lay out same-owner parallel capacitors as one two-rail drawing."""
    candidates = {}
    for component in members:
        if component.component_type != "capacitor" or len(component.terminals) != 2:
            continue
        supply = [t for t, (ref, net) in component.terminals.items()
                  if is_rail_net(ref, net) and not is_return_net(ref, net)]
        returns = [t for t, (ref, net) in component.terminals.items() if is_return_net(ref, net)]
        if len(supply) == len(returns) == 1:
            key = (component.terminals[supply[0]][0], component.terminals[returns[0]][0])
            candidates.setdefault(key, []).append((component, supply[0], returns[0]))
    attachments = []
    bank_refs = set()
    for bank in candidates.values():
        if len(bank) < 2:
            continue
        refs = {component.ref for component, _, _ in bank}
        other_drawings = [
            component_drawing_envelope(c, positions[c.symbol_id])
            for c in members
            if c.ref not in refs
        ]
        first, supply, _ = bank[0]
        supply_y = median_point(pin_positions(first.instance, positions[first.symbol_id], supply)).y
        cursor = max((e.max_x for e in other_drawings), default=0) + 160
        # The bank is placed as a whole after its owner's other attachments.
        # Each capacitor points from the upper supply bus to the lower return bus.
        for component, supply, returned in bank:
            base = oriented_terminal_vector(component, supply, returned, axis="y", direction=1)
            source_pin = median_point(pin_positions(component.instance, base, supply))
            drawing = component_drawing_envelope(component, base)
            placed = translate_pin_to(base, source_pin, Point(cursor + source_pin.x - drawing.min_x,
                                                        supply_y))
            positions[component.symbol_id] = placed
            cursor = component_drawing_envelope(component, placed).max_x + 80
        for terminal_index, side in ((1, "top"), (2, "bottom")):
            component = bank[0][0]
            net_ref, net = component.terminals[bank[0][terminal_index]]
            points = tuple(median_point(pin_positions(c.instance, positions[c.symbol_id], item))
                           for c, item in ((entry[0], entry[terminal_index]) for entry in bank))
            attachments.append(net_symbol_attachment(net_ref, net, points, side))
        bank_refs.update(refs)
    return bank_refs, attachments


def general_local_blocks(
    schematic: dict, module: ModuleLayout, *, padding: float = 40,
    excluded_modules: tuple[str, ...] = (),
    child_blocks: dict[str, BlockPlan] | None = None,
) -> BlockPlan | None:
    """Assign every electrical component once, then measure and pack local blocks.

    Anchors are ICs, connectors and non-two-terminal devices. Passives attach
    along signal nets first, supply nets second; a shared return alone does
    not establish ownership. Passive-only circuits start their own chain.
    Child modules with separate layouts are never swallowed by their parent.
    """
    instances, _, all_components = collect_components(schematic, module)
    components = tuple(c for c in all_components
                       if not is_non_explanatory_component(
                           c.instance, instances.get(c.ref.rsplit(".", 1)[0]))
                       and not any(c.ref.startswith(ref + ".") for ref in excluded_modules))
    child_blocks = child_blocks or {}
    if not components and not child_blocks:
        return None
    if components and not child_blocks:
        role_block = role_directed_series_block(instances, components, padding=padding)
        if role_block is not None:
            return role_block
    anchors = sorted(
        (c for c in components if c.component_type not in NON_OWNER_TYPES
         or c.component_type == "connector" or len(c.terminals) != 2),
        key=lambda c: (-len(c.terminals), c.ref),
    )
    groups = [[anchor] for anchor in anchors]
    owned = {anchor.ref: (index, 0) for index, anchor in enumerate(anchors)}
    by_designator = {c.instance["reference_designator"]: c.ref for c in components}
    authored_owners = {}
    for role in component_roles(instances, components):
        if role.owner is not None:
            if role.owner not in by_designator:
                raise ToolchainError(f"{role.component_ref}: missing authored owner {role.owner}")
            authored_owners[role.component_ref] = by_designator[role.owner]

    def root_owner(ref: str, seen: frozenset[str] = frozenset()) -> str:
        if ref in seen:
            raise ToolchainError(f"cyclic authored ownership at {ref}")
        if ref not in authored_owners:
            return ref
        return root_owner(authored_owners[ref], seen | {ref})

    pending = {c.ref: c for c in components if c.ref not in owned}
    links = {}
    while pending:
        candidates = []
        for child in pending.values():
            for parent in components:
                if parent.ref not in owned:
                    continue
                group, depth = owned[parent.ref]
                if child.ref in authored_owners:
                    owner_ref = root_owner(child.ref)
                    if owner_ref not in owned or owned[owner_ref][0] != group:
                        continue
                for child_terminal, (net_ref, net) in child.terminals.items():
                    if is_return_net(net_ref, net) or is_not_connected_net(net_ref, net):
                        continue
                    for parent_terminal, (other_ref, _) in parent.terminals.items():
                        if other_ref == net_ref:
                            candidates.append((int(is_rail_net(net_ref, net)), depth, group,
                                               child.ref, parent.ref,
                                               child_terminal, parent_terminal))
        if not candidates:
            independent = [c for c in pending.values() if c.ref not in authored_owners]
            if not independent:
                raise ToolchainError(
                    "cannot attach authored support to its owner: " + ", ".join(sorted(pending))
                )
            anchor = min(independent, key=lambda c: c.ref)
            owned[anchor.ref] = (len(groups), 0)
            groups.append([anchor])
            del pending[anchor.ref]
            continue
        _, depth, group, child_ref, parent_ref, child_terminal, parent_terminal = min(candidates)
        child = pending.pop(child_ref)
        groups[group].append(child)
        owned[child_ref] = (group, depth + 1)
        links[child_ref] = (parent_ref, child_terminal, parent_terminal)

    symbols = NetSymbols(qualified=True)
    blocks = []
    by_ref = {c.ref: c for c in components}
    for index, members in enumerate(groups):
        anchor = members[0]
        positions = {anchor.symbol_id: Position(0, 0)}
        envelopes = [component_drawing_envelope(anchor, positions[anchor.symbol_id])]
        for child in members[1:]:
            parent_ref, attached, parent_terminal = links[child.ref]
            parent = by_ref[parent_ref]
            parent_position = positions[parent.symbol_id]
            origin = median_point(pin_positions(parent.instance, parent_position, parent_terminal))
            side = pin_outward_side(parent.instance, parent_position, parent_terminal)
            dx, dy = {"left": (-1, 0), "right": (1, 0),
                      "top": (0, -1), "bottom": (0, 1)}[side]
            remote = next(t for t in child.terminals if t != attached)
            base = oriented_terminal_vector(child, attached, remote,
                                            axis="x" if dx else "y", direction=dx or dy)
            point = median_point(pin_positions(child.instance, base, attached))
            # Preserve the chosen pin lane. Increase outward clearance only
            # when the completed component drawing would collide with a peer.
            for step in range(1000):
                distance = 160 + step * 80
                position = translate_pin_to(base, point,
                                       Point(origin.x + dx * distance, origin.y + dy * distance))
                envelope = component_drawing_envelope(child, position)
                if not any(drawings_overlap(envelope, other) for other in envelopes):
                    break
            else:
                raise ToolchainError(f"cannot clear local attachment lane for {child.ref}")
            positions[child.symbol_id] = position
            envelopes.append(envelope)

        bank_refs, attachments = _parallel_capacitor_banks(members, positions)
        power_feed_refs, power_feed_skips = place_authored_power_feeds(
            instances, members, positions,
        )
        inline_refs, inline_skips = place_authored_inline_parts(
            instances, members, positions,
        )
        bypass_refs = place_authored_bypasses(instances, members, positions)
        divider_refs = place_authored_dividers(instances, members, positions)
        place_tapped_passive_chains(
            [component for component in members
             if component.ref not in divider_refs | power_feed_refs | inline_refs],
            positions,
        )
        place_single_rail_shunts(
            members,
            positions,
            skipped=bank_refs | bypass_refs | power_feed_refs | inline_refs,
        )
        pullup_refs, pullup_attachments = place_authored_pullup_banks(
            instances, members, positions,
        )
        pulldown_refs, pulldown_attachments = place_authored_pulldown_banks(
            instances, members, positions,
        )
        attachments.extend(pullup_attachments)
        attachments.extend(pulldown_attachments)
        divider_owner_skips: dict[str, set[str]] = {}
        for chain, owner in authored_divider_chains(instances, members):
            endpoint_nets = {chain.net_refs[0], chain.net_refs[-1]}
            divider_owner_skips.setdefault(owner.ref, set()).update(
                terminal
                for terminal, (net_ref, net) in owner.terminals.items()
                if net_ref in endpoint_nets and is_rail_net(net_ref, net)
            )
        attachment_skips = {
            component_ref: set(terminals)
            for component_ref, terminals in power_feed_skips.items()
        }
        for component_ref, terminals in inline_skips.items():
            attachment_skips.setdefault(component_ref, set()).update(terminals)
        for owner_ref, terminals in divider_owner_skips.items():
            attachment_skips.setdefault(owner_ref, set()).update(terminals)
        envelopes = [component_drawing_envelope(c, positions[c.symbol_id]) for c in members]
        member_refs = tuple(c.ref for c in members)
        for component in members:
            position = positions[component.symbol_id]
            if component.ref not in bank_refs | pullup_refs | pulldown_refs:
                attachments.extend(anchor_net_symbol_attachments(
                    component,
                    position,
                    skipped_terminals=attachment_skips.get(component.ref),
                ))
            attachments.extend(single_ended_net_symbol_attachments(
                component, position, member_refs,
            ))
        signal_lanes = [
            (attachment.outward_side, net_symbol_drawing_envelope(attachment))
            for attachment in attachments
            if attachment.outward_side in {"left", "right"}
            and not is_rail_net(attachment.net_ref, attachment.net)
        ]
        signal_endpoints = [
            (attachment.outward_side, attachment.target)
            for attachment in attachments
            if attachment.outward_side in {"left", "right"}
            and not is_rail_net(attachment.net_ref, attachment.net)
        ]
        attachments = clear_rail_signal_lanes(
            attachments, signal_lanes, signal_endpoints,
        )
        attach_net_symbols(positions, tuple(attachments), symbols=symbols)
        envelopes.extend(rail_drawing_envelope(a.net, a.position()) for a in attachments)
        # Include actual displaced symbol drawings too, not just planned endpoints.
        envelopes.extend(symbols.rail_envelopes)
        symbols.rail_envelopes.clear()
        min_x = min(e.min_x for e in envelopes)
        min_y = min(e.min_y for e in envelopes)
        max_x = max(e.max_x for e in envelopes)
        max_y = max(e.max_y for e in envelopes)
        blocks.append(block_from_positions(
            f"local-{index}", positions,
            occupied=component_envelopes(tuple(members), positions),
            content_bounds=Rect(min_x, min_y, max_x - min_x, max_y - min_y), padding=padding,
        ))
    for ref, child in child_blocks.items():
        key = "comp:" + ref.removeprefix(module.instance_ref + ".")
        blocks.append(block_from_positions(
            f"module-{len(blocks)}", {key: Position(0, 0)},
            content_bounds=Rect(0, 0, child.root.width, child.root.height), padding=0,
        ))
    columns = ceil(sqrt(len(blocks)))
    rows = tuple(compose_row(f"row-{i}", tuple(blocks[i:i + columns]), gap=180)
                 for i in range(0, len(blocks), columns))
    result = BlockPlan(compose_column("general-local-blocks", rows, gap=180))
    result.validate()
    return result
