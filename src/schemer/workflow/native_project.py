from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from schemer.analysis.circuits import component_layout_groups
from schemer.analysis.hierarchy import PlannedSheet, plan_sheets
from schemer.analysis.inventory import inventory_markdown, structural_inventory
from schemer.analysis.visibility import electrical_view, shortest_unique_net_names
from schemer.core.diagnostics import capture_layout_issues
from schemer.core.errors import KiCadSchematicError
from schemer.core.layout import LayoutPlan, ModuleLayout
from schemer.integration.kicad_cli import DEFAULT_KICAD_CLI, verify_native_project_connectivity
from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.content import content_envelope
from schemer.kicad.geometry.envelopes import Envelope, union_all
from schemer.kicad.items import Vector2, translate_item
from schemer.kicad.syntax import Edit, apply_edits
from schemer.native.association import associate_components
from schemer.native.packing import DRAWING_MARGIN_MM, packed_group_deltas, standard_page_for_bounds
from schemer.native.pipeline import layout_kicad_from_zener
from schemer.placement.pipeline import generate_functional_ic_blocks

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class NativeProjectReport:
    root_file: Path
    checked_pins: int | None
    sheets: tuple[dict[str, Any], ...]
    draft: bool = False
    connectivity_error: str | None = None


def sheet_view(schematic: dict[str, Any], sheet: PlannedSheet) -> dict[str, Any]:
    """Keep original identities and authored metadata, selecting physical members.

    Ancestors remain available for role lookup. Net membership contains only
    physical terminals on this sheet; module aliases do not create endpoints.
    """
    result = deepcopy(schematic)
    parts = set(sheet.component_refs)
    keep = {schematic["root_ref"]}
    for ref in schematic["instances"]:
        if any(ref == part or ref.startswith(part + ".") or part.startswith(ref + ".")
               for part in parts):
            keep.add(ref)
    result["instances"] = {ref: item for ref, item in result["instances"].items() if ref in keep}
    for item in result["instances"].values():
        item["symbol_positions"] = {}
        if "children" in item:
            item["children"] = {key: ref for key, ref in item["children"].items() if ref in keep}
    result["nets"] = {}
    for key, net in schematic["nets"].items():
        ports = [port for port in net.get("ports", ())
                 if any(port.startswith(part + ".") for part in parts)]
        if ports:
            result["nets"][key] = {**deepcopy(net), "ports": ports}
    return result


def _seed_sheet(view: dict[str, Any], entrypoint: Path) -> dict[str, Any]:
    """Place each authored circuit independently, using the routing partition.

    Resolve membership before geometry. A whole-sheet connectivity placement
    followed by selective reseeding lets unrelated neighbours determine the
    internal spacing of a circuit that will later be wired independently.
    """
    module = ModuleLayout(view["root_ref"], entrypoint, {})
    groups, _ = component_layout_groups(view)
    positions = {}
    x = 0.0
    net_copies = {}
    for group in sorted(set(groups.values())):
        refs = tuple(ref for ref, name in groups.items() if name == group)
        local = sheet_view(view, PlannedSheet(view["root_ref"], group, None, None, refs, (), ()))
        composition = generate_functional_ic_blocks(local, LayoutPlan((module,)))
        if composition.sheet_hints:
            raise KiCadSchematicError("native hierarchy layout does not accept spatial sheet hints")
        block = composition.module_blocks[0][1]
        for key, point in composition.plan.modules[0].positions.items():
            if key.startswith("sym:"):
                name = key.rsplit("#", 1)[0]
                index = net_copies.get(name, 0)
                net_copies[name] = index + 1
                key = f"{name}#{index}"
            positions[key] = replace(point, x=point.x + x)
        # Temporary separation only; native layout packs completed drawings.
        x += block.root.width + 200
    return LayoutPlan((replace(module, positions=positions),)).apply_to_schematic(view)


def _seed_document(seed: FileSchematic, sheet: PlannedSheet, root_ref: str) -> FileSchematic:
    source = seed.get_as_string()
    library = seed.document.root.first_list("lib_symbols")
    if library is None:
        raise KiCadSchematicError("native seed lacks embedded symbol libraries")
    paths = {ref.removeprefix(root_ref + ".") for ref in sheet.component_refs}
    symbols = [source[item.expression.start:item.expression.end]
               for item in seed.document.symbols if item.path is None or item.path in paths]
    return FileSchematic.from_text(
        f'(kicad_sch (version {seed.version}) (generator "schemer") '
        f'(uuid "{uuid4()}") (paper "A4")\n'
        + source[library.start:library.end] + "\n" + "\n".join(symbols) + "\n)"
    )


