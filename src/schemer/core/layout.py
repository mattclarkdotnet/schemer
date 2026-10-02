from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from schemer.core.errors import ToolchainError


@dataclass(frozen=True)
class Position:
    """A viewer position persisted by pcb-sch."""

    x: float
    y: float
    rotation: float = 0
    mirror: str | None = None

    def as_viewer_dict(self) -> dict[str, float | str]:
        result: dict[str, float | str] = {
            "x": self.x,
            "y": self.y,
            "rotation": self.rotation,
        }
        if self.mirror is not None:
            result["mirror"] = self.mirror
        return result


@dataclass(frozen=True)
class ModuleLayout:
    """Positions owned by one evaluated module and one Zener source file."""

    instance_ref: str
    source_path: Path
    positions: dict[str, Position]
    source_net_names: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutPlan:
    """A complete multi-module layout proposal for one evaluated entrypoint."""

    modules: tuple[ModuleLayout, ...]

    def apply_to_schematic(self, schematic: dict[str, Any]) -> dict[str, Any]:
        proposed = deepcopy(schematic)
        instances = proposed.get("instances")
        if not isinstance(instances, dict):
            raise ToolchainError("schematic instances were not an object")

        for module in self.modules:
            instance = instances.get(module.instance_ref)
            if not isinstance(instance, dict):
                raise ToolchainError(
                    f"layout module is absent from evaluation: {module.instance_ref}"
                )
            positions = resolve_module_position_ids(module, proposed)
            instance["symbol_positions"] = {
                symbol_id: position.as_viewer_dict() for symbol_id, position in positions.items()
            }
        return proposed


def resolve_module_position_ids(
    module: ModuleLayout, schematic: dict[str, Any]
) -> dict[str, Position]:
    """Resolve source-local net symbols to evaluated, instance-scoped viewer IDs."""

    root_ref = schematic.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic has no root reference")
    if module.instance_ref == root_ref:
        module_path = ""
    elif module.instance_ref.startswith(root_ref + "."):
        module_path = module.instance_ref.removeprefix(root_ref + ".")
    else:
        raise ToolchainError(f"layout module is outside the schematic root: {module.instance_ref}")

    raw_nets = schematic.get("nets")
    if not isinstance(raw_nets, dict):
        raise ToolchainError("schematic nets were not an object")
    net_names = {
        net.get("name")
        for net in raw_nets.values()
        if isinstance(net, dict) and isinstance(net.get("name"), str)
    }

    resolved: dict[str, Position] = {}
    for symbol_id, position in module.positions.items():
        if symbol_id.startswith("comp:"):
            resolved_id = symbol_id
        elif symbol_id.startswith("sym:"):
            net_symbol = symbol_id.removeprefix("sym:")
            net_name, separator, suffix = net_symbol.rpartition("#")
            if not separator or not suffix.isdigit():
                raise ToolchainError(f"invalid schematic net-symbol ID: {symbol_id}")
            scoped_name = f"{module_path}.{net_name}" if module_path else net_name
            if net_name in net_names:
                actual_name = net_name
            elif scoped_name in net_names:
                actual_name = scoped_name
            else:
                raise ToolchainError(
                    f"layout net symbol does not resolve in {module.instance_ref}: {symbol_id}"
                )
            resolved_id = f"sym:{actual_name}#{suffix}"
        else:
            raise ToolchainError(f"invalid schematic symbol ID: {symbol_id}")

        if resolved_id in resolved:
            raise ToolchainError(
                f"multiple source positions resolve to the same viewer ID: {resolved_id}"
            )
        resolved[resolved_id] = position
    return resolved


def position_from_viewer(raw: dict[str, Any]) -> Position:
    return Position(
        x=float(raw["x"]),
        y=float(raw["y"]),
        rotation=float(raw.get("rotation", 0)),
        mirror=raw.get("mirror"),
    )
