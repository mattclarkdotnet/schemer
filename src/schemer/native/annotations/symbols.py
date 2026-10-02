from __future__ import annotations

from collections import defaultdict
from math import cos, radians, sin

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.text import oriented_field_text
from schemer.kicad.items import (
    BaseLabel,
    GlobalLabel,
    LocalLabel,
    SchematicSymbolInstance,
    Text,
    TextAttributes,
    Vector2,
    place_symbol,
)
from schemer.native.annotations.rails import rail_approach_side
from schemer.native.model import NetSymbolTarget
from schemer.native.routing_model import PlacedEndpoint


def label_approach_side(target: NetSymbolTarget) -> str:
    """A boxed port is entered through its tip, along the label axis."""
    sign = -1 if target.text_alignment == "left" else 1
    x, y = round(sign * cos(radians(target.rotation))), round(-sign * sin(radians(target.rotation)))
    return {(1, 0): "right", (-1, 0): "left", (0, 1): "bottom", (0, -1): "top"}[x, y]


def place_net_symbols(
    editor: FileSchematic,
    targets: list[NetSymbolTarget],
    *,
    global_nets: frozenset[str] = frozenset(),
) -> tuple[
    list[SchematicSymbolInstance],
    list[BaseLabel],
    list[str],
    list[PlacedEndpoint],
    dict[str, list[PlacedEndpoint]],
    set[str],
    dict[str, str],
]:
    power_pool: dict[str, list[SchematicSymbolInstance]] = defaultdict(list)
    for symbol in editor.get_symbols():
        if symbol.zener_path is None:
            power_pool[symbol.value].append(symbol)
    updates = []
    labels = []
    label_net_names = []
    label_anchors = []
    endpoints: dict[str, list[PlacedEndpoint]] = defaultdict(list)
    used: set[str] = set()
    item_groups: dict[str, str] = {}
    for target in targets:
        candidates = power_pool.get(target.display_name, []) if target.rail else []
        symbol = next((candidate for candidate in candidates if candidate.id not in used), None)
        if symbol is None and target.rail:
            expected_shape = "GND" if target.ground else "VCC"
            symbol = next(
                (
                    candidate
                    for pool in power_pool.values()
                    for candidate in pool
                    if candidate.id not in used
                    and candidate.library_id.rsplit(":", 1)[-1] == expected_shape
                ),
                None,
            )
            if symbol is None:
                exemplar = next((candidate for pool in power_pool.values() for candidate in pool
                                 if candidate.library_id.rsplit(":", 1)[-1] == expected_shape),
                                None)
                if exemplar is not None:
                    references = {s.reference for s in editor.get_symbols()}
                    number = 1
                    while f"#PWR{number:04}" in references:
                        number += 1
                    symbol = editor.clone_graphic_symbol(exemplar.id, f"#PWR{number:04}")
        if symbol is not None:
            place_symbol(symbol, target.position, target.rotation)
            symbol.value = target.display_name
            # Turning a rail graphic must not turn its name. Captions remain
            # independently movable and are fitted after the electrical layout.
            for field in symbol.fields:
                if field.visible:
                    field.text = oriented_field_text(field.text, target.rotation)
            updates.append(symbol)
            used.add(symbol.id)
            endpoints[target.net_name].append(
                PlacedEndpoint(target.position, side=rail_approach_side(target),
                                escape_length=635_000, owner=target.owner, group=target.group,
                                members=target.members)
            )
            if target.group is not None:
                item_groups[symbol.id] = target.group
        else:
            label_type = GlobalLabel if target.net_name in global_nets else LocalLabel
            text = Text(
                target.display_name,
                target.position,
                TextAttributes(
                    Vector2.from_xy_mm(1.27, 1.27),
                    target.rotation,
                    target.text_alignment,
                    vertical_alignment="center" if label_type is GlobalLabel else "bottom",
                ),
            )
            labels.append(label_type(id="", position=target.position, text=text))
            label_net_names.append(target.net_name)
            label_anchors.append(PlacedEndpoint(target.position,
                                                  side=label_approach_side(target)
                                                  if label_type is GlobalLabel else None,
                                                  owner=target.owner,
                                                  group=target.group, members=target.members,
                                                  escape_length=635_000
                                                  if label_type is GlobalLabel else None))
    return (
        updates,
        labels,
        label_net_names,
        label_anchors,
        endpoints,
        used,
        item_groups,
    )
