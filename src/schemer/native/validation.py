from __future__ import annotations

from dataclasses import replace

from schemer.core.diagnostics import record_draft_issue
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import (
    Envelope,
    envelope_from_points,
    envelopes_do_not_overlap,
    point_distance,
)
from schemer.kicad.geometry.library import (
    placed_pin_segments,
    placed_symbol_body_positions,
)
from schemer.kicad.geometry.text import label_envelope
from schemer.kicad.items import (
    LocalLabel,
    SchematicLine,
    Vector2,
)
from schemer.native.contacts import wire_hits_label, wires_intersect
from schemer.native.obstacles import component_body_obstacles
from schemer.native.routing_paths import segment_hits_box, wire_contact


def validate_fixed_geometry(
    editor: FileSchematic, groups: dict[str, str], *, wire_nets: dict[str, str] | None = None,
) -> None:
    """Check the completed drawing using the same envelopes as placement/routing."""
    bodies = component_body_obstacles(editor, {
        symbol.id: groups[symbol.id] for symbol in editor.get_symbols()
        if symbol.id in groups
    })
    labels = list(editor.get_labels())

    def problem(code, message, subjects):
        if not record_draft_issue(code, message, subjects):
            raise KiCadSchematicError(message)

    painted = [(symbol, envelope_from_points(points)) for symbol in editor.document.symbols
               if symbol.uuid in groups and (points := placed_symbol_body_positions(
                   editor.document, symbol, fallback_to_pins=False))]
    for index, (symbol, box) in enumerate(painted):
        for other, other_box in painted[:index]:
            if (groups[symbol.uuid] == groups[other.uuid]
                    and not envelopes_do_not_overlap(box, other_box,
                        250_000 if symbol.path is None or other.path is None else 0)):
                problem("symbol-body-overlap", "symbol graphics touch or overlap",
                        (symbol.reference, other.reference))

    for index, label in enumerate(labels):
        group = groups.get(label.id)
        box = envelope_from_points(label_envelope(label.text))
        if any(not envelopes_do_not_overlap(box, body, 0) for body in bodies.get(group, [])):
            problem("label-body-overlap", f"label crosses component body: {label.text.value}",
                    (label.text.value,))
        for other in labels[:index]:
            if groups.get(other.id) == group and not envelopes_do_not_overlap(
                    box, envelope_from_points(label_envelope(other.text)), 0):
                problem("label-label-overlap",
                        f"labels overlap: {label.text.value}, {other.text.value}",
                        (label.text.value, other.text.value))
    for wire in editor.get_lines():
        if any(segment_hits_box(wire.start, wire.end, body)
               for body in bodies.get(groups.get(wire.id), [])):
            problem("wire-body-overlap", "wire crosses component body", (wire.id,))
    if wire_nets:
        wires = list(editor.get_lines())
        for index, wire in enumerate(wires):
            for other in wires[:index]:
                if (groups.get(wire.id) == groups.get(other.id)
                        and wire.id in wire_nets and other.id in wire_nets
                        and wire_nets[wire.id] != wire_nets[other.id]
                        and wire_contact(wire.start, wire.end, other)):
                    problem("wire-clearance", "distinct-net wire strokes lack visible clearance",
                            (wire_nets[wire.id], wire_nets[other.id]))


def clear_final_label_wires(
    editor: FileSchematic, groups: dict[str, str] | None = None,
) -> None:
    """Fit text against actual routes before extending a dangling straight stub.

    Predicted routes cannot establish final annotation clearance. This last
    text pass may slide or rotate a plain label on its incident wires, but
    cannot change a junction, a component, or the electrical attachment.
    """

    groups = groups or {}
    bodies = [(groups.get(symbol.uuid), envelope_from_points(points))
              for symbol in editor.document.symbols
              if (points := placed_symbol_body_positions(editor.document, symbol))]
    bodies.extend((groups.get(symbol.uuid), envelope_from_points(segment))
                  for symbol in editor.document.symbols
                  for segment in placed_pin_segments(editor.document, symbol).values())
    for label in editor.get_labels():
        group = groups.get(label.id)
        local_bodies = [box for g, box in bodies if g == group]
        wires = [w for w in editor.get_lines() if groups.get(w.id) == group]
        box = envelope_from_points(label_envelope(label.text))
        if not any(wire_hits_label(w, label) for w in wires):
            continue
        incident = [w for w in wires if label.position in (w.start, w.end)]
        other_labels = [envelope_from_points(label_envelope(item.text))
                        for item in editor.get_labels()
                        if item.id != label.id and groups.get(item.id) == group]
        if isinstance(label, LocalLabel) and incident:
            # A label at a junction is movable even though the junction is
            # not. Fit on the existing connected segments without rerouting.
            points = [label.position]
            for wire in incident:
                points.extend((wire.start, wire.end, Vector2(
                    (wire.start.x + wire.end.x) // 2, (wire.start.y + wire.end.y) // 2)))
            candidates = []
            for point in points:
                if point != label.position and any(
                    segment_hits_box(w.start, w.end, Envelope(point.x, point.y, point.x, point.y))
                    for w in wires if w not in incident and point not in (w.start, w.end)
                ):
                    continue
                for rotation in (0, 90):
                    for alignment in ("left", "right"):
                        text = replace(label.text, position=point, attributes=replace(
                            label.text.attributes, angle=rotation, horizontal_alignment=alignment))
                        candidate = replace(label, position=point, text=text)
                        box = envelope_from_points(label_envelope(text))
                        if (any(wire_hits_label(w, candidate) for w in wires)
                                or any(not envelopes_do_not_overlap(box, b, 250_000)
                                       for b in [*local_bodies, *other_labels])):
                            continue
                        candidates.append((rotation != label.text.attributes.angle,
                            point_distance(point, label.position),
                            alignment != label.text.attributes.horizontal_alignment, candidate))
            if candidates:
                editor.update_items(min(candidates, key=lambda c: c[:3])[3])
                continue
        if len(incident) != 1:
            message = f"label crosses final wiring: {label.text.value}"
            if record_draft_issue("label-wire-overlap", message, (label.text.value,)):
                continue
            raise KiCadSchematicError(message)
        wire = incident[0]
        other = wire.end if wire.start == label.position else wire.start
        dx = (label.position.x > other.x) - (label.position.x < other.x)
        dy = (label.position.y > other.y) - (label.position.y < other.y)
        foreign = [w for w in wires if w.id != wire.id]
        for step in range(1, 33):
            point = Vector2(label.position.x + dx * step * 635_000,
                            label.position.y + dy * step * 635_000)
            candidate = replace(label.text, position=point)
            box = envelope_from_points(label_envelope(candidate))
            if (any(segment_hits_box(w.start, w.end, box) for w in foreign)
                    or any(not envelopes_do_not_overlap(box, b, 250_000)
                           for b in [*local_bodies, *other_labels])
                    or any(segment_hits_box(label.position, point, b)
                           for b in [*local_bodies, *other_labels])
                    or any(wires_intersect(SchematicLine(
                        id="", start=label.position, end=point), w) for w in foreign)):
                continue
            if wire.start == label.position:
                wire.start = point
            else:
                wire.end = point
            label.position = point
            label.text = candidate
            editor.update_items([wire, label])
            break
        else:
            message = f"no clear final label position: {label.text.value}"
            if not record_draft_issue("label-wire-overlap", message, (label.text.value,)):
                raise KiCadSchematicError(message)
