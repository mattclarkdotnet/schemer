from __future__ import annotations

from typing import Any

from schemer.analysis.connections import (
    NON_OWNER_TYPES,
    is_rail_net,
    is_return_net,
    module_boundary_net_names,
)
from schemer.core.layout import ModuleLayout
from schemer.placement.blocks.composition import block_from_positions, compose_column, compose_row
from schemer.placement.blocks.model import LayoutBlock
from schemer.placement.blocks.plan import BlockPlan
from schemer.placement.builders.transformer import (
    parallel_transformer_chain,
    transformer_chain_block,
)
from schemer.placement.circuits.bypasses import (
    assign_local_bypasses,
    component_envelopes,
    local_bypasses,
    place_local_bypass,
)
from schemer.placement.circuits.model import BoundaryChannel, Component
from schemer.placement.circuits.net_symbols import (
    NetSymbols,
    attach_net_symbols,
    consolidated_rail_attachments,
    is_not_connected_net,
    terminal_net_symbol_attachment,
)
from schemer.placement.circuits.orientation import (
    median_point,
    orientation_for_terminal_sides,
    oriented_terminal_vector,
    translate_pin_to,
)
from schemer.placement.circuits.queries import (
    collect_components,
    is_boundary_net,
    net_components,
    other_terminal,
    terminal_for_net,
)
from schemer.source.hints import HintSet
from schemer.symbols.geometry import pin_positions
from schemer.symbols.model import Point


def _boundary_channel(
    active: Component,
    components: tuple[Component, ...],
    boundary_net_names: set[str],
    bypass: Component,
) -> BoundaryChannel | None:
    component_refs = tuple(component.ref for component in components)
    signals = [
        (terminal, net_ref, net)
        for terminal, (net_ref, net) in active.terminals.items()
        if not is_rail_net(net_ref, net) and not is_not_connected_net(net_ref, net)
    ]
    if len(signals) != 1:
        return None
    active_terminal, signal_ref, signal_net = signals[0]
    peers = net_components(signal_net, component_refs) - {active.ref}
    if len(peers) != 1:
        return None
    series = next((component for component in components if component.ref in peers), None)
    if (
        series is None
        or series.component_type not in NON_OWNER_TYPES
        or len(series.terminals) != 2
    ):
        return None
    series_active_terminal = terminal_for_net(series, signal_ref)
    if series_active_terminal is None:
        return None
    series_boundary_terminal = other_terminal(series, series_active_terminal)
    if series_boundary_terminal is None:
        return None
    boundary_ref, boundary_net = series.terminals[series_boundary_terminal]
    if not is_boundary_net(boundary_ref, boundary_net, boundary_net_names):
        return None
    return BoundaryChannel(
        active,
        series,
        active_terminal,
        series_active_terminal,
        series_boundary_terminal,
        boundary_ref,
        boundary_net,
        bypass,
    )


def _boundary_channel_block(
    channel: BoundaryChannel,
    symbols: NetSymbols,
    *,
    block_id: str,
    padding: float,
) -> LayoutBlock:
    rail_sides = {
        terminal: ("bottom" if is_return_net(net_ref, net) else "top")
        for terminal, (net_ref, net) in channel.active.terminals.items()
        if is_rail_net(net_ref, net)
    }
    active_position = orientation_for_terminal_sides(
        channel.active,
        {channel.active_terminal: "left", **rail_sides},
    )
    positions = {channel.active.symbol_id: active_position}
    active_pin = median_point(
        pin_positions(channel.active.instance, active_position, channel.active_terminal)
    )
    base = oriented_terminal_vector(
        channel.series,
        channel.series_active_terminal,
        channel.series_boundary_terminal,
        axis="x",
        direction=-1.0,
    )
    series_pin = median_point(
        pin_positions(channel.series.instance, base, channel.series_active_terminal)
    )
    series_position = translate_pin_to(
        base,
        series_pin,
        Point(active_pin.x - 120.0, active_pin.y),
    )
    positions[channel.series.symbol_id] = series_position
    attachments = list(consolidated_rail_attachments(channel.active, active_position))
    attachments.extend(
        place_local_bypass(
            channel.active,
            active_position,
            channel.bypass,
            positions,
        )
    )
    attachments.append(
        terminal_net_symbol_attachment(
            channel.series,
            series_position,
            channel.series_boundary_terminal,
            side="left",
        )
    )
    symbols.rail_envelopes = []
    attach_net_symbols(positions, tuple(attachments), symbols=symbols)
    members = (channel.active, channel.series, channel.bypass)
    return block_from_positions(
        block_id,
        positions,
        occupied=component_envelopes(members, positions),
        padding=padding,
    )


def multi_active_interface_block_from_zero(
    schematic: dict[str, Any],
    module: ModuleLayout,
    *,
    padding: float = 40.0,
    hints: HintSet | None = None,
) -> BlockPlan | None:
    """Compose repeated endpoint channels with a parallel transformer chain.

    This intentionally small grammar is recognized solely from evaluated
    topology: several one-signal active endpoints, a unique higher-pin-count
    active with parallel series outputs, a common shunt, a transformer, one
    coupling passive, and a two-pin connector. Existing coordinates never
    participate, and the transform aborts unless every component is owned.
    """

    instances, _, components = collect_components(schematic, module)
    active = tuple(
        component
        for component in components
        if component.component_type not in NON_OWNER_TYPES
        and component.component_type != "connector"
    )
    if len(active) < 3:
        return None
    largest_count = max(len(component.terminals) for component in active)
    drivers = tuple(component for component in active if len(component.terminals) == largest_count)
    if len(drivers) != 1 or largest_count < 8:
        return None
    driver = drivers[0]
    endpoint_active = tuple(component for component in active if component != driver)
    bypasses = local_bypasses(components)
    bypass_by_active = assign_local_bypasses(active, bypasses)
    if bypass_by_active is None:
        return None
    boundary_net_names = module_boundary_net_names(instances[module.instance_ref])
    channels = tuple(
        _boundary_channel(
            component,
            components,
            boundary_net_names,
            bypass_by_active[component.ref],
        )
        for component in endpoint_active
    )
    if any(channel is None for channel in channels):
        return None
    resolved_channels = tuple(channel for channel in channels if channel is not None)
    chain = parallel_transformer_chain(
        driver,
        components,
        boundary_net_names,
        bypass_by_active[driver.ref],
    )
    if chain is None:
        return None

    classified = {
        driver.ref,
        chain.shunt.ref,
        chain.transformer.ref,
        chain.coupling.ref,
        chain.connector.ref,
        chain.bypass.ref,
        *(link.passive.ref for link in chain.links),
    }
    for channel in resolved_channels:
        classified.update({channel.active.ref, channel.series.ref, channel.bypass.ref})
    if classified != {component.ref for component in components}:
        return None

    symbols = NetSymbols()
    channel_blocks = tuple(
        _boundary_channel_block(
            channel,
            symbols,
            block_id=f"endpoint-channel-{index}",
            padding=padding,
        )
        for index, channel in enumerate(
            sorted(resolved_channels, key=lambda candidate: candidate.active.ref)
        )
    )
    endpoint_bank = compose_column(
        "endpoint-channel-bank",
        channel_blocks,
        gap=60.0,
        alignment="start",
    )
    transformer_block = transformer_chain_block(
        chain, symbols, padding=padding, hints=hints if hints is not None else HintSet()
    )
    root = compose_row(
        "multi-active-interface",
        (endpoint_bank, transformer_block),
        gap=180.0,
        padding=padding,
        alignment="center",
    )
    result = BlockPlan(root)
    result.validate()
    return result
