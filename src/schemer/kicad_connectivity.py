"""Compare KiCad's independently exported connectivity with Zener intent."""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from schemer.kicad_api import FileSchematic
from schemer.kicad_schematic import KiCadSchematicError
from schemer.symbol_geometry import symbol_pin_number_groups
from schemer.view_policy import electrical_view

Pin = tuple[str, str]
DEFAULT_KICAD_CLI = Path(
    shutil.which("kicad-cli") or "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"
)


def verify_native_connectivity(
    schematic: dict[str, Any], editor: FileSchematic, executable: Path = DEFAULT_KICAD_CLI,
) -> int:
    """Ask KiCad to interpret the completed drawing before publishing it."""

    if not executable.is_file():
        raise KiCadSchematicError(f"KiCad CLI required for connectivity verification: {executable}")
    with TemporaryDirectory(prefix="schemer-netlist-") as directory:
        source = Path(directory) / "drawing.kicad_sch"
        output = Path(directory) / "drawing.net"
        editor.save_as(source)
        process = subprocess.run(
            [str(executable), "sch", "export", "netlist", "--format", "kicadxml",
             "--output", str(output), str(source)],
            capture_output=True, text=True, check=False,
        )
        if process.returncode or not output.is_file():
            raise KiCadSchematicError("KiCad netlist export failed: " + process.stderr.strip())
        return validate_exported_netlist(schematic, output.read_text())


def expected_pin_nets(schematic: dict[str, Any]) -> dict[Pin, str]:
    schematic = electrical_view(schematic)
    instances = schematic["instances"]
    components = sorted(
        (ref for ref, instance in instances.items() if instance.get("reference_designator")),
        key=len,
        reverse=True,
    )
    numbers = {ref: symbol_pin_number_groups(instances[ref]) for ref in components}
    result: dict[Pin, str] = {}
    for net in schematic["nets"].values():
        for port in net.get("ports", []):
            ref = next((ref for ref in components if port.startswith(ref + ".")), None)
            if ref is None:
                continue
            terminal = port[len(ref) + 1:].split(".", 1)[0]
            pins = numbers[ref].get(terminal, (terminal.removeprefix("Pin_"),))
            for pin in pins:
                identity = (instances[ref]["reference_designator"], pin)
                # Each unused pin is its own isolated net, even when the
                # compiler represents multiple NC ports with one sentinel.
                result[identity] = (
                    f"NC:{identity}" if net.get("kind") == "NotConnected" else net["name"]
                )
    return result


def validate_exported_netlist(schematic: dict[str, Any], xml: str) -> int:
    """Reject opens, shorts, or missing pins, independent of display net names."""

    expected = expected_pin_nets(schematic)
    actual: dict[Pin, str] = {}
    for net in ET.fromstring(xml).findall("./nets/net"):
        for node in net.findall("node"):
            pin = (node.attrib["ref"], node.attrib["pin"])
            if pin in expected:
                actual[pin] = net.attrib["code"]
    missing = set(expected) - set(actual)
    if missing:
        raise KiCadSchematicError(f"KiCad netlist is missing pins: {sorted(missing)}")
    outputs_by_input: dict[str, set[str]] = defaultdict(set)
    inputs_by_output: dict[str, set[str]] = defaultdict(set)
    for pin, net in expected.items():
        outputs_by_input[net].add(actual[pin])
        inputs_by_output[actual[pin]].add(net)
    opens = sorted(net for net, outputs in outputs_by_input.items() if len(outputs) > 1)
    shorts = sorted(sorted(inputs) for inputs in inputs_by_output.values() if len(inputs) > 1)
    if opens or shorts:
        raise KiCadSchematicError(f"KiCad connectivity mismatch: opens={opens}; shorts={shorts}")
    return len(expected)