def _instance_paths(editor: FileSchematic, project: str, sheet_path: str,
                    page: int, title: str) -> FileSchematic:
    """Rebase native instances, not Zener Path fields or physical references."""
    source = editor.get_as_string()
    edits = []
    for index, symbol in enumerate(editor.document.symbols):
        reference = symbol.reference
        if symbol.path is None:
            reference = f"#PWR{page:03d}{index:04d}"
            field = symbol.field("Reference")
            if field:
                value = field.expression.children[2]
                edits.append(Edit(value.start, value.end, json.dumps(reference)))
            uuid = symbol.expression.first_list("uuid")
            edits.append(Edit(uuid.start, uuid.end, f'(uuid "{uuid4()}")'))
        instances = symbol.expression.first_list("instances")
        text = (f'(instances (project {json.dumps(project)} (path {json.dumps(sheet_path)} '
                f'(reference {json.dumps(reference)}) (unit {symbol.unit}))))')
        if instances is not None:
            edits.append(Edit(instances.start, instances.end, text))
        else:
            edits.append(Edit(symbol.expression.end - 1, symbol.expression.end - 1, text))
    edits.append(Edit(editor.document.root.end - 1, editor.document.root.end - 1,
                       f'\n(title_block (title {json.dumps(title)}))\n'))
    return FileSchematic.from_text(apply_edits(source, edits))


def _append(editor: FileSchematic, fragments: list[str]) -> FileSchematic:
    source = editor.get_as_string()
    at = editor.document.root.end - 1
    return FileSchematic.from_text(source[:at] + "\n" + "\n".join(fragments) + source[at:])


def _overview(editor: FileSchematic, sheets: tuple[PlannedSheet, ...],
              files: dict[str, str], ids: dict[str, str],
              root_id: str, project: str) -> FileSchematic:
    """Keep compact native sheet links; cross-sheet nets connect at the circuits."""
    if not sheets:
        # A single circuit sheet is already packed, centred and sized. There
        # is no navigation overview to compose, and no reason to reframe it.
        return _append(editor, ['(sheet_instances (path "/" (page "1")))'])
    boxes = {}
    dimensions = {}
    for sheet in sheets:
        body_width = max(25.4, len(sheet.name) * 1.27 + 5.08)
        height = 12.7
        dimensions[sheet.module_ref] = body_width, height
        boxes[sheet.module_ref] = Envelope(
            0, 0, round(body_width * 1_000_000), round(height * 1_000_000),
        )
    # The circuit on the root keeps its already-completed bounding box.
    root_box = content_envelope(editor) if editor.get_items() else None
    if root_box:
        boxes["root-circuit"] = Envelope(0, 0, root_box.width, root_box.height)
    if not boxes:
        return editor
    primary = next(iter(boxes))
    deltas = packed_group_deltas(boxes, primary)
    margin = Vector2.from_xy_mm(DRAWING_MARGIN_MM, DRAWING_MARGIN_MM)
    # Root electrical items are translated together, including their fields.
    if root_box:
        delta = deltas["root-circuit"]
        shift = Vector2(delta.x + margin.x - root_box.min_x,
                        delta.y + margin.y - root_box.min_y)
        items = list(editor.get_items())
        for item in items:
            translate_item(item, shift)
        editor.update_items(items)
    fragments = []
    for page, sheet in enumerate(sheets, 2):
        delta = deltas[sheet.module_ref]
        width, height = dimensions[sheet.module_ref]
        x = (delta.x + margin.x) / 1_000_000
        y = (delta.y + margin.y) / 1_000_000
        fragments.append(
            f'(sheet (at {x:g} {y:g}) (size {width:g} {height:g}) '
            f'(stroke (width 0) (type default)) (fill (color 0 0 0 0)) '
            f'(uuid "{ids[sheet.module_ref]}") '
            f'(property "Sheetname" {json.dumps(sheet.name)} (at {x+2.54:g} {y+6.35:g} 0) '
            '(effects (font (size 1.27 1.27)) (justify left))) '
            f'(property "Sheetfile" {json.dumps(files[sheet.module_ref])} '
            f'(at {x:g} {y+height+1.27:g} 0) '
            '(effects (font (size 1.27 1.27)) (justify left) hide)) '
            + f' (instances (project {json.dumps(project)} (path "/{root_id}" '
            f'(page "{page}")))))'
        )
    fragments.append('(sheet_instances (path "/" (page "1")))')
    result = _append(editor, fragments)
    packed = [box.translated(Vector2(deltas[key].x + margin.x, deltas[key].y + margin.y))
              for key, box in boxes.items()]
    result.set_page_settings(standard_page_for_bounds(union_all(packed)))
    return result


