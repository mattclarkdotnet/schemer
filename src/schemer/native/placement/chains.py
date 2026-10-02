from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any

from schemer.analysis.circuits import (
    component_layout_groups,
    component_properties,
)
from schemer.analysis.connectivity import expected_pin_nets
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
from schemer.kicad.geometry.text import text_width
from schemer.kicad.items import (
    Vector2,
    place_symbol,
)
from schemer.kicad.records import KiCadSymbol
from schemer.native.placement.queries import owner_symbol_for_pin
from schemer.native.routing_model import PIN_STUB_MM
from schemer.symbols.library import symbol_pin_number_groups


def stack_owned_passive_runs(
    schematic: dict[str, Any], editor: FileSchematic,
) -> dict[str, tuple[str, str, str, str]]:
    """Fold runs longer than two parts into a one-part-per-leg snake.

    Each electrical run keeps its order, with alternate rows reversed so the
    connecting wire turns at the end of the row rather than crossing the bank.
    No new owners, groups or labelled boundaries are introduced.
    """
    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.uuid: s for s in editor.document.symbols}
    source = {r: schematic["root_ref"] + "." + s.zener_path for r, s in symbols.items()}
    props = {r: component_properties(schematic, path) for r, path in source.items()}
    groups, _ = component_layout_groups(schematic)
    nets = expected_pin_nets(schematic)
    roles = {"series", "gain-setting", "series-termination", "source-impedance",
             "current-limit", "ac-coupling"}
    eligible = {r for r, s in symbols.items()
                if props[r].get("role") in roles and props[r].get("owner") in symbols
                and props[r].get("pin")
                and len(symbol_library_pins(editor.document, raw[s.id])) == 2}
    children: dict[str, list[str]] = defaultdict(list)
    for ref in eligible:
        owner = props[ref]["owner"]
        if owner in eligible and props[ref].get("group") == props[owner].get("group"):
            children[owner].append(ref)
    roots = sorted(eligible - {r for members in children.values() for r in members})
    gap = 5_080_000
    bank_ids = {}
    for root in roots:
        chain = [root]
        while len(children[chain[-1]]) == 1:
            child = children[chain[-1]][0]
            if child in chain:
                raise KiCadSchematicError(f"cyclic passive run at {child}")
            chain.append(child)
        if len(chain) <= 2 or children[chain[-1]]:
            continue
        # Other dependent branches must move as a unit with their parent;
        # leave those networks to their branch composer rather than tear them.
        if any(p.get("owner") in chain and ref not in chain for ref, p in props.items()):
            continue
        owner = owner_symbol_for_pin(schematic, editor, props[root]["owner"], props[root]["pin"])
        if owner is None:
            continue
        number = symbol_pin_number_groups(schematic["instances"][source[owner.reference]])[
            props[root]["pin"]][0]
        owner_raw = raw[owner.id]
        side = placed_pin_sides(editor.document, owner_raw)[number]
        if side not in {"left", "right"}:
            continue
        anchor = placed_pin_positions(editor.document, owner_raw)[number]
        direction = -1 if side == "left" else 1
        plans = []
        spans = []
        caption_widths = []
        height = 0
        for index, ref in enumerate(chain):
            symbol = symbols[ref]
            parent = props[ref]["owner"]
            pin = symbol_pin_number_groups(schematic["instances"][source[parent]])[
                props[ref]["pin"]][0]
            attached = nets[parent, pin]
            terminal = next(n for n in placed_pin_positions(editor.document, raw[symbol.id])
                            if nets[ref, n] == attached)
            row_direction = direction if index % 2 == 0 else -direction
            facing = "right" if row_direction < 0 else "left"
            rotation = next(a for a in (0, 90, 180, 270) if placed_pin_sides(
                editor.document, replace(raw[symbol.id], rotation=a))[terminal] == facing)
            original = raw[symbol.id]
            points = list(placed_pin_positions(editor.document, replace(
                original, rotation=rotation)).values())
            span = max(p.x for p in points) - min(p.x for p in points)
            caption_widths.append(sum(text_width(f.text) + f.text.attributes.size.x
                                      for f in symbol.fields
                                      if f.visible and f.name in {"Reference", "Value"}))
            spans.append(span)
            height = max(height, 2 * max((f.text.attributes.size.y for f in symbol.fields
                                         if f.visible), default=1_270_000))
            plans.append((ref, rotation))
        span = max(spans)
        caption_width = max(caption_widths)
        row_pitch = max(gap, height + 1_270_000)
        occupied = [envelope_from_points(placed_symbol_body_positions(editor.document, s))
                    for s in raw.values() if s.path is not None
                    and s.reference not in chain
                    and groups[schematic["root_ref"] + "." + s.path] == groups[source[root]]]
        for shift in range(64):
            proposed = []
            for index, (ref, rotation) in enumerate(plans):
                # Keep the whole title column outside the alternating turns.
                distance = gap + span // 2 + (caption_width + gap if direction < 0 else 0)
                point = Vector2(anchor.x + direction * distance,
                                anchor.y + (index + shift) * row_pitch)
                original = raw[symbols[ref].id]
                turned = replace(original, rotation=rotation)
                pins = list(placed_pin_positions(editor.document, turned).values())
                center = Vector2(round(sum(p.x for p in pins) / 2),
                                 round(sum(p.y for p in pins) / 2))
                delta = Vector2(point.x - center.x, point.y - center.y)
                box = Envelope(point.x - span // 2 - gap, point.y - height // 2,
                                point.x + span // 2 + gap + caption_width,
                                point.y + height // 2)
                proposed.append((ref, rotation, delta, box))
            if any(not envelopes_do_not_overlap(box, other, gap // 2)
                   for _, _, _, box in proposed for other in occupied):
                continue
            for ref, rotation, delta, _ in proposed:
                s = symbols[ref]
                place_symbol(s, Vector2(s.position.x + delta.x, s.position.y + delta.y),
                                  rotation)
                editor.update_items(s)
                bank_ids[s.id] = (owner.id, "snake", root, side)
            raw = {s.uuid: s for s in editor.document.symbols}
            break
    return bank_ids


def compose_owned_series_trees(schematic: dict[str, Any], editor: FileSchematic) -> set[str]:
    """Compose cascaded authored series/shunt trees before placing peer channels.

    A channel is built in local coordinates from its electrical attachments,
    not repaired from independently scattered seed positions. Only authored
    ownership and roles establish membership. Simple single-node networks
    retain their existing placement; this composes their multi-stage form.
    """
    symbols = {s.reference: s for s in editor.get_symbols() if s.zener_path is not None}
    raw = {s.reference: s for s in editor.document.symbols if s.path is not None}
    source = {r: schematic["root_ref"] + "." + s.zener_path for r, s in symbols.items()}
    props = {r: component_properties(schematic, path) for r, path in source.items()}
    nets = expected_pin_nets(schematic)
    groups, _ = component_layout_groups(schematic)
    children: dict[tuple[str, str], list[str]] = defaultdict(list)
    inline_roles = {"series", "ac-coupling", "series-termination", "source-impedance",
                    "current-limit"}
    for ref, attributes in props.items():
        owner = attributes.get("owner")
        if owner in symbols and attributes.get("pin"):
            numbers = symbol_pin_number_groups(schematic["instances"][source[owner]])[
                attributes["pin"]]
            if len(numbers) == 1:
                children[owner, numbers[0]].append(ref)

    def members(owner: str, number: str) -> set[str] | None:
        direct = children[owner, number]
        if len({props[r].get("group") for r in direct}) > 1:
            return None
        if sum(props[r].get("role") in inline_roles for r in direct) > 1:
            return None
        found = set(direct)
        for ref in direct:
            role = props[ref].get("role")
            pins = placed_pin_positions(editor.document, raw[ref])
            if role not in inline_roles | {"shunt"} or len(pins) != 2:
                return None
            for pin in pins:
                branch = members(ref, pin)
                if branch is None or role == "shunt" and branch:
                    return None
                found.update(branch)
        return found

    candidates = {}
    for (owner, number), direct in list(children.items()):
        if not any(props[r].get("role") == "series" for r in direct):
            continue
        tree = members(owner, number)
        if (tree and any(props[r].get("role") == "shunt" for r in tree)
                and (sum(props[r].get("role") == "series" for r in tree) > 1
                     or sum(props[r].get("role") == "shunt" for r in tree) > 1)
                and placed_pin_sides(editor.document, raw[owner]).get(number)
                in {"left", "right"}):
            candidates[owner, number] = tree
    roots = {key: tree for key, tree in candidates.items()
             if not any(key[0] in other for other_key, other in candidates.items()
                        if other_key != key)}
    handled = set().union(*roots.values()) if roots else set()
    stub = round(PIN_STUB_MM * 1e6)
    plans: dict[
        tuple[str, str], list[tuple[str, dict[str, KiCadSymbol], Envelope]]
    ] = defaultdict(list)
    for (owner, number), tree in roots.items():
        side = placed_pin_sides(editor.document, raw[owner])[number]
        direction = -1 if side == "left" else 1
        plan: dict[str, KiCadSymbol] = {}

        def place(ref: str, terminal: str, point: Vector2, facing: str) -> None:
            angle = next(a for a in (0, 90, 180, 270) if placed_pin_sides(
                editor.document, replace(raw[ref], rotation=a))[terminal] == facing)
            item = replace(raw[ref], rotation=angle)
            pin = placed_pin_positions(editor.document, item)[terminal]
            plan[ref] = replace(item, position=(item.position[0] + (point.x - pin.x) / 1e6,
                                                item.position[1] + (point.y - pin.y) / 1e6))

        def compose(parent: str, pin: str, cursor: int) -> None:
            direct = children[parent, pin]
            attached = nets[parent, pin]
            shunts = sorted(r for r in direct if props[r]["role"] == "shunt")
            series = [r for r in direct if props[r]["role"] in inline_roles]
            for ref in shunts:
                terminal = next(n for n in placed_pin_positions(editor.document, raw[ref])
                                if nets[ref, n] == attached)
                cursor += 2 * stub
                place(ref, terminal, Vector2(direction * cursor, stub), "top")
                width = max((text_width(f.text) for f in symbols[ref].fields
                             if f.name in {"Reference", "Value"}), default=0)
                cursor += max(2 * stub, width + stub)
            for ref in series:
                pins = placed_pin_positions(editor.document, raw[ref])
                terminal = next(n for n in pins if nets[ref, n] == attached)
                other = next(n for n in pins if n != terminal)
                cursor += 2 * stub
                place(ref, terminal, Vector2(direction * cursor, 0),
                      "right" if direction == -1 else "left")
                far = placed_pin_positions(editor.document, plan[ref])[other]
                compose(ref, other, direction * far.x)

        compose(owner, number, 0)
        if set(plan) != tree:
            raise KiCadSchematicError(f"{owner}/{number}: incomplete authored channel composition")
        points = [p for item in plan.values()
                  for p in placed_pin_positions(editor.document, item).values()]
        box = envelope_from_points(tuple(points))
        # Reserve ordinary caption and local return space before arranging
        # peer channels. Captions are fitted without changing native fonts.
        box = Envelope(box.min_x - 2 * stub, -3 * stub, box.max_x + 2 * stub,
                        box.max_y + 4 * stub)
        plans[owner, side].append((number, plan, box))

    for (owner, side), channels in plans.items():
        pins = placed_pin_positions(editor.document, raw[owner])
        channels.sort(key=lambda c: pins[c[0]].y)
        total = sum(box.height for _, _, box in channels) + stub * (len(channels) - 1)
        cursor = (pins[channels[0][0]].y + pins[channels[-1][0]].y - total) // 2
        placed: dict[str, KiCadSymbol] = {}
        for number, plan, box in channels:
            delta = Vector2(pins[number].x, cursor - box.min_y)
            for ref, item in plan.items():
                placed[ref] = replace(item, position=(item.position[0] + delta.x / 1e6,
                                                     item.position[1] + delta.y / 1e6))
            cursor += box.height + stub
        obstacles = [envelope_from_points(placed_symbol_body_positions(editor.document, item))
                     for ref, item in raw.items() if ref not in handled
                     and groups[source[ref]] == groups[source[owner]]]
        support_banks: dict[tuple[str, str], list[Envelope]] = defaultdict(list)
        for ref, item in raw.items():
            attributes = props[ref]
            if (ref not in handled and attributes.get("role") in {"bypass", "shunt"}
                    and attributes.get("owner") == owner and attributes.get("group")):
                support_banks[attributes["owner"], attributes["group"]].append(
                    envelope_from_points(placed_symbol_body_positions(editor.document, item)))
        # A composed channel may not occupy the gaps inside another authored
        # support bank. Reserve that bank as a whole, including caption room.
        for boxes in support_banks.values():
            if len(boxes) > 1:
                bank = union_all(boxes)
                obstacles.append(Envelope(bank.min_x - stub, bank.min_y - 2 * stub,
                                           bank.max_x + stub, bank.max_y + 2 * stub))
        direction = -1 if side == "left" else 1
        for step in range(32):
            shift = Vector2(direction * step * stub, 0)
            if any(not envelopes_do_not_overlap(
                    envelope_from_points(placed_symbol_body_positions(editor.document, item))
                    .translated(shift), box, stub)
                   for item in placed.values() for box in obstacles):
                continue
            for ref, item in placed.items():
                place_symbol(symbols[ref], Vector2.from_xy_mm(
                    item.position[0] + shift.x / 1e6, item.position[1]), item.rotation)
            editor.update_items([symbols[ref] for ref in placed])
            break
        else:
            raise KiCadSchematicError(f"{owner}: no clear position for authored channels")
    return handled
