from __future__ import annotations

from collections import defaultdict
from typing import Any

from schemer.analysis.circuits import component_properties
from schemer.analysis.inventory import StructuralInventory
from schemer.analysis.repetition import repeated_owner_blocks
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    envelopes_do_not_overlap,
    union_all,
)
from schemer.kicad.geometry.library import (
    placed_pin_positions,
    placed_pin_sides,
    placed_symbol_body_positions,
    symbol_library_pins,
)
from schemer.kicad.geometry.text import (
    text_envelope,
    text_width,
)
from schemer.kicad.items import (
    SchematicSymbolInstance,
    Vector2,
    place_symbol,
)
from schemer.native.association import KiCadComponentAssociation


def select_primary_group(
    editor: FileSchematic,
    associations: tuple[KiCadComponentAssociation, ...],
    groups: dict[str, str],
) -> str:
    """Return the group containing the physical component with most pins."""

    primary = max(
        associations,
        key=lambda association: (
            sum(
                len(symbol_library_pins(editor.document, symbol)) for symbol in association.symbols
            ),
            association.path,
        ),
    )
    return groups[primary.instance_ref]


def reuse_repeated_block_geometry(
    schematic: dict[str, Any], editor: FileSchematic, groups: dict[str, str],
    inventory: StructuralInventory | None = None,
) -> dict[str, int]:
    """Use one local arrangement regardless of the inter-block representation.

    Keep source identities and actual values. Annotation and wire clearance
    still validate each instantiation; a match is not permission to skip them.
    """
    by_ref: dict[str, list[SchematicSymbolInstance]] = defaultdict(list)
    for symbol in editor.get_symbols():
        if symbol.zener_path:
            by_ref[schematic["root_ref"] + "." + symbol.zener_path].append(symbol)
    caption_widths = {}
    for family in repeated_owner_blocks(schematic, groups, inventory):
        # Prefer the instance with the largest caption demand as the common
        # geometry. References and values are never copied between instances.
        exemplar = max(family, key=lambda members: sum(
            len(s.reference) + len(s.value) for ref in members.values() for s in by_ref[ref]
        ))
        origin = min(by_ref[exemplar["anchor"]], key=lambda s: s.unit).position
        template = {(key, symbol.unit): (
            Vector2(symbol.position.x - origin.x, symbol.position.y - origin.y),
            symbol.transform.orientation,
        ) for key, ref in exemplar.items() for symbol in by_ref[ref]}
        widths: dict[tuple[str, int], int] = {}
        for members in family:
            for key, ref in members.items():
                for symbol in by_ref[ref]:
                    reference, value = symbol.reference_field.text, symbol.value_field.text
                    widths[key, symbol.unit] = max(widths.get((key, symbol.unit), 0),
                        text_width(reference) + reference.attributes.size.x + text_width(value))
        for members in family:
            anchor = min(by_ref[members["anchor"]], key=lambda s: s.unit).position
            updates = []
            for key, ref in members.items():
                for symbol in by_ref[ref]:
                    delta, rotation = template[key, symbol.unit]
                    place_symbol(symbol, Vector2(anchor.x + delta.x, anchor.y + delta.y),
                                      rotation)
                    updates.append(symbol)
                    caption_widths[symbol.id] = widths[key, symbol.unit]
            editor.update_items(updates)
    return caption_widths


def _compact_axis_offsets(boxes: dict[str, Envelope], axis: str, gap: int) -> dict[str, int]:
    """Collapse empty slabs, preserving overlapping spans and pin alignment."""
    offsets = {}
    end = None
    removed = 0
    for key, box in sorted(boxes.items(), key=lambda entry: (
            getattr(entry[1], "min_" + axis), entry[0])):
        start, stop = getattr(box, "min_" + axis), getattr(box, "max_" + axis)
        if end is not None:
            removed += max(0, start - end - gap)
        offsets[key] = -removed
        end = max(end, stop) if end is not None else stop
    return offsets


