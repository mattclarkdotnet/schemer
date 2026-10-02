from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from schemer.analysis.measurements import top_level_root_symbol_groups
from schemer.analysis.visibility import clean_schematic_labels
from schemer.core.errors import KiCadSchematicError
from schemer.core.layout import Position
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.library import (
    placed_pin_sides,
    symbol_library_pins,
)
from schemer.kicad.items import Vector2
from schemer.kicad.records import KiCadSymbol
from schemer.native.model import NetSymbolTarget, PositionedNetSymbol
from schemer.native.packing import DRAWING_MARGIN_MM
from schemer.native.policy import MULTI_UNIT_PITCH_MM, VIEWER_UNITS_PER_MM
from schemer.native.routing_model import PlacedEndpoint
from schemer.symbols.net_symbols import net_symbol_pin_position, net_with_default_signal_symbol


def ordered_unit_position(base_x: float, base_y: float, index: int) -> tuple[float, float]:
    """Place one physical component's units on a shared vertical axis."""

    return base_x, base_y + index * MULTI_UNIT_PITCH_MM * VIEWER_UNITS_PER_MM


def power_unit_rotation(editor: FileSchematic, symbol: KiCadSymbol) -> float:
    """Put opposed supply banks on the sides, with a lone auxiliary pin below.

    Only dedicated power units qualify. NC pins do not establish a wiring
    face. A simple two-pin supply unit, mixed signal unit or ambiguous set
    of populated faces keeps its native orientation.
    """
    pins = {number: pin for number, pin in symbol_library_pins(editor.document, symbol).items()
            if pin.electrical_type != "no_connect"}
    if len(pins) <= 2 or any(p.electrical_type not in {"power_in", "power_out"}
                             for p in pins.values()):
        return 0.0
    sides = placed_pin_sides(editor.document, replace(symbol, rotation=0))
    counts = {side: sum(sides[number] == side for number in pins)
              for side in ("left", "right", "top", "bottom")}
    if (min(counts["top"], counts["bottom"]) < 2
            or counts["left"] + counts["right"] > 1):
        return 0.0
    # KiCad's positive angles turn counterclockwise: a right-face auxiliary
    # goes south with a clockwise quarter-turn, a left-face one with CCW.
    return 270.0 if counts["right"] else 90.0


def flatten_positions(
    schematic: dict[str, Any],
) -> tuple[dict[str, Position], list[PositionedNetSymbol]]:
    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    nets = schematic.get("nets")
    if (
        not isinstance(root_ref, str)
        or not isinstance(instances, dict)
        or not isinstance(nets, dict)
    ):
        raise KiCadSchematicError("evaluated schematic lacks root, instances, or nets")

    modules = {
        ref: instance
        for ref, instance in instances.items()
        if isinstance(ref, str)
        and isinstance(instance, dict)
        and isinstance(instance.get("symbol_positions"), dict)
        and instance["symbol_positions"]
    }
    offsets: dict[str, tuple[float, float]] = {root_ref: (0.0, 0.0)}
    unresolved = set(modules) - {root_ref}
    while unresolved:
        progress = False
        for module_ref in sorted(unresolved, key=len):
            module_path = module_ref.removeprefix(root_ref + ".")
            for parent_ref, parent in modules.items():
                if parent_ref not in offsets or parent_ref == module_ref:
                    continue
                parent_path = (
                    "" if parent_ref == root_ref else parent_ref.removeprefix(root_ref + ".")
                )
                if parent_path and not module_path.startswith(parent_path + "."):
                    continue
                local_path = (
                    module_path if not parent_path else module_path.removeprefix(parent_path + ".")
                )
                raw = parent["symbol_positions"].get(f"comp:{local_path}")
                if isinstance(raw, dict):
                    position = _position(raw)
                    offsets[module_ref] = (
                        offsets[parent_ref][0] + position.x,
                        offsets[parent_ref][1] + position.y,
                    )
                    unresolved.remove(module_ref)
                    progress = True
                    break
        if not progress:
            break

    physical_refs = {
        ref
        for ref, instance in instances.items()
        if isinstance(ref, str)
        and isinstance(instance, dict)
        and instance.get("reference_designator")
    }
    component_candidates: dict[str, list[tuple[int, Position]]] = defaultdict(list)
    root_symbol_groups = top_level_root_symbol_groups(schematic)
    net_positions: list[PositionedNetSymbol] = []
    net_names = {
        net.get("name")
        for net in nets.values()
        if isinstance(net, dict) and isinstance(net.get("name"), str)
    }
    for module_ref, module in modules.items():
        if module_ref not in offsets:
            continue
        module_path = "" if module_ref == root_ref else module_ref.removeprefix(root_ref + ".")
        for symbol_id, raw in module["symbol_positions"].items():
            if not isinstance(symbol_id, str) or not isinstance(raw, dict):
                continue
            position = _position(raw)
            absolute = Position(
                position.x + offsets[module_ref][0],
                position.y + offsets[module_ref][1],
                position.rotation,
                position.mirror,
            )
            if symbol_id.startswith("comp:"):
                local = symbol_id.removeprefix("comp:").split("@", 1)[0]
                path = local if not module_path else f"{module_path}.{local}"
                full_ref = f"{root_ref}.{path}"
                if full_ref in physical_refs:
                    depth = 0 if not module_path else len(module_path.split("."))
                    component_candidates[full_ref].append((depth, absolute))
            elif symbol_id.startswith("sym:"):
                value = symbol_id.removeprefix("sym:")
                local_name, separator, index = value.rpartition("#")
                if not separator or not index.isdigit():
                    continue
                scoped = f"{module_path}.{local_name}" if module_path else local_name
                actual_name = local_name if local_name in net_names else scoped
                if actual_name in net_names:
                    group = (
                        root_symbol_groups.get(symbol_id)
                        if module_ref == root_ref
                        else module_path.split(".", 1)[0]
                    )
                    net_positions.append(
                        PositionedNetSymbol(actual_name, symbol_id, absolute, group)
                    )

    missing = physical_refs - set(component_candidates)
    if missing:
        raise KiCadSchematicError(
            "accepted layout lacks physical components: "
            + ", ".join(sorted(ref.removeprefix(root_ref + ".") for ref in missing))
        )
    components = {
        ref: min(candidates, key=lambda candidate: candidate[0])[1]
        for ref, candidates in component_candidates.items()
    }
    return components, net_positions


