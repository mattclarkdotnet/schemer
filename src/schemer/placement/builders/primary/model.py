from __future__ import annotations

from dataclasses import dataclass, field

from schemer.analysis.roles import ComponentRole
from schemer.core.layout import Position
from schemer.placement.circuits.model import Component, NetSymbolAttachment
from schemer.symbols.geometry import placed_symbol_bounds
from schemer.symbols.model import PlacedBounds


@dataclass(frozen=True)
class PrimaryCircuit:
    """Validated local topology; no positions or rendering decisions."""

    components: tuple[Component, ...]
    primary: Component
    subordinate: tuple[Component, ...]
    component_refs: tuple[str, ...]
    boundary_net_names: set[str]
    subordinate_links: dict[str, tuple[str, str, str]]
    series: list[tuple[Component, str, str, str]]
    shunts: list[tuple[Component, str, str, str, str]]
    rail_feeds: list[tuple[Component, str, str, str]]
    authored_owner_refs: dict[str, str]
    primary_bias_roles: dict[str, ComponentRole]
    shunts_by_ref: dict[str, tuple[Component, str, str, str, str]]
    power_feed_roles: dict[str, ComponentRole]


@dataclass
class PrimaryPlacement:
    """Geometry accumulated by the primary circuit placement phases."""

    recipe: PrimaryCircuit
    primary_position: Position = field(default_factory=lambda: Position(0.0, 0.0, 0.0))
    positions: dict[str, Position] = field(default_factory=dict)
    attachments: list[NetSymbolAttachment] = field(default_factory=list)
    labelled_nets: set[str] = field(default_factory=set)
    skipped_primary_rails: set[str] = field(default_factory=set)
    placed_active: dict[str, tuple[Component, Position]] = field(default_factory=dict)

    @property
    def primary_bounds(self) -> PlacedBounds:
        return placed_symbol_bounds(self.recipe.primary.instance, self.primary_position)
