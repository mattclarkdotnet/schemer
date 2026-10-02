from __future__ import annotations

from math import sqrt

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.content import completed_group_envelopes, content_envelope
from schemer.kicad.geometry.envelopes import Envelope, envelopes_do_not_overlap, union_all
from schemer.kicad.items import PageSettings, Vector2, translate_item

# Clearance is inside KiCad's worksheet frame, not measured from the paper edge.
DRAWING_MARGIN_MM = 20.0
_STANDARD_PAGES = (
    ("A4", 297.0, 210.0), ("A3", 420.0, 297.0), ("A2", 594.0, 420.0),
    ("A1", 841.0, 594.0), ("A0", 1189.0, 841.0),
)
_BLOCK_CLEARANCE_MM = 20.32
_LANDSCAPE_RATIO = sqrt(2)


def pack_completed_groups(
    editor: FileSchematic,
    groups: dict[str, str],
    primary_group: str,
    *,
    connector_groups: frozenset[str] = frozenset(),
    right_of: tuple[tuple[str, str], ...] = (),
    stage_order: dict[str, tuple[str, ...]] | None = None,
) -> PageSettings:
    """Measure and pack frozen blocks after all local drawing is complete."""

    envelopes = completed_group_envelopes(editor, groups)

    packed_envelopes = dict(envelopes)
    inner_deltas = {}
    parents = {}
    for parent, children in (stage_order or {}).items():
        children = tuple(child for child in children if child in envelopes)
        if not children:
            continue
        gap = round(_BLOCK_CLEARANCE_MM * 1_000_000)
        inner = _pack_block_rows(envelopes, list(children),
                                 sum(envelopes[c].width + gap for c in children), gap)
        # Keep residual module-owned objects as their own measured block.
        if parent in packed_envelopes:
            continue
        packed_envelopes[parent] = union_all([envelopes[c].translated(inner[c]) for c in children])
        for child in children:
            del packed_envelopes[child]
            parents[child] = parent
            inner_deltas[child] = inner[child]
    outer = packed_group_deltas(
        packed_envelopes,
        parents.get(primary_group, primary_group),
        connector_groups=connector_groups,
        right_of=right_of,
    )
    deltas = {}
    for group in envelopes:
        delta = outer[parents.get(group, group)]
        inner = inner_deltas.get(group, Vector2(0, 0))
        deltas[group] = Vector2(delta.x + inner.x, delta.y + inner.y)
    packed = {
        group: envelope.translated(deltas.get(group, Vector2(0, 0)))
        for group, envelope in envelopes.items()
    }
    drawing = union_all(list(packed.values()))
    margin = Vector2.from_xy_mm(DRAWING_MARGIN_MM, DRAWING_MARGIN_MM)
    for name, width, height in _STANDARD_PAGES:
        usable = Envelope(margin.x, margin.y,
                           round((width - DRAWING_MARGIN_MM) * 1_000_000),
                           round((height - DRAWING_MARGIN_MM) * 1_000_000))
        if drawing.width > usable.width or drawing.height > usable.height:
            continue
        global_delta = Vector2(usable.center_x - drawing.center_x,
                               usable.center_y - drawing.center_y)
        # Reserve the default title block plus 8 mm clearance, not an
        # unnecessary blank strip across the entire bottom of the sheet.
        title = Envelope(round((width - 128) * 1_000_000),
                          round((height - 52) * 1_000_000),
                          round(width * 1_000_000), round(height * 1_000_000))
        if all(envelopes_do_not_overlap(box.translated(global_delta), title, 0)
               for box in packed.values()):
            page = PageSettings(name, "landscape")
            break
    else:
        raise KiCadSchematicError("native layout exceeds the usable A0 landscape area")
    final_deltas = {
        # Text bounds may introduce sub-micrometre offsets. Keep electrical
        # blocks on a common grid: independently rounded symbol-pin sums and
        # wire coordinates otherwise disagree inside KiCad's netlist reader.
        group: Vector2(round((delta.x + global_delta.x) / 1000) * 1000,
                       round((delta.y + global_delta.y) / 1000) * 1000)
        for group, delta in deltas.items()
    }
    updates = []
    for item in editor.get_items():
        group = groups.get(item.id)
        if group is None:
            continue
        delta = final_deltas[group]
        translate_item(item, delta)
        updates.append(item)
    editor.update_items(updates)
    return page