def _position(raw: dict[str, Any]) -> Position:
    return Position(
        float(raw["x"]),
        float(raw["y"]),
        float(raw.get("rotation", 0.0)),
        raw.get("mirror") if isinstance(raw.get("mirror"), str) else None,
    )


def drawing_translation(
    visible_targets: list[tuple[float, float, float]],
) -> tuple[float, float]:
    """Move only visible electrical geometry to the drawing margin."""

    min_x = min((target[0] for target in visible_targets), default=0.0)
    min_y = min((target[1] for target in visible_targets), default=0.0)
    margin = DRAWING_MARGIN_MM * VIEWER_UNITS_PER_MM
    return margin - min_x, margin - min_y


def display_names(schematic: dict[str, Any]) -> dict[str, str]:
    cleaned = clean_schematic_labels(schematic)
    return {
        str(net["id"]): net["name"]
        for net in cleaned["nets"].values()
        if isinstance(net, dict) and "id" in net and isinstance(net.get("name"), str)
    }


def net_symbol_targets(
    schematic: dict[str, Any],
    positions: list[PositionedNetSymbol],
    display_by_net: dict[str, str],
    translation: tuple[float, float],
    group_deltas: dict[str, Vector2],
    global_delta: Vector2,
    component_endpoints: dict[str, list[PlacedEndpoint]] | None = None,
    *,
    required_nets: frozenset[str] = frozenset(),
) -> list[NetSymbolTarget]:
    nets_by_name = {
        net["name"]: net
        for net in schematic["nets"].values()
        if isinstance(net, dict) and isinstance(net.get("name"), str)
    }
    targets = []
    for placed in positions:
        net_name = placed.net_name
        position = placed.position
        net = nets_by_name[net_name]
        point = net_symbol_pin_position(net_with_default_signal_symbol(net), position)
        unshifted = Vector2.from_xy_mm(
            (point.x + translation[0]) / VIEWER_UNITS_PER_MM,
            (point.y + translation[1]) / VIEWER_UNITS_PER_MM,
        )
        delta = group_deltas.get(placed.group, global_delta)
        target = Vector2(unshifted.x + delta.x, unshifted.y + delta.y)
        targets.append(
            NetSymbolTarget(
                net_name=net_name,
                display_name=display_by_net.get(str(net.get("id")), net_name.split(".")[-1]),
                position=target,
                rotation=position.rotation,
                rail=net.get("kind") in {"Power", "Ground"},
                ground=net.get("kind") == "Ground",
                group=placed.group,
                required=net_name in required_nets,
            )
        )
    represented = {target.net_name for target in targets}
    for net_name, endpoints in (component_endpoints or {}).items():
        net = nets_by_name[net_name]
        if (net_name in represented or net.get("kind") == "NotConnected"
                or (net_name not in required_nets
                    and len({endpoint.group for endpoint in endpoints}) < 2)):
            continue
        # Independently packed blocks cannot retain an unowned cross-block
        # wire. An interface needs local terminations even if the earlier
        # renderer did not emit any explicit symbol-position comments.
        targets.append(NetSymbolTarget(
            net_name=net_name,
            display_name=display_by_net.get(str(net.get("id")), net_name.split(".")[-1]),
            position=endpoints[0].position,
            rotation=0,
            rail=net.get("kind") in {"Power", "Ground"},
            ground=net.get("kind") == "Ground",
            group=None,
            required=net_name in required_nets,
        ))
    return targets