def compact_connected_circuits(
    schematic: dict[str, Any], editor: FileSchematic,
    groups: dict[str, str], direct_groups: frozenset[str],
) -> None:
    """Finish physical assemblies before composing an explicitly wired circuit.

    Seed blocks are independently spaced. Merging their wire boundaries does
    not compose their geometry. Legalize terminal/body envelopes, remove empty
    slabs within each authored assembly, then arrange those measured assemblies.
    This changes positions only: no owners, roles or wire boundaries are inferred.
    """
    if not direct_groups:
        return
    symbols = {s.id: s for s in editor.get_symbols() if s.zener_path is not None}
    source = {key: schematic["root_ref"] + "." + s.zener_path for key, s in symbols.items()}
    by_reference = {s.reference: source[key] for key, s in symbols.items()}
    parents = {ref: by_reference.get(component_properties(schematic, ref).get("owner"))
               for ref in source.values()}

    def ancestry(ref: str) -> list[str]:
        result = [ref]
        while parents.get(ref):
            ref = parents[ref]
            if ref in result:
                raise KiCadSchematicError(f"cyclic authored ownership at {ref}")
            result.append(ref)
        return result

    ancestors = {key: ancestry(ref) for key, ref in source.items()}
    step = 2_540_000
    parallel_axes = {}
    for raw in editor.document.symbols:
        sides = list(placed_pin_sides(editor.document, raw).values())
        if len(sides) == 2:
            if set(sides) == {"left", "right"}:
                parallel_axes[raw.uuid] = "x"
            elif set(sides) == {"top", "bottom"}:
                parallel_axes[raw.uuid] = "y"

    def separation(key: str, box: Envelope, peer: str, other: Envelope) -> int:
        axis = parallel_axes.get(key)
        if (axis and axis == parallel_axes.get(peer)
                and abs(getattr(box, "center_" + axis)
                        - getattr(other, "center_" + axis)) < 500_000):
            # Parallel pin rows need ink clearance, not another full pin
            # pitch. Expanding them can block the owner's next signal exit.
            return 635_000
        return step

    def bounds(ids: list[str], captions: bool = False) -> dict[str, Envelope]:
        raw = {s.uuid: s for s in editor.document.symbols}
        result = {}
        for key in ids:
            points = [*placed_symbol_body_positions(editor.document, raw[key]),
                      *placed_pin_positions(editor.document, raw[key]).values()]
            if captions:
                points.extend(p for f in symbols[key].fields
                              if f.visible and f.name in {"Reference", "Value"}
                              for p in text_envelope(f.text, symbols[key].transform.orientation))
            result[key] = envelope_from_points(points or [symbols[key].position])
        return result

    def move(key: str, delta: Vector2) -> None:
        s = symbols[key]
        place_symbol(s, Vector2(s.position.x + delta.x, s.position.y + delta.y),
                          s.transform.orientation)

    for group in sorted(direct_groups):
        ids = [key for key in symbols if groups[source[key]] == group]
        if not ids:
            continue
        original = union_all(list(bounds(ids, captions=True).values()))
        assemblies: dict[str, list[str]] = defaultdict(list)
        for key in ids:
            assemblies[ancestors[key][-1]].append(key)
        measured = {}
        for root, members in assemblies.items():
            # Root units first, followed by their transitive supports. Moving
            # a support carries its still-unplaced descendants, not just itself.
            ordered = sorted(members, key=lambda k: (
                len(ancestors[k]), symbols[k].reference, symbols[k].unit))
            occupied = []
            for index, key in enumerate(ordered):
                box = bounds([key])[key]
                selected = None
                for radius in range(65):
                    candidates = sorted({(x, y) for x in range(-radius, radius + 1)
                                         for y in (-radius + abs(x), radius - abs(x))},
                                        key=lambda p: (abs(p[1]), p))
                    for x, y in candidates:
                        delta = Vector2(x * step, y * step)
                        shifted = box.translated(delta)
                        if all(envelopes_do_not_overlap(
                                shifted, other, separation(key, shifted, peer, other))
                               for peer, other in occupied):
                            selected = delta
                            break
                    if selected is not None:
                        break
                if selected is None:
                    raise KiCadSchematicError(f"no clear assembly position for {source[key]}")
                occupied.append((key, box.translated(selected)))
                for child in ordered[index:]:
                    if child == key or source[key] in ancestors[child][1:]:
                        move(child, selected)
                editor.update_items([symbols[k] for k in ordered[index:]])
            for axis in ("x", "y"):
                offsets = _compact_axis_offsets(bounds(members, captions=True), axis, 2 * step)
                for key, offset in offsets.items():
                    move(key, Vector2(offset, 0) if axis == "x" else Vector2(0, offset))
                editor.update_items([symbols[k] for k in members])
            measured[root] = union_all(list(bounds(members, captions=True).values()))

        # Device assemblies form the main row. Unowned shared supply/access
        # items remain unowned and occupy a compact accessory row below it.
        main = sorted((root for root, members in assemblies.items() if len(members) > 1),
                      key=lambda root: schematic["instances"][root]["reference_designator"])
        accessories = sorted(set(assemblies) - set(main))
        x = bottom = 0
        for root in main:
            box = measured[root]
            delta = Vector2(x - box.min_x, -box.min_y)
            for key in assemblies[root]:
                move(key, delta)
            x += box.width + 8 * step
            bottom = max(bottom, box.height)
        width = max(x, 40 * step)
        x, y, row_height = 0, bottom + 8 * step, 0
        for root in accessories:
            box = measured[root]
            if x and x + box.width > width:
                x, y, row_height = 0, y + row_height + 4 * step, 0
            delta = Vector2(x - box.min_x, y - box.min_y)
            for key in assemblies[root]:
                move(key, delta)
            x += box.width + 4 * step
            row_height = max(row_height, box.height)
        # These are local coordinates, not sheet placement. Keep distinct
        # seed circuits separated until their captions/rails are completed
        # and the final measured-group packer assigns sheet positions.
        for key in ids:
            move(key, Vector2(original.min_x, original.min_y))
        editor.update_items([symbols[key] for key in ids])
