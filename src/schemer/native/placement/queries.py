from __future__ import annotations

import re
from typing import Any

from schemer.kicad.editor import FileSchematic
from schemer.kicad.geometry.envelopes import point_distance
from schemer.kicad.geometry.library import placed_pin_positions
from schemer.kicad.items import SchematicSymbolInstance
from schemer.native.routing_model import PlacedEndpoint
from schemer.symbols.library import symbol_pin_number_groups


def owner_symbol_for_pin(
    schematic: dict[str, Any], editor: FileSchematic, owner: str, pin: str,
) -> SchematicSymbolInstance | None:
    """Resolve the drawn unit that actually contains an authored owner pin."""
    raw = {s.uuid: s for s in editor.document.symbols}
    for symbol in sorted(editor.get_symbols(), key=lambda s: s.unit):
        if symbol.reference != owner or symbol.zener_path is None:
            continue
        ref = schematic["root_ref"] + "." + symbol.zener_path
        numbers = symbol_pin_number_groups(schematic["instances"][ref]).get(pin, ())
        if set(numbers) & placed_pin_positions(editor.document, raw[symbol.id]).keys():
            return symbol
    return None


def bypass_capacitance(instance: dict[str, Any]) -> float:
    """Order an authored bypass bank by its typed nominal capacitance.

    Do not parse captions or reference prefixes, and retain stable ordering
    for missing/unknown units rather than guessing from a part number.
    """
    value = instance.get("attributes", {}).get("capacitance", {})
    value = value.get("String") if isinstance(value, dict) else value
    if not isinstance(value, str):
        return float("inf")
    match = re.fullmatch(r"\s*([+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([fpnumµμ]?)F\s*",
                         value or "")
    if match is None:
        return float("inf")
    return float(match[1]) * {"f": 1e-15, "p": 1e-12, "n": 1e-9,
                             "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3, "": 1}[match[2]]


def local_link_priority(
    endpoints: list[PlacedEndpoint], targets: list[PlacedEndpoint],
) -> tuple[int, float]:
    if not targets and len(endpoints) == 2 and endpoints[0].group == endpoints[1].group:
        return (0, point_distance(endpoints[0].position, endpoints[1].position))
    return (1, 0)
