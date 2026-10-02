from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemer.core.layout import Position
from schemer.symbols.model import Point
from schemer.symbols.net_symbols import position_net_symbol_pin
from schemer.symbols.signal_termination import SYMBOL as SIGNAL_TERMINATION_SYMBOL


@dataclass(frozen=True)
class Component:
    ref: str
    symbol_id: str
    instance: dict[str, Any]
    component_type: str
    terminals: dict[str, tuple[str, dict[str, Any]]]


@dataclass(frozen=True)
class SeriesLink:
    passive: Component
    connector: Component
    central_terminal: str
    connector_net: str
    connector_terminal: str
    passive_central_terminal: str
    passive_connector_terminal: str


@dataclass(frozen=True)
class SeriesWire:
    """One electrical lane derived from a real IC pin before symbols are placed."""

    link: SeriesLink
    side: str
    owner_pin: Point
    passive_pin_target: Point


@dataclass(frozen=True)
class BoundaryChannel:
    """One active endpoint, its inline passive, and its public signal net."""

    active: Component
    series: Component
    active_terminal: str
    series_active_terminal: str
    series_boundary_terminal: str
    boundary_net_ref: str
    boundary_net: dict[str, Any]
    bypass: Component


@dataclass(frozen=True)
class ParallelLink:
    """One output pin and inline passive feeding a common downstream net."""

    passive: Component
    owner_terminal: str
    passive_owner_terminal: str
    passive_common_terminal: str


@dataclass(frozen=True)
class TransformerChain:
    """A parallel active driver followed by shunt, transformer, and connector."""

    driver: Component
    input_net_ref: str
    input_net: dict[str, Any]
    input_terminals: tuple[str, ...]
    links: tuple[ParallelLink, ...]
    common_net_ref: str
    shunt: Component
    shunt_common_terminal: str
    shunt_return_terminal: str
    transformer: Component
    transformer_common_terminal: str
    transformer_return_terminal: str
    transformer_signal_terminal: str
    transformer_connector_return_terminal: str
    coupling: Component
    coupling_transformer_terminal: str
    coupling_connector_terminal: str
    connector: Component
    connector_signal_terminal: str
    connector_return_terminal: str
    bypass: Component


@dataclass(frozen=True)
class NetSymbolAttachment:
    """A wire endpoint to which a one-pin net symbol will later be attached."""

    net_ref: str
    net: dict[str, Any]
    target: Point
    rotation: float = 0.0
    outward_side: str | None = None
    origins: tuple[Point, ...] = ()

    def position(self) -> Position:
        properties = self.net.get("properties")
        if not isinstance(properties, dict) or "__symbol_value" not in properties:
            # Plain named nets acquire Schemer's neutral termination symbol
            # when the proposal shadow is materialised. Position against that
            # future symbol now: its stored anchor is not its electrical pin,
            # and treating the target as both creates an otherwise unexplained
            # dogleg after recompilation.
            projected = {
                "properties": {"__symbol_value": SIGNAL_TERMINATION_SYMBOL},
            }
            return position_net_symbol_pin(
                projected,
                self.target,
                rotation=self.rotation,
            )
        return position_net_symbol_pin(
            self.net,
            self.target,
            rotation=self.rotation,
        )


@dataclass(frozen=True)
class PassiveChain:
    """An ordered resistor path whose intermediate nets must stay visible."""

    entries: tuple[tuple[Component, str, str], ...]
    net_refs: tuple[str, ...]
