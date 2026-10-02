from __future__ import annotations

from statistics import median
from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    is_rail_net,
    is_return_net,
)
from schemer.placement.blocks.composition import block_from_positions
from schemer.placement.blocks.model import LayoutBlock
from schemer.placement.circuits.bypasses import component_envelopes, place_local_bypass
from schemer.placement.circuits.model import Component, ParallelLink, TransformerChain
from schemer.placement.circuits.net_symbols import (
    NetSymbols,
    attach_net_symbols,
    consolidated_rail_attachments,
    is_not_connected_net,
    net_symbol_attachment,
    terminal_net_symbol_attachment,
)
from schemer.placement.circuits.orientation import (
    median_point,
    orientation_for_terminal_sides,
    oriented_terminal_vector,
    translate_pin_to,
)
from schemer.placement.circuits.policy import LOCAL_RAIL_STUB, NET_SYMBOL_STUB, PIN_EXIT_STUB
from schemer.placement.circuits.queries import (
    is_boundary_net,
    net_components,
    other_terminal,
    terminal_for_net,
)
from schemer.source.hints import Endpoint, HintSet
from schemer.symbols.geometry import (
    pin_outward_side,
    pin_positions,
)
from schemer.symbols.library import symbol_pin_electrical_types
from schemer.symbols.model import Point


def parallel_transformer_chain(
    driver: Component,
    components: tuple[Component, ...],
    boundary_net_names: set[str],
    bypass: Component,
) -> TransformerChain | None:
    component_refs = tuple(component.ref for component in components)
    by_ref = {component.ref: component for component in components}
    boundary_inputs: dict[str, list[str]] = {}
    boundary_nets: dict[str, dict[str, Any]] = {}
    for terminal, (net_ref, net) in driver.terminals.items():
        if (
            not is_rail_net(net_ref, net)
            and not is_not_connected_net(net_ref, net)
            and is_boundary_net(net_ref, net, boundary_net_names)
        ):
            boundary_inputs.setdefault(net_ref, []).append(terminal)
            boundary_nets[net_ref] = net
    if len(boundary_inputs) != 1:
        return None
    input_net_ref, input_terminals = next(iter(boundary_inputs.items()))
    if len(input_terminals) < 2:
        return None

    links: list[ParallelLink] = []
    common_refs: set[str] = set()
    for owner_terminal, (net_ref, net) in driver.terminals.items():
        if (
            is_rail_net(net_ref, net)
            or is_not_connected_net(net_ref, net)
            or is_boundary_net(net_ref, net, boundary_net_names)
        ):
            continue
        peers = net_components(net, component_refs) - {driver.ref}
        if len(peers) != 1:
            return None
        passive = by_ref[next(iter(peers))]
        if passive.component_type not in NON_OWNER_TYPES or len(passive.terminals) != 2:
            return None
        passive_owner_terminal = terminal_for_net(passive, net_ref)
        if passive_owner_terminal is None:
            return None
        passive_common_terminal = other_terminal(passive, passive_owner_terminal)
        if passive_common_terminal is None:
            return None
        common_refs.add(passive.terminals[passive_common_terminal][0])
        links.append(
            ParallelLink(
                passive,
                owner_terminal,
                passive_owner_terminal,
                passive_common_terminal,
            )
        )
    if len(links) < 2 or len(common_refs) != 1:
        return None
    common_net_ref = common_refs.pop()

    shunts = []
    transformers = []
    for component in components:
        common_terminal = terminal_for_net(component, common_net_ref)
        if common_terminal is None:
            continue
        if component.component_type == "transformer":
            transformers.append((component, common_terminal))
            continue
        remote_terminal = other_terminal(component, common_terminal)
        if remote_terminal is not None and is_return_net(*component.terminals[remote_terminal]):
            shunts.append((component, common_terminal, remote_terminal))
    if len(shunts) != 1 or len(transformers) != 1:
        return None
    shunt, shunt_common_terminal, shunt_return_terminal = shunts[0]
    transformer, transformer_common_terminal = transformers[0]
    transformer_return_terminals = [
        terminal
        for terminal, (net_ref, net) in transformer.terminals.items()
        if terminal != transformer_common_terminal and is_return_net(net_ref, net)
    ]
    if len(transformer_return_terminals) != 1:
        return None
    transformer_return_terminal = transformer_return_terminals[0]

    connectors = [
        component
        for component in components
        if component.component_type == "connector" and len(component.terminals) == 2
    ]
    if len(connectors) != 1:
        return None
    connector = connectors[0]
    secondary = [
        terminal
        for terminal in transformer.terminals
        if terminal not in {transformer_common_terminal, transformer_return_terminal}
    ]
    if len(secondary) != 2:
        return None

    direct: tuple[str, str] | None = None
    coupled: tuple[str, Component, str, str, str] | None = None
    for terminal in secondary:
        net_ref, net = transformer.terminals[terminal]
        peers = net_components(net, component_refs) - {transformer.ref}
        if peers == {connector.ref}:
            connector_terminal = terminal_for_net(connector, net_ref)
            if connector_terminal is not None:
                direct = (terminal, connector_terminal)
            continue
        if len(peers) != 1:
            continue
        coupling = by_ref[next(iter(peers))]
        coupling_transformer_terminal = terminal_for_net(coupling, net_ref)
        if coupling_transformer_terminal is None:
            continue
        coupling_connector_terminal = other_terminal(coupling, coupling_transformer_terminal)
        if coupling_connector_terminal is None:
            continue
        connector_net_ref = coupling.terminals[coupling_connector_terminal][0]
        connector_terminal = terminal_for_net(connector, connector_net_ref)
        if connector_terminal is not None:
            coupled = (
                terminal,
                coupling,
                coupling_transformer_terminal,
                coupling_connector_terminal,
                connector_terminal,
            )
    if direct is None or coupled is None:
        return None
    return TransformerChain(
        driver=driver,
        input_net_ref=input_net_ref,
        input_net=boundary_nets[input_net_ref],
        input_terminals=tuple(sorted(input_terminals)),
        links=tuple(sorted(links, key=lambda link: link.passive.ref)),
        common_net_ref=common_net_ref,
        shunt=shunt,
        shunt_common_terminal=shunt_common_terminal,
        shunt_return_terminal=shunt_return_terminal,
        transformer=transformer,
        transformer_common_terminal=transformer_common_terminal,
        transformer_return_terminal=transformer_return_terminal,
        transformer_signal_terminal=coupled[0],
        transformer_connector_return_terminal=direct[0],
        coupling=coupled[1],
        coupling_transformer_terminal=coupled[2],
        coupling_connector_terminal=coupled[3],
        connector=connector,
        connector_signal_terminal=coupled[4],
        connector_return_terminal=direct[1],
        bypass=bypass,
    )