def layout_native_project(schematic: dict[str, Any], seed: FileSchematic,
                          entrypoint: Path, output: Path, *,
                          executable: Path = DEFAULT_KICAD_CLI,
                          draft: bool = False) -> NativeProjectReport:
    """Stage, check, then publish a new flat KiCad project.

    ``seed`` must be fresh compiler output from the same authored source.
    Existing projects are never overwritten; user-edit merging is out of scope.
    Explicit drafts retain layout defects and failed connectivity results for
    visual review. Normal exports require the complete connectivity check.
    """
    if output.exists():
        raise KiCadSchematicError(f"project destination already exists: {output}")
    visible = electrical_view(schematic)
    associate_components(visible, seed.document, allow_unexpected=True)
    plan = plan_sheets(visible)
    # Sheet membership is for navigation. Boundary signals connect through
    # global labels on the actual circuit; rails retain native power symbols.
    rails = frozenset(net["name"] for net in visible["nets"].values()
                      if net.get("kind") in {"Power", "Ground"})
    plan = replace(plan, sheets=tuple(
        replace(sheet, ports=tuple(port for port in sheet.ports if port.net not in rails))
        for sheet in plan.sheets
    ))
    captions = shortest_unique_net_names([net["name"] for net in visible["nets"].values()])
    display = {str(net["id"]): captions[net["name"]] for net in visible["nets"].values()}
    project = entrypoint.stem
    files = {sheet.module_ref: (f"{project}.kicad_sch" if index == 0
                               else f"{index+1:02d}-" + re.sub(r"[^\w-]+", "-", sheet.name)
                               + ".kicad_sch")
             for index, sheet in enumerate(plan.sheets)}
    ids = {sheet.module_ref: str(uuid4()) for sheet in plan.sheets}
    root_id = ids[plan.root_ref]
    reports = []
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".schemer-stage-", dir=output.parent) as temporary:
        stage = Path(temporary) / "project"
        stage.mkdir()
        root_editor = None
        for page, sheet in enumerate(plan.sheets, 1):
            _LOG.warning("Laying out sheet %d/%d: %s (%d parts)",
                         page, len(plan.sheets), sheet.name, len(sheet.component_refs))
            view = sheet_view(visible, sheet)
            inventory = structural_inventory(view)
            worklist = Path(files[sheet.module_ref]).stem + "-structure.md"
            (stage / worklist).write_text(inventory_markdown(inventory))
            editor = _seed_document(seed, sheet, plan.root_ref)
            boundary = frozenset(port.net for port in sheet.ports)
            if page == 1:
                boundary = frozenset(port.net for child in plan.sheets[1:] for port in child.ports)
            with capture_layout_issues(draft) as issues:
                if sheet.component_refs:
                    positioned = _seed_sheet(view, entrypoint)
                    report = layout_kicad_from_zener(
                        positioned, editor, net_display_names=display, boundary_nets=boundary,
                        global_nets=boundary | rails,
                        inventory=inventory,
                    )
                    details = asdict(report)
                else:
                    editor.remove_items_by_id([item.id for item in editor.get_items()])
                    details = {"structural_inventory": asdict(inventory)}
            details["structural_review"] = worklist
            details["issues"] = issues
            path = f"/{root_id}" + (f"/{ids[sheet.module_ref]}" if page != 1 else "")
            title = sheet.name
            editor = _instance_paths(editor, project, path, page,
                                      f"DRAFT - {title}" if draft else title)
            if page == 1:
                source = editor.get_as_string()
                uuid = editor.document.root.first_list("uuid")
                root_editor = FileSchematic.from_text(apply_edits(source, [
                    Edit(uuid.start, uuid.end, f'(uuid "{root_id}")'),
                ]))
            else:
                editor.save_as(stage / files[sheet.module_ref])
            reports.append({"name": sheet.name, "file": files[sheet.module_ref],
                            "ports": [port.net for port in sheet.ports],
                            "parts": len(sheet.component_refs), **details,
                            "page_size": editor.get_page_settings().page_size})
        assert root_editor is not None
        root_editor = _overview(
            root_editor, plan.sheets[1:], files, ids, root_id, project,
        )
        reports[0]["page_size"] = root_editor.get_page_settings().page_size
        root_file = stage / files[plan.root_ref]
        root_editor.save_as(root_file)
        connectivity_error = None
        try:
            checked = verify_native_project_connectivity(visible, root_file, executable)
        except KiCadSchematicError as error:
            if not draft:
                raise
            checked = None
            connectivity_error = str(error)
        (stage / "layout-report.json").write_text(json.dumps(
            {"draft": draft, "checked_pins": checked, "connectivity_error": connectivity_error,
             "sheets": reports}, indent=2) + "\n")
        stage.rename(output)
    return NativeProjectReport(output / files[plan.root_ref], checked, tuple(reports),
                               draft, connectivity_error)
