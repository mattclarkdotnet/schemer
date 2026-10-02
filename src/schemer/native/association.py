from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from schemer.core.errors import KiCadSchematicError
from schemer.kicad.document import KiCadSchematicDocument
from schemer.kicad.records import KiCadSymbol


@dataclass(frozen=True)
class KiCadComponentAssociation:
    instance_ref: str
    path: str
    symbols: tuple[KiCadSymbol, ...]


def associate_components(
    schematic: dict[str, Any],
    document: KiCadSchematicDocument,
    *,
    allow_unexpected: bool = False,
) -> tuple[KiCadComponentAssociation, ...]:
    """Match every physical Zener component to KiCad units by its stable Path field."""

    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    if not isinstance(root_ref, str) or not isinstance(instances, dict):
        raise KiCadSchematicError("evaluated Zener schematic lacks root_ref or instances")

    zener_by_path: dict[str, str] = {}
    for instance_ref, instance in instances.items():
        if not isinstance(instance_ref, str) or not isinstance(instance, dict):
            continue
        if not instance.get("reference_designator"):
            continue
        prefix = root_ref + "."
        if not instance_ref.startswith(prefix):
            raise KiCadSchematicError(
                f"physical component {instance_ref!r} is outside root {root_ref!r}"
            )
        zener_by_path[instance_ref.removeprefix(prefix)] = instance_ref

    kicad_by_path: dict[str, list[KiCadSymbol]] = {}
    for symbol in document.symbols:
        if symbol.path is not None:
            kicad_by_path.setdefault(symbol.path, []).append(symbol)

    missing = sorted(set(zener_by_path) - set(kicad_by_path))
    unexpected = sorted(set(kicad_by_path) - set(zener_by_path))
    if missing or unexpected and not allow_unexpected:
        details = []
        if missing:
            details.append("missing KiCad paths: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected KiCad paths: " + ", ".join(unexpected))
        raise KiCadSchematicError("component identity mismatch; " + "; ".join(details))

    associations = []
    for path, instance_ref in sorted(zener_by_path.items()):
        symbols = tuple(kicad_by_path[path])
        references = {symbol.reference for symbol in symbols}
        if len(references) != 1:
            raise KiCadSchematicError(
                f"KiCad component path {path!r} has inconsistent references: "
                + ", ".join(sorted(references))
            )
        associations.append(KiCadComponentAssociation(instance_ref, path, symbols))
    return tuple(associations)
