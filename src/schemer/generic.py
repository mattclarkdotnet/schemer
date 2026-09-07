"""Deterministic placement for small, flat, generic-symbol circuits."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from schemer.layout import Position
from schemer.process import ComponentRole, PlacementDecision, PlacementResult, PlacementStage
from schemer.symbol_geometry import rotated_offset, symbol_pin_offsets
from schemer.toolchain import ToolchainError

_X_SPACING = 300.0
_Y_SPACING = 220.0
_SHUNT_OFFSET = 260.0
_COMMON_POWER_TYPES = {"ferrite_bead"}


@dataclass(frozen=True)
class _SemanticPlacement:
    """Coordinate-free placement facts consumed by the geometry pass."""

    decision: PlacementDecision
    flow_layer: int | None = None
    flow_row: int | None = None
    proximity_rank: int | None = None


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)
    )


def _is_rail(net_ref: str, net: dict[str, Any]) -> bool:
    kind = str(net.get("kind", "")).casefold()
    name = str(net.get("name", net_ref)).upper()
    return kind in {"ground", "power"} or bool(
        re.search(r"(?:^|_)(?:GND|GROUND|VCC|VDD|POWER)(?:$|_)", name)
    )


def _is_return(net_ref: str, net: dict[str, Any]) -> bool:
    kind = str(net.get("kind", "")).casefold()
    name = str(net.get("name", net_ref)).upper()
    return kind == "ground" or bool(re.search(r"(?:^|_)(?:GND|GROUND)(?:$|_)", name))


def _is_input(net_ref: str, net: dict[str, Any]) -> bool:
    name = str(net.get("name", net_ref)).upper()
    return bool(re.search(r"(?:^|_)(?:IN|INPUT)(?:$|_)", name))


def _is_output(net_ref: str, net: dict[str, Any]) -> bool:
    name = str(net.get("name", net_ref)).upper()
    return bool(re.search(r"(?:^|_)(?:OUT|OUTPUT)(?:$|_)", name))


def _component_type(instance: dict[str, Any]) -> str:
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return ""
    value = attributes.get("type")
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, dict):
        string_value = value.get("String")
        if isinstance(string_value, str):
            return string_value.casefold()
    return ""


def _attribute_string(instance: dict[str, Any], name: str) -> str | None:
    attributes = instance.get("attributes")
    if not isinstance(attributes, dict):
        return None
    value = attributes.get(name)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        string_value = value.get("String")
        if isinstance(string_value, str):
            return string_value
    return None


_CAPACITANCE_SCALE = {
    "pf": 1e-12,
    "nf": 1e-9,
    "uf": 1e-6,
    "µf": 1e-6,
    "μf": 1e-6,
    "mf": 1e-3,
    "f": 1.0,
}


def _capacitance_farads(instance: dict[str, Any]) -> float | None:
    raw_value = _attribute_string(instance, "capacitance") or _attribute_string(instance, "value")
    if raw_value is None:
        return None
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?|\.\d+)\s*(pF|nF|uF|µF|μF|mF|F)\s*",
        raw_value,
        re.IGNORECASE,
    )
    if match is None:
        return None
    magnitude = float(match.group(1))
    unit = match.group(2).lower()
    return magnitude * _CAPACITANCE_SCALE[unit]


def _anchor_rotation(
    symbol_id: str,
    *,
    ids_to_refs: dict[str, str],
    component_instances: dict[str, dict[str, Any]],
    component_nets: dict[str, set[str]],
    net_components: dict[str, set[str]],
    nets: dict[str, dict[str, Any]],
    rail_nets: set[str],
    layers: dict[str, int],
    main: set[str],
) -> tuple[float, str]:
    """Choose a rotation from real pin offsets and semantic flow direction."""

    pin_offsets = symbol_pin_offsets(component_instances[symbol_id])
    if not pin_offsets:
        return 0.0, "rotation 0 retained because evaluated symbol pin geometry is unavailable"

    desired_pins: list[tuple[tuple[float, float], int]] = []
    component_ref = ids_to_refs[symbol_id]
    own_layer = layers[symbol_id]
    for net_ref in sorted(component_nets[symbol_id], key=_natural_key):
        if net_ref in rail_nets:
            continue
        net = nets[net_ref]
        neighbours = (net_components[net_ref] & main) - {symbol_id}
        desired_x = 0
        if neighbours:
            neighbour_layer = sum(layers[neighbour] for neighbour in neighbours) / len(neighbours)
            desired_x = 1 if neighbour_layer > own_layer else -1
        elif _is_input(net_ref, net):
            desired_x = -1
        elif _is_output(net_ref, net):
            desired_x = 1
        if desired_x == 0:
            continue
        ports = net.get("ports", [])
        if not isinstance(ports, list):
            continue
        prefix = component_ref + "."
        for port_ref in ports:
            if not isinstance(port_ref, str) or not port_ref.startswith(prefix):
                continue
            pin_name = port_ref.removeprefix(prefix)
            offset = pin_offsets.get(pin_name)
            if offset is not None:
                desired_pins.append((offset, desired_x))

    if not desired_pins:
        return 0.0, "rotation 0 retained because no signal pin has a semantic flow direction"

    def score(rotation: float) -> tuple[float, float]:
        projected = 0.0
        vertical = 0.0
        for offset, desired_x in desired_pins:
            pin_x, pin_y = rotated_offset(offset, rotation)
            projected -= desired_x * pin_x
            vertical += abs(pin_y)
        return projected, vertical

    rotations = (0.0, 90.0, 180.0, 270.0)
    selected = min(rotations, key=lambda rotation: (*score(rotation), rotation))
    return (
        selected,
        f"rotation {selected:.0f} selected from {len(desired_pins)} real pin offsets "
        "to face semantic upstream/downstream endpoints",
    )


def _physical_components(
    schematic: dict[str, Any], root_ref: str
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, dict[str, Any]],
]:
    """Return viewer-ID/ref maps for component leaves below one root module."""

    raw_instances = schematic.get("instances")
    if not isinstance(raw_instances, dict):
        raise ToolchainError("schematic instances were not an object")

    ids_to_refs: dict[str, str] = {}
    refs_to_ids: dict[str, str] = {}
    component_types: dict[str, str] = {}
    component_instances: dict[str, dict[str, Any]] = {}
    prefix = root_ref + "."
    for instance_ref, instance in raw_instances.items():
        if not isinstance(instance_ref, str) or not instance_ref.startswith(prefix):
            continue
        if not isinstance(instance, dict) or instance.get("kind") != "Component":
            continue
        if not instance.get("reference_designator"):
            continue
        symbol_id = "comp:" + instance_ref.removeprefix(prefix)
        ids_to_refs[symbol_id] = instance_ref
        refs_to_ids[instance_ref] = symbol_id
        component_types[symbol_id] = _component_type(instance)
        component_instances[symbol_id] = instance

    if not ids_to_refs:
        raise ToolchainError("root module contains no physical components")
    return ids_to_refs, refs_to_ids, component_types, component_instances


def generic_flat_layout(schematic: dict[str, Any]) -> PlacementResult:
    """Build a semantic plan, then realize it as a small flat layout.

    This deliberately narrow scaffold supports the generic-symbol end-to-end
    fixtures. It is also a topology-derived primitive for flat modules that do
    not need the viewer-backed hierarchical seed.
    """

    root_ref = schematic.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic has no root reference")
    ids_to_refs, refs_to_ids, component_types, component_instances = _physical_components(
        schematic, root_ref
    )

    raw_nets = schematic.get("nets")
    if not isinstance(raw_nets, dict):
        raise ToolchainError("schematic nets were not an object")

    component_nets: dict[str, set[str]] = {symbol_id: set() for symbol_id in ids_to_refs}
    net_components: dict[str, set[str]] = defaultdict(set)
    nets: dict[str, dict[str, Any]] = {}
    for net_ref, net in raw_nets.items():
        if not isinstance(net_ref, str) or not isinstance(net, dict):
            continue
        nets[net_ref] = net
        ports = net.get("ports", [])
        if not isinstance(ports, list):
            continue
        for port_ref in ports:
            if not isinstance(port_ref, str):
                continue
            component_ref = port_ref.rsplit(".", 1)[0]
            symbol_id = refs_to_ids.get(component_ref)
            if symbol_id is not None:
                component_nets[symbol_id].add(net_ref)
                net_components[net_ref].add(symbol_id)

    rail_nets = {net_ref for net_ref, net in nets.items() if _is_rail(net_ref, net)}
    return_nets = {net_ref for net_ref, net in nets.items() if _is_return(net_ref, net)}
    supply_nets = rail_nets - return_nets
    decouplers = {
        symbol_id
        for symbol_id, attached in component_nets.items()
        if component_types[symbol_id] == "capacitor"
        and attached & supply_nets
        and attached & return_nets
    }
    power_elements = {
        symbol_id
        for symbol_id, component_type in component_types.items()
        if component_type in _COMMON_POWER_TYPES
    }
    shunts = {
        symbol_id
        for symbol_id, attached in component_nets.items()
        if symbol_id not in decouplers
        and component_types[symbol_id] in {"capacitor", "resistor"}
        and attached & rail_nets
        and attached - rail_nets
    }
    main = set(ids_to_refs) - shunts - decouplers - power_elements
    if not main:
        main = set(ids_to_refs)
        shunts = set()
        decouplers = set()
        power_elements = set()

    adjacency: dict[str, set[str]] = {symbol_id: set() for symbol_id in main}
    for net_ref, attached in net_components.items():
        if net_ref in rail_nets:
            continue
        on_path = attached & main
        for symbol_id in on_path:
            adjacency[symbol_id].update(on_path - {symbol_id})

    sources = {
        symbol_id
        for net_ref, attached in net_components.items()
        if _is_input(net_ref, nets[net_ref]) and len(attached & main) == 1
        for symbol_id in attached & main
    }
    if not sources:
        sources = {min(main, key=_natural_key)}

    layers: dict[str, int] = {}
    queue: deque[str] = deque()
    for symbol_id in sorted(sources, key=_natural_key):
        layers[symbol_id] = 0
        queue.append(symbol_id)
    while queue:
        symbol_id = queue.popleft()
        for neighbour in sorted(adjacency[symbol_id], key=_natural_key):
            if neighbour in layers:
                continue
            layers[neighbour] = layers[symbol_id] + 1
            queue.append(neighbour)

    next_layer = max(layers.values(), default=-1) + 1
    for symbol_id in sorted(main - layers.keys(), key=_natural_key):
        layers[symbol_id] = next_layer
        next_layer += 1

    by_layer: dict[int, list[str]] = defaultdict(list)
    for symbol_id, layer in layers.items():
        by_layer[layer].append(symbol_id)

    semantic: dict[str, _SemanticPlacement] = {}
    for layer, members in sorted(by_layer.items()):
        ordered = sorted(members, key=_natural_key)
        for row, symbol_id in enumerate(ordered):
            is_anchor = component_types[symbol_id] not in {"capacitor", "resistor"}
            role = ComponentRole.MAJOR_ANCHOR if is_anchor else ComponentRole.SIGNAL_PATH
            stage = PlacementStage.MAJOR_ANCHORS if is_anchor else PlacementStage.SIGNAL_NETWORKS
            rotation, orientation_reason = _anchor_rotation(
                symbol_id,
                ids_to_refs=ids_to_refs,
                component_instances=component_instances,
                component_nets=component_nets,
                net_components=net_components,
                nets=nets,
                rail_nets=rail_nets,
                layers=layers,
                main=main,
            )
            if is_anchor:
                reason = (
                    "active or connector-like device anchors the functional flow; "
                    + orientation_reason
                )
            else:
                reason = (
                    f"inline component occupies signal-flow layer {layer}; " + orientation_reason
                )
            semantic[symbol_id] = _SemanticPlacement(
                decision=PlacementDecision(
                    symbol_id=symbol_id,
                    role=role,
                    stage=stage,
                    owner=None,
                    rotation=rotation,
                    reason=reason,
                ),
                flow_layer=layer,
                flow_row=row,
            )

    for index, symbol_id in enumerate(sorted(shunts, key=_natural_key)):
        signal_nets = component_nets[symbol_id] - rail_nets
        neighbours = {
            neighbour for net_ref in signal_nets for neighbour in net_components[net_ref] & main
        }
        anchor_id = min(neighbours, key=_natural_key) if neighbours else None
        semantic[symbol_id] = _SemanticPlacement(
            decision=PlacementDecision(
                symbol_id=symbol_id,
                role=ComponentRole.SIGNAL_SHUNT,
                stage=PlacementStage.SIGNAL_NETWORKS,
                owner=anchor_id,
                rotation=0.0,
                reason="passive joins a signal net to a rail and is drawn as a local shunt",
            ),
            flow_row=index,
        )

    power_element_owners: dict[str, str] = {}
    for symbol_id in sorted(power_elements, key=_natural_key):
        attached_supplies = component_nets[symbol_id] & supply_nets
        consumers = {
            consumer
            for net_ref in attached_supplies
            for consumer in net_components[net_ref] & main
            if component_types[consumer] not in {"capacitor", "resistor"}
        }
        owner = min(consumers, key=_natural_key) if consumers else min(main, key=_natural_key)
        power_element_owners[symbol_id] = owner
        semantic[symbol_id] = _SemanticPlacement(
            decision=PlacementDecision(
                symbol_id=symbol_id,
                role=ComponentRole.COMMON_POWER,
                stage=PlacementStage.POWER_STRUCTURE,
                owner=owner,
                rotation=90.0,
                reason="shared power filtering belongs to a branch above its consumer",
            )
        )

    local_decoupler_owners: dict[str, str] = {}
    common_decoupler_owners: dict[str, str] = {}
    for symbol_id in sorted(decouplers, key=_natural_key):
        shared_supplies = component_nets[symbol_id] & supply_nets
        consumers = {
            consumer
            for net_ref in shared_supplies
            for consumer in net_components[net_ref] & main
            if component_types[consumer] not in {"capacitor", "resistor"}
        }
        if consumers:
            local_decoupler_owners[symbol_id] = min(consumers, key=_natural_key)
            continue
        branch_elements = {
            element
            for net_ref in shared_supplies
            for element in net_components[net_ref] & power_elements
        }
        if branch_elements:
            branch_element = min(branch_elements, key=_natural_key)
            common_decoupler_owners[symbol_id] = power_element_owners[branch_element]
            continue
        local_decoupler_owners[symbol_id] = min(main, key=_natural_key)

    decouplers_by_owner: dict[str, list[str]] = defaultdict(list)
    for symbol_id, owner in local_decoupler_owners.items():
        decouplers_by_owner[owner].append(symbol_id)
    for owner, owned_decouplers in sorted(decouplers_by_owner.items()):
        ordered = sorted(
            owned_decouplers,
            key=lambda symbol_id: (
                _capacitance_farads(component_instances[symbol_id]) is None,
                _capacitance_farads(component_instances[symbol_id]) or 0.0,
                _natural_key(symbol_id),
            ),
        )
        for rank, symbol_id in enumerate(ordered):
            capacitance = _attribute_string(component_instances[symbol_id], "capacitance")
            ranking_reason = (
                f"{capacitance} decoupler rank {rank + 1}/{len(ordered)}; "
                "lower capacitance is placed closer to the consumer"
                if capacitance is not None
                else f"decoupler rank {rank + 1}/{len(ordered)}; value unavailable"
            )
            semantic[symbol_id] = _SemanticPlacement(
                decision=PlacementDecision(
                    symbol_id=symbol_id,
                    role=ComponentRole.LOCAL_DECOUPLING,
                    stage=PlacementStage.POWER_STRUCTURE,
                    owner=owner,
                    rotation=0.0,
                    reason=ranking_reason,
                ),
                proximity_rank=rank,
            )

    for symbol_id, owner in sorted(common_decoupler_owners.items()):
        capacitance = _attribute_string(component_instances[symbol_id], "capacitance")
        semantic[symbol_id] = _SemanticPlacement(
            decision=PlacementDecision(
                symbol_id=symbol_id,
                role=ComponentRole.COMMON_POWER,
                stage=PlacementStage.POWER_STRUCTURE,
                owner=owner,
                rotation=0.0,
                reason=(
                    f"{capacitance or 'bulk'} capacitor is upstream of local distribution and "
                    "belongs to the common power branch"
                ),
            )
        )

    if set(semantic) != set(ids_to_refs):
        unclassified = sorted(set(ids_to_refs) - set(semantic), key=_natural_key)
        raise ToolchainError(f"semantic placement left components unclassified: {unclassified}")

    # Geometry realization starts only after every component has a semantic role,
    # owner (where applicable), and topological slot.
    positions: dict[str, Position] = {}
    for layer, members in sorted(by_layer.items()):
        ordered = sorted(members, key=_natural_key)
        midpoint = (len(ordered) - 1) / 2
        for row, symbol_id in enumerate(ordered):
            positions[symbol_id] = Position(
                x=layer * _X_SPACING,
                y=(row - midpoint) * _Y_SPACING,
                rotation=semantic[symbol_id].decision.rotation,
            )

    for symbol_id in sorted(shunts, key=_natural_key):
        signal_nets = component_nets[symbol_id] - rail_nets
        neighbours = {
            neighbour for net_ref in signal_nets for neighbour in net_components[net_ref] & main
        }
        anchors = [positions[neighbour] for neighbour in sorted(neighbours, key=_natural_key)]
        x = sum(position.x for position in anchors) / len(anchors) if anchors else 0.0
        if len(anchors) == 1 and any(_is_output(net_ref, nets[net_ref]) for net_ref in signal_nets):
            x += _X_SPACING / 2
        y = max((position.y for position in anchors), default=0.0) + _SHUNT_OFFSET
        row = semantic[symbol_id].flow_row or 0
        positions[symbol_id] = Position(
            x=x,
            y=y + row * _Y_SPACING,
            rotation=semantic[symbol_id].decision.rotation,
        )

    for index, symbol_id in enumerate(sorted(power_elements, key=_natural_key)):
        owner = power_element_owners[symbol_id]
        anchor = positions[owner]
        positions[symbol_id] = Position(
            x=anchor.x + index * _X_SPACING,
            y=anchor.y - _SHUNT_OFFSET,
            rotation=semantic[symbol_id].decision.rotation,
        )

    for index, symbol_id in enumerate(sorted(common_decoupler_owners, key=_natural_key)):
        attached_supplies = component_nets[symbol_id] & supply_nets
        branch_elements = {
            element
            for net_ref in attached_supplies
            for element in net_components[net_ref] & power_elements
        }
        branch_id = min(branch_elements, key=_natural_key)
        branch = positions[branch_id]
        positions[symbol_id] = Position(
            x=branch.x - _Y_SPACING - index * _Y_SPACING,
            y=branch.y + _Y_SPACING / 2,
            rotation=semantic[symbol_id].decision.rotation,
        )

    for symbol_id in sorted(local_decoupler_owners, key=_natural_key):
        placement = semantic[symbol_id]
        owner = placement.decision.owner
        if owner is None:
            raise ToolchainError(f"local decoupler has no semantic owner: {symbol_id}")
        anchor = positions[owner]
        rank = placement.proximity_rank or 0
        positions[symbol_id] = Position(
            x=anchor.x + _Y_SPACING + rank * _Y_SPACING,
            y=anchor.y,
            rotation=semantic[symbol_id].decision.rotation,
        )

    return PlacementResult(
        positions=positions,
        decisions={symbol_id: placement.decision for symbol_id, placement in semantic.items()},
    )


def generic_flat_positions(schematic: dict[str, Any]) -> dict[str, Position]:
    """Compatibility wrapper returning only realized viewer coordinates."""

    return generic_flat_layout(schematic).positions
