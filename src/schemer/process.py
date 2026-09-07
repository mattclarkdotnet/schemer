"""Semantic placement records shared by layout implementations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from schemer.layout import Position


class PlacementStage(StrEnum):
    """The semantic stage at which a component enters the drawing."""

    MAJOR_ANCHORS = "major_anchors"
    POWER_STRUCTURE = "power_structure"
    SIGNAL_NETWORKS = "signal_networks"
    SERVICE_ITEMS = "service_items"


class ComponentRole(StrEnum):
    """A component's engineering role in the schematic narrative."""

    MAJOR_ANCHOR = "major_anchor"
    SIGNAL_PATH = "signal_path"
    SIGNAL_SHUNT = "signal_shunt"
    LOCAL_DECOUPLING = "local_decoupling"
    COMMON_POWER = "common_power"
    SERVICE = "service"
    MECHANICAL = "mechanical"


@dataclass(frozen=True)
class PlacementDecision:
    """Auditable semantic reason for placing one visible component."""

    symbol_id: str
    role: ComponentRole
    stage: PlacementStage
    owner: str | None
    rotation: float
    reason: str


@dataclass(frozen=True)
class PlacementResult:
    """Semantic decisions followed by their realized viewer coordinates."""

    positions: dict[str, Position]
    decisions: dict[str, PlacementDecision]

    def __post_init__(self) -> None:
        position_ids = set(self.positions)
        decision_ids = set(self.decisions)
        if position_ids != decision_ids:
            missing_decisions = sorted(position_ids - decision_ids)
            missing_positions = sorted(decision_ids - position_ids)
            raise ValueError(
                "placement trace and geometry cover different symbols: "
                f"missing decisions={missing_decisions}, missing positions={missing_positions}"
            )
