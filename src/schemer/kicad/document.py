from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.decode import (
    decode_junction,
    decode_label,
    decode_no_connect,
    decode_symbol,
    decode_wire,
)
from schemer.kicad.records import KiCadJunction, KiCadLabel, KiCadNoConnect, KiCadSymbol, KiCadWire
from schemer.kicad.syntax import (
    Edit,
    ListExpr,
    Parser,
    apply_edits,
    descendant,
    format_number,
    sexpr_atom,
)


@dataclass(frozen=True)
class KiCadSchematicDocument:
    source: str
    root: ListExpr
    version: str
    symbols: tuple[KiCadSymbol, ...]
    wires: tuple[KiCadWire, ...]
    labels: tuple[KiCadLabel, ...]
    junctions: tuple[KiCadJunction, ...]
    no_connects: tuple[KiCadNoConnect, ...]

    @classmethod
    def from_text(cls, source: str) -> KiCadSchematicDocument:
        root = Parser(source).parse()
        if root.tag != "kicad_sch":
            raise KiCadSchematicError(f"expected kicad_sch root, found {root.tag!r}")
        version = root.first_list("version")
        if version is None:
            raise KiCadSchematicError("kicad_sch document has no version")
        top_level = tuple(item for item in root.children[1:] if isinstance(item, ListExpr))
        return cls(
            source=source,
            root=root,
            version=sexpr_atom(version, 1, "version").value,
            symbols=tuple(decode_symbol(item) for item in top_level if item.tag == "symbol"),
            wires=tuple(decode_wire(item) for item in top_level if item.tag == "wire"),
            labels=tuple(
                decode_label(item)
                for item in top_level
                if item.tag in {"label", "global_label", "hierarchical_label"}
            ),
            junctions=tuple(decode_junction(item) for item in top_level if item.tag == "junction"),
            no_connects=tuple(
                decode_no_connect(item) for item in top_level if item.tag == "no_connect"
            ),
        )

    @classmethod
    def from_file(cls, path: Path) -> KiCadSchematicDocument:
        return cls.from_text(path.read_text())

    def symbols_by_path(self, path: str) -> tuple[KiCadSymbol, ...]:
        return tuple(symbol for symbol in self.symbols if symbol.path == path)

    def with_field_size(
        self,
        *,
        path: str,
        field_names: tuple[str, ...],
        size_mm: float,
    ) -> str:
        """Return source with selected fields resized on every unit at ``path``."""

        symbols = self.symbols_by_path(path)
        if not symbols:
            raise KiCadSchematicError(f"no symbol has Path {path!r}")
        edits: list[Edit] = []
        edited_fields: set[str] = set()
        replacement = format_number(size_mm)
        for symbol in symbols:
            for field_name in field_names:
                field = symbol.field(field_name)
                if field is None:
                    continue
                size = descendant(field.expression, ("effects", "font", "size"))
                if size is None:
                    raise KiCadSchematicError(
                        f"symbol {path!r} field {field_name!r} has no font size"
                    )
                x = sexpr_atom(size, 1, f"symbol {path} field {field_name} size")
                y = sexpr_atom(size, 2, f"symbol {path} field {field_name} size")
                edits.extend(
                    (Edit(x.start, x.end, replacement), Edit(y.start, y.end, replacement))
                )
                edited_fields.add(field_name)
        missing = set(field_names) - edited_fields
        if missing:
            raise KiCadSchematicError(
                f"symbol {path!r} is missing requested fields: {', '.join(sorted(missing))}"
            )
        return apply_edits(self.source, edits)

    def summary(self) -> dict[str, object]:
        component_symbols = tuple(symbol for symbol in self.symbols if symbol.path is not None)
        return {
            "version": self.version,
            "symbol_count": len(self.symbols),
            "component_symbol_count": len(component_symbols),
            "component_path_count": len({symbol.path for symbol in component_symbols}),
            "power_symbol_count": len(self.symbols) - len(component_symbols),
            "wire_count": len(self.wires),
            "label_count": len(self.labels),
            "junction_count": len(self.junctions),
            "no_connect_count": len(self.no_connects),
            "symbols": [
                {
                    "path": symbol.path,
                    "reference": symbol.reference,
                    "value": symbol.value,
                    "library_id": symbol.library_id,
                    "unit": symbol.unit,
                    "uuid": symbol.uuid,
                    "position": list(symbol.position),
                    "rotation": symbol.rotation,
                    "field_sizes": {
                        field.name: list(field.text_size) if field.text_size is not None else None
                        for field in symbol.fields
                    },
                }
                for symbol in self.symbols
            ],
        }