def packed_group_deltas(
    envelopes: dict[str, Envelope],
    primary_group: str,
    *,
    clearance_mm: float = _BLOCK_CLEARANCE_MM,
    connector_groups: frozenset[str] = frozenset(),
    right_of: tuple[tuple[str, str], ...] = (),
) -> dict[str, Vector2]:
    """Pack intact blocks into a standard landscape frame, connectors last."""

    clearance = Vector2.from_xy_mm(clearance_mm, clearance_mm).x
    mentioned = {group for pair in right_of for group in pair}
    connector_names = sorted(
        group for group in connector_groups
        if group in envelopes and group not in mentioned and group != primary_group
    )
    functional = {group: box for group, box in envelopes.items()
                  if group not in connector_names}

    def add_connector_rows(result: dict[str, Vector2], bounds: Envelope, width: int) -> None:
        if not connector_names:
            return
        bank = _pack_block_rows(envelopes, connector_names, width, clearance)
        bank_bounds = union_all([envelopes[g].translated(bank[g]) for g in connector_names])
        for group, delta in bank.items():
            result[group] = Vector2(delta.x + bounds.center_x - bank_bounds.center_x,
                                    delta.y + bounds.max_y + clearance)

    if right_of:
        result = _relative_group_deltas(functional, right_of, clearance)
        bounds = union_all([box.translated(result[group]) for group, box in functional.items()])
        if connector_names:
            width = max(bounds.width, max(envelopes[g].width for g in connector_names))
            add_connector_rows(result, bounds, width)
        return result

    # Tall circuit blocks share the first shelves. Shallow, wide banks then
    # occupy their own lower shelf rather than separating the tall circuits.
    order = [primary_group, *sorted(set(functional) - {primary_group},
                                   key=lambda g: (-functional[g].height, g))]
    minimum_width = max(box.width for box in envelopes.values())
    candidates = []
    # Keep the stable name order as another candidate: a shape heuristic
    # should not force a larger sheet on circuits with different proportions.
    for sequence in (order, [primary_group, *sorted(set(functional) - {primary_group})]):
        all_names = sequence + connector_names
        # Row composition changes only at sums of consecutive block widths.
        widths = {minimum_width}
        for start in range(len(all_names)):
            width = -clearance
            for group in all_names[start:]:
                width += envelopes[group].width + clearance
                widths.add(max(minimum_width, width))
        for width in sorted(widths):
            result = _pack_block_rows(envelopes, sequence, width, clearance)
            bounds = union_all([envelopes[g].translated(result[g]) for g in sequence])
            add_connector_rows(result, bounds, width)
            complete = union_all([box.translated(result[g]) for g, box in envelopes.items()])
            frame_width = max(complete.width, complete.height * _LANDSCAPE_RATIO)
            candidates.append((
                frame_width ** 2 / _LANDSCAPE_RATIO,
                abs(complete.width / complete.height - _LANDSCAPE_RATIO),
                width,
                result,
            ))
    return min(candidates, key=lambda item: item[:3])[3]


def _pack_block_rows(
    envelopes: dict[str, Envelope], order: list[str], width: int, gap: int,
) -> dict[str, Vector2]:
    """Place a stable sequence of measured blocks in rows without resizing."""

    x = y = row_height = 0
    result = {}
    for group in order:
        box = envelopes[group]
        if x and x + box.width > width:
            x = 0
            y += row_height + gap
            row_height = 0
        result[group] = Vector2(x - box.min_x, y - box.min_y)
        x += box.width + gap
        row_height = max(row_height, box.height)
    return result


def _relative_group_deltas(
    envelopes: dict[str, Envelope],
    right_of: tuple[tuple[str, str], ...],
    clearance: int,
) -> dict[str, Vector2]:
    """Pack functional groups from authored relative stages."""

    names = set(envelopes)
    mentioned = {name for pair in right_of for name in pair} & names
    if mentioned != names:
        raise KiCadSchematicError(
            "right-of relations do not cover all native functional groups: "
            + ", ".join(sorted(names - mentioned))
        )
    predecessors = {name: set() for name in names}
    for subject, predecessor in right_of:
        if subject in names and predecessor in names:
            predecessors[subject].add(predecessor)
    levels: dict[str, int] = {}
    while len(levels) < len(names):
        ready = sorted(
            name for name in names - levels.keys() if predecessors[name] <= levels.keys()
        )
        if not ready:
            raise KiCadSchematicError("right-of relations form a cycle")
        for name in ready:
            levels[name] = max(
                (levels[previous] + 1 for previous in predecessors[name]),
                default=0,
            )
    result: dict[str, Vector2] = {}
    cursor_x = 0
    for level in range(max(levels.values()) + 1):
        peers = sorted(name for name in names if levels[name] == level)
        total_height = sum(envelopes[name].height for name in peers)
        total_height += clearance * (len(peers) - 1)
        cursor_y = -round(total_height / 2)
        for name in peers:
            envelope = envelopes[name]
            result[name] = Vector2(cursor_x - envelope.min_x, cursor_y - envelope.min_y)
            cursor_y += envelope.height + clearance
        cursor_x += max(envelopes[name].width for name in peers) + clearance
    return result


def page_settings_for_content(editor: FileSchematic) -> PageSettings:
    """Choose the smallest standard landscape sheet containing the drawing."""

    return standard_page_for_bounds(content_envelope(editor))


def standard_page_for_bounds(envelope: Envelope) -> PageSettings:
    """Choose output framing after scale-independent layout is complete."""

    required_width = envelope.max_x + round(DRAWING_MARGIN_MM * 1_000_000)
    required_height = envelope.max_y + round(DRAWING_MARGIN_MM * 1_000_000)
    for name, width_mm, height_mm in _STANDARD_PAGES:
        size = Vector2.from_xy_mm(width_mm, height_mm)
        if required_width <= size.x and required_height <= size.y:
            return PageSettings(name, "landscape")
    raise KiCadSchematicError("native layout exceeds an A0 landscape sheet")