def transformer_chain_block(
    chain: TransformerChain,
    symbols: NetSymbols,
    *,
    padding: float,
    hints: HintSet,
) -> LayoutBlock:
    # Select semantic relationships before creating electrical lanes. Only
    # the recognized local secondary circuit can consume this return pairing.
    local_return = hints.take(
        "local-return",
        Endpoint(
            chain.transformer.symbol_id.removeprefix("comp:"),
            chain.transformer_connector_return_terminal,
        ),
        Endpoint(chain.connector.symbol_id.removeprefix("comp:"), chain.connector_return_terminal),
    )
    pin_exit = hints.take(
        "pin-exit",
        Endpoint(
            chain.transformer.symbol_id.removeprefix("comp:"), chain.transformer_return_terminal
        ),
    )
    # Local wires need pin-exit clearance, not an empty routing channel.
    # Keep enough room at the primary for its separate downward return.
    inline_span = 4 * PIN_EXIT_STUB
    primary_span = 8 * PIN_EXIT_STUB
    secondary_stub = LOCAL_RAIL_STUB if local_return else inline_span
    connector_stub = LOCAL_RAIL_STUB if local_return else inline_span
    electrical_types = symbol_pin_electrical_types(chain.driver.instance)
    desired = {terminal: "left" for terminal in chain.input_terminals}
    desired.update({link.owner_terminal: "right" for link in chain.links})
    desired.update(
        {
            terminal: ("bottom" if is_return_net(net_ref, net) else "top")
            for terminal, (net_ref, net) in chain.driver.terminals.items()
            if is_rail_net(net_ref, net) and electrical_types.get(terminal) == "power_in"
        }
    )
    driver_position = orientation_for_terminal_sides(chain.driver, desired)
    positions = {chain.driver.symbol_id: driver_position}

    remote_points: list[Point] = []
    for link in sorted(
        chain.links,
        key=lambda candidate: (
            median_point(
                pin_positions(
                    chain.driver.instance,
                    driver_position,
                    candidate.owner_terminal,
                )
            ).y
        ),
    ):
        owner_pin = median_point(
            pin_positions(chain.driver.instance, driver_position, link.owner_terminal)
        )
        base = oriented_terminal_vector(
            link.passive,
            link.passive_owner_terminal,
            link.passive_common_terminal,
            axis="x",
            direction=1.0,
        )
        passive_pin = median_point(
            pin_positions(link.passive.instance, base, link.passive_owner_terminal)
        )
        placed = translate_pin_to(
            base,
            passive_pin,
            Point(owner_pin.x + inline_span, owner_pin.y),
        )
        positions[link.passive.symbol_id] = placed
        remote_points.append(
            median_point(
                pin_positions(
                    link.passive.instance,
                    placed,
                    link.passive_common_terminal,
                )
            )
        )

    common_x = float(median(point.x for point in remote_points))
    # Continue from the top of the bank; the shunt leaves from its bottom.
    # A continuation opposite an interior branch creates a four-way node.
    common_y = min(point.y for point in remote_points)
    transformer_base = orientation_for_terminal_sides(
        chain.transformer,
        {
            chain.transformer_common_terminal: "left",
            chain.transformer_return_terminal: "left",
            chain.transformer_signal_terminal: "right",
            chain.transformer_connector_return_terminal: "right",
        },
    )
    transformer_common_pin = median_point(
        pin_positions(
            chain.transformer.instance,
            transformer_base,
            chain.transformer_common_terminal,
        )
    )
    transformer_position = translate_pin_to(
        transformer_base,
        transformer_common_pin,
        Point(common_x + primary_span, common_y),
    )
    positions[chain.transformer.symbol_id] = transformer_position

    shunt_base = oriented_terminal_vector(
        chain.shunt,
        chain.shunt_common_terminal,
        chain.shunt_return_terminal,
        axis="y",
        direction=1.0,
    )
    shunt_common_pin = median_point(
        pin_positions(chain.shunt.instance, shunt_base, chain.shunt_common_terminal)
    )
    shunt_position = translate_pin_to(
        shunt_base,
        shunt_common_pin,
        Point(common_x + PIN_EXIT_STUB, max(point.y for point in remote_points) + inline_span),
    )
    positions[chain.shunt.symbol_id] = shunt_position

    transformer_signal_pin = median_point(
        pin_positions(
            chain.transformer.instance,
            transformer_position,
            chain.transformer_signal_terminal,
        )
    )
    coupling_base = oriented_terminal_vector(
        chain.coupling,
        chain.coupling_transformer_terminal,
        chain.coupling_connector_terminal,
        axis="x",
        direction=1.0,
    )
    coupling_signal_pin = median_point(
        pin_positions(
            chain.coupling.instance,
            coupling_base,
            chain.coupling_transformer_terminal,
        )
    )
    coupling_position = translate_pin_to(
        coupling_base,
        coupling_signal_pin,
        Point(transformer_signal_pin.x + secondary_stub, transformer_signal_pin.y),
    )
    positions[chain.coupling.symbol_id] = coupling_position
    coupling_connector_pin = median_point(
        pin_positions(
            chain.coupling.instance,
            coupling_position,
            chain.coupling_connector_terminal,
        )
    )

    connector_base = orientation_for_terminal_sides(
        chain.connector,
        {
            chain.connector_signal_terminal: "left",
            chain.connector_return_terminal: "left",
        },
    )
    connector_signal_pin = median_point(
        pin_positions(
            chain.connector.instance,
            connector_base,
            chain.connector_signal_terminal,
        )
    )
    connector_position = translate_pin_to(
        connector_base,
        connector_signal_pin,
        Point(coupling_connector_pin.x + connector_stub, coupling_connector_pin.y),
    )
    positions[chain.connector.symbol_id] = connector_position

    input_points = tuple(
        point
        for terminal in chain.input_terminals
        for point in pin_positions(chain.driver.instance, driver_position, terminal)
    )
    attachments = [
        net_symbol_attachment(
            chain.input_net_ref,
            chain.input_net,
            input_points,
            "left",
        ),
        terminal_net_symbol_attachment(
            chain.shunt,
            shunt_position,
            chain.shunt_return_terminal,
            side="bottom",
            clearance=LOCAL_RAIL_STUB,
        ),
        terminal_net_symbol_attachment(
            chain.transformer,
            transformer_position,
            chain.transformer_return_terminal,
            side=(
                pin_outward_side(
                    chain.transformer.instance,
                    transformer_position,
                    chain.transformer_return_terminal,
                )
                if pin_exit
                else "bottom"
            ),
            clearance=LOCAL_RAIL_STUB if pin_exit else NET_SYMBOL_STUB,
        ),
    ]
    attachments.extend(consolidated_rail_attachments(chain.driver, driver_position))
    attachments.extend(
        place_local_bypass(
            chain.driver,
            driver_position,
            chain.bypass,
            positions,
        )
    )
    symbols.rail_envelopes = []
    attach_net_symbols(positions, tuple(attachments), symbols=symbols)

    members = (
        chain.driver,
        *(link.passive for link in chain.links),
        chain.shunt,
        chain.transformer,
        chain.coupling,
        chain.connector,
        chain.bypass,
    )
    return block_from_positions(
        "parallel-transformer-chain",
        positions,
        occupied=component_envelopes(members, positions),
        padding=padding,
    )
