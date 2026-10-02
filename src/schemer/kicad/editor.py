from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from uuid import uuid4

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.document import KiCadSchematicDocument
from schemer.kicad.item_codec import (
    junction_item,
    label_item,
    line_item,
    no_connect_item,
    serialize_item,
    symbol_item,
)
from schemer.kicad.item_edits import item_edits
from schemer.kicad.items import (
    BaseLabel,
    Commit,
    EditableItem,
    Junction,
    NoConnectMarker,
    PageSettings,
    SchematicItem,
    SchematicLine,
    SchematicSymbolInstance,
)
from schemer.kicad.records import (
    KiCadJunction,
    KiCadLabel,
    KiCadNoConnect,
    KiCadSymbol,
    KiCadWire,
)
from schemer.kicad.syntax import Atom, Edit, ListExpr, apply_edits, sexpr_atom


class FileSchematic:
    """Source-preserving file backend with KiCad 11-style editing methods."""

    def __init__(self, source: str, path: Path | None = None):
        self._source = source
        self._path = path
        self._open_commit: Commit | None = None
        self._commit_source: str | None = None
        self._parse()

    @classmethod
    def from_text(cls, source: str) -> FileSchematic:
        return cls(source)

    @classmethod
    def from_file(cls, path: Path) -> FileSchematic:
        resolved = path.expanduser().resolve()
        return cls(resolved.read_text(), resolved)

    @property
    def name(self) -> str:
        return self._path.name if self._path is not None else "<memory>.kicad_sch"

    @property
    def version(self) -> str:
        return self._document.version

    @property
    def document(self) -> KiCadSchematicDocument:
        """Current parsed file state for geometry operations in the file backend."""

        return self._document

    def get_as_string(self) -> str:
        return self._source

    def get_items(self) -> Sequence[EditableItem]:
        return (
            *self.get_symbols(),
            *self.get_lines(),
            *self.get_labels(),
            *self.get_junctions(),
            *self.get_no_connects(),
        )

    def get_items_by_id(self, ids: str | Sequence[str]) -> Sequence[EditableItem]:
        requested = [ids] if isinstance(ids, str) else list(ids)
        by_id = {item.id: item for item in self.get_items()}
        missing = set(requested) - set(by_id)
        if missing:
            raise KiCadSchematicError("unknown schematic item IDs: " + ", ".join(sorted(missing)))
        return [by_id[item_id] for item_id in requested]

    def get_symbols(self) -> Sequence[SchematicSymbolInstance]:
        return [symbol_item(symbol) for symbol in self._document.symbols]

    def get_lines(self) -> Sequence[SchematicLine]:
        return [line_item(wire) for wire in self._document.wires]

    def get_labels(self) -> Sequence[BaseLabel]:
        return [label_item(label) for label in self._document.labels]

    def get_junctions(self) -> Sequence[Junction]:
        return [junction_item(junction) for junction in self._document.junctions]

    def get_no_connects(self) -> Sequence[NoConnectMarker]:
        return [no_connect_item(marker) for marker in self._document.no_connects]

    def get_page_settings(self) -> PageSettings:
        paper = self._document.root.first_list("paper")
        if paper is None:
            raise KiCadSchematicError("schematic has no paper settings")
        page_size = sexpr_atom(paper, 1, "paper size").value
        orientation = "landscape"
        if len(paper.children) > 2:
            raw_orientation = paper.children[2]
            if not isinstance(raw_orientation, Atom):
                raise KiCadSchematicError("paper orientation is not an atom")
            orientation = raw_orientation.value
        return PageSettings(page_size, orientation)

    def set_page_settings(self, settings: PageSettings) -> None:
        if settings.orientation not in {"landscape", "portrait"}:
            raise KiCadSchematicError(f"unsupported paper orientation: {settings.orientation!r}")
        paper = self._document.root.first_list("paper")
        if paper is None:
            raise KiCadSchematicError("schematic has no paper settings")
        orientation = " portrait" if settings.orientation == "portrait" else ""
        replacement = f"(paper {json.dumps(settings.page_size)}{orientation})"
        self._source = apply_edits(
            self._source,
            [Edit(paper.start, paper.end, replacement)],
        )
        self._parse()

    def begin_commit(self) -> Commit:
        if self._open_commit is not None:
            raise KiCadSchematicError("a schematic commit is already open")
        commit = Commit(str(uuid4()))
        self._open_commit = commit
        self._commit_source = self._source
        return commit

    def push_commit(self, commit: Commit, message: str = "") -> None:
        del message
        self._require_commit(commit)
        self._open_commit = None
        self._commit_source = None

    def drop_commit(self, commit: Commit) -> None:
        self._require_commit(commit)
        assert self._commit_source is not None
        self._source = self._commit_source
        self._open_commit = None
        self._commit_source = None
        self._parse()

    def update_items(self, items: EditableItem | Sequence[EditableItem]) -> list[EditableItem]:
        requested = [items] if isinstance(items, SchematicItem) else list(items)
        if not requested:
            return []
        duplicate_ids = _duplicates(item.id for item in requested)
        if duplicate_ids:
            raise KiCadSchematicError(
                "duplicate schematic item IDs in update: " + ", ".join(sorted(duplicate_ids))
            )

        raw_by_id = self._raw_items_by_id()
        edits: list[Edit] = []
        for item in requested:
            raw = raw_by_id.get(item.id)
            if raw is None:
                raise KiCadSchematicError(f"cannot update unknown schematic item {item.id!r}")
            edits.extend(item_edits(raw, item))

        self._source = apply_edits(self._source, edits)
        self._parse()
        return list(self.get_items_by_id([item.id for item in requested]))

    def create_items(self, items: EditableItem | Iterable[EditableItem]) -> list[EditableItem]:
        requested = [items] if isinstance(items, SchematicItem) else list(items)
        if not requested:
            return []
        existing_ids = {item.id for item in self.get_items()}
        created_ids: list[str] = []
        expressions: list[str] = []
        for item in requested:
            item.id = item.id or str(uuid4())
            if item.id in existing_ids or item.id in created_ids:
                raise KiCadSchematicError(f"duplicate schematic item UUID: {item.id}")
            expressions.append(serialize_item(item))
            created_ids.append(item.id)
        insertion = "".join(f"\n\t{expression}" for expression in expressions)
        self._source = apply_edits(
            self._source,
            [Edit(self._document.root.end - 1, self._document.root.end - 1, insertion)],
        )
        self._parse()
        return list(self.get_items_by_id(created_ids))

    def clone_graphic_symbol(self, symbol_id: str, reference: str) -> SchematicSymbolInstance:
        """Reuse an embedded non-physical symbol without changing native defaults."""
        raw = next(s for s in self._document.symbols if s.uuid == symbol_id)
        if raw.path is not None:
            raise KiCadSchematicError("cannot clone a physical component as a graphic")
        if any(s.reference == reference for s in self._document.symbols):
            raise KiCadSchematicError(f"duplicate graphic reference: {reference}")
        edits = []
        new_id = str(uuid4())

        def visit(expression: ListExpr) -> None:
            replacement = None
            index = 1
            if expression.tag == "uuid":
                replacement = (new_id if expression is raw.expression.first_list("uuid")
                               else str(uuid4()))
            elif expression.tag == "reference":
                replacement = reference
            elif (expression.tag == "property"
                  and sexpr_atom(expression, 1, "property").value == "Reference"):
                replacement, index = reference, 2
            if replacement is not None:
                value = sexpr_atom(expression, index, expression.tag)
                edits.append(Edit(value.start - raw.expression.start,
                                   value.end - raw.expression.start, json.dumps(replacement)))
            for child in expression.children:
                if isinstance(child, ListExpr):
                    visit(child)

        visit(raw.expression)
        expression = apply_edits(self._source[raw.expression.start:raw.expression.end], edits)
        insertion = self._document.root.end - 1
        self._source = apply_edits(self._source, [Edit(insertion, insertion, "\n" + expression)])
        self._parse()
        return next(s for s in self.get_symbols() if s.id == new_id)

    def remove_items(self, items: EditableItem | Sequence[EditableItem]) -> None:
        requested = [items] if isinstance(items, SchematicItem) else list(items)
        self.remove_items_by_id([item.id for item in requested])

    def remove_items_by_id(self, ids: str | Sequence[str]) -> None:
        requested = [ids] if isinstance(ids, str) else list(ids)
        if not requested:
            return
        duplicate_ids = _duplicates(requested)
        if duplicate_ids:
            raise KiCadSchematicError(
                "duplicate schematic item IDs in removal: " + ", ".join(sorted(duplicate_ids))
            )
        raw_by_id = self._raw_items_by_id()
        missing = set(requested) - set(raw_by_id)
        if missing:
            raise KiCadSchematicError(
                "cannot remove unknown schematic item IDs: " + ", ".join(sorted(missing))
            )
        edits = [
            Edit(
                _top_level_item_start(self._source, raw_by_id[item_id].expression.start),
                raw_by_id[item_id].expression.end,
                "",
            )
            for item_id in requested
        ]
        self._source = apply_edits(self._source, edits)
        self._parse()

    def save(self) -> None:
        if self._path is None:
            raise KiCadSchematicError("an in-memory schematic has no file path; use save_as")
        if self._open_commit is not None:
            raise KiCadSchematicError("cannot save while a schematic commit is open")
        self._path.write_text(self._source)

    def save_as(
        self,
        filename: str | Path,
        overwrite: bool = False,
        include_project: bool = True,
    ) -> None:
        # The file backend has no live KiCad project to copy.  Retaining this
        # parameter keeps call sites compatible with EditorCommandsHandler.
        del include_project
        if self._open_commit is not None:
            raise KiCadSchematicError("cannot save while a schematic commit is open")
        destination = Path(filename).expanduser().resolve()
        if destination.exists() and not overwrite:
            raise KiCadSchematicError(f"schematic already exists: {destination}")
        destination.write_text(self._source)

    def revert(self) -> None:
        if self._path is None:
            raise KiCadSchematicError("an in-memory schematic cannot be reverted")
        if self._open_commit is not None:
            raise KiCadSchematicError("cannot revert while a schematic commit is open")
        self._source = self._path.read_text()
        self._parse()

    def _parse(self) -> None:
        self._document = KiCadSchematicDocument.from_text(self._source)
        ids = [item.id for item in self.get_items()]
        missing = [type(item).__name__ for item in self.get_items() if not item.id]
        if missing:
            raise KiCadSchematicError(
                "editable schematic items lack UUIDs: " + ", ".join(sorted(set(missing)))
            )
        duplicate_ids = _duplicates(ids)
        if duplicate_ids:
            raise KiCadSchematicError(
                "duplicate schematic item UUIDs: " + ", ".join(sorted(duplicate_ids))
            )

    def _raw_items_by_id(
        self,
    ) -> dict[str, KiCadSymbol | KiCadWire | KiCadLabel | KiCadJunction | KiCadNoConnect]:
        items = (
            *self._document.symbols,
            *self._document.wires,
            *self._document.labels,
            *self._document.junctions,
            *self._document.no_connects,
        )
        return {item.uuid: item for item in items}

    def _require_commit(self, commit: Commit) -> None:
        if self._open_commit != commit:
            raise KiCadSchematicError("schematic commit is not open")


def _duplicates(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def _top_level_item_start(source: str, expression_start: int) -> int:
    """Include a top-level item's indentation and leading newline in deletion."""

    newline = source.rfind("\n", 0, expression_start)
    line_start = newline + 1
    if source[line_start:expression_start].strip():
        return expression_start
    return newline if newline >= 0 else 0
