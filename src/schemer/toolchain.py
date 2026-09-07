"""Discovery and evaluation for the local Zener schematic toolchain."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PCB_COMPILER = Path(os.environ.get(
    "SCHEMER_COMPILER", shutil.which("pcbc") or shutil.which("pcb") or "pcb",
))
DEFAULT_EXTENSION_ROOT = Path(
    os.environ.get("SCHEMER_EXTENSION_ROOT", str(Path.home() / ".vscode/extensions"))
)
DEFAULT_CHROME = Path(os.environ.get(
    "SCHEMER_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
))

REQUIRED_VIEWER_ASSETS = (
    "schematic_viewer.js",
    "schematic_viewer_bg.wasm",
    "worker.js",
    "worker_bg.wasm",
)


class ToolchainError(RuntimeError):
    """A local compiler or viewer dependency is missing or unusable."""


@dataclass(frozen=True)
class Toolchain:
    """Resolved local dependencies needed to evaluate and render Zener."""

    compiler: Path
    extension: Path
    chrome: Path

    @property
    def viewer_assets(self) -> Path:
        return self.extension / "wasm"


def _version_key(path: Path) -> tuple[int, ...]:
    match = re.fullmatch(r"diode-inc\.zener-(\d+(?:\.\d+)*)", path.name)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _has_viewer_assets(extension: Path) -> bool:
    wasm_dir = extension / "wasm"
    return all((wasm_dir / asset).is_file() for asset in REQUIRED_VIEWER_ASSETS)


def find_zener_extension(extension_root: Path = DEFAULT_EXTENSION_ROOT) -> Path:
    """Return the newest installed Zener extension with schematic viewer assets."""

    candidates = [
        candidate
        for candidate in extension_root.glob("diode-inc.zener-*")
        if candidate.is_dir() and _version_key(candidate) and _has_viewer_assets(candidate)
    ]
    if not candidates:
        raise ToolchainError(f"No usable diode-inc.zener extension found under {extension_root}")
    return max(candidates, key=_version_key)


def resolve_toolchain(
    *,
    compiler: Path = DEFAULT_PCB_COMPILER,
    extension: Path | None = None,
    chrome: Path = DEFAULT_CHROME,
) -> Toolchain:
    """Validate and return the selected local compiler, viewer, and browser."""

    compiler = compiler.expanduser().resolve()
    chrome = chrome.expanduser().resolve()
    extension = find_zener_extension() if extension is None else extension.expanduser().resolve()

    if not compiler.is_file():
        raise ToolchainError(f"pcb compiler not found: {compiler}")
    if not chrome.is_file():
        raise ToolchainError(f"Chrome executable not found: {chrome}")
    if not extension.is_dir() or not _has_viewer_assets(extension):
        raise ToolchainError(f"Zener extension viewer assets not found: {extension}")

    return Toolchain(compiler=compiler, extension=extension, chrome=chrome)


def evaluate_zener(entrypoint: Path, compiler: Path) -> dict[str, Any]:
    """Evaluate a Zener entrypoint and return the compiler's schematic JSON."""

    entrypoint = entrypoint.expanduser().resolve()
    if not entrypoint.is_file():
        raise ToolchainError(f"Zener entrypoint not found: {entrypoint}")

    process = subprocess.run(
        [str(compiler), "build", str(entrypoint), "--netlist"],
        cwd=entrypoint.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise ToolchainError(
            f"pcb evaluation failed with exit code {process.returncode}:\n{detail}"
        )

    try:
        schematic = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise ToolchainError(f"pcb returned invalid netlist JSON: {error}") from error

    if not isinstance(schematic, dict):
        raise ToolchainError("pcb netlist JSON was not an object")
    for key in ("instances", "nets", "root_ref", "symbols"):
        if key not in schematic:
            raise ToolchainError(f"pcb netlist JSON is missing {key!r}")
    return schematic


def viewer_evaluation(schematic: dict[str, Any]) -> dict[str, Any]:
    """Wrap a compiler netlist in the payload expected by the VS Code viewer."""

    return {
        "success": True,
        "schematic": schematic,
        "diagnostics": [],
    }


def connectivity_digest(schematic: dict[str, Any]) -> str:
    """Hash semantic topology without placement or compiler-local net IDs."""

    root_ref = schematic.get("root_ref")

    def normalize_instance_ref(value: Any) -> Any:
        if not isinstance(value, str) or not isinstance(root_ref, str):
            return value
        if value == root_ref:
            return "<root>"
        if value.startswith(root_ref + "."):
            return "<root>." + value.removeprefix(root_ref + ".")
        return value

    instances: dict[str, Any] = {}
    raw_instances = schematic.get("instances", {})
    if isinstance(raw_instances, dict):
        for ref, instance in raw_instances.items():
            normalized_ref = normalize_instance_ref(ref)
            if not isinstance(instance, dict):
                instances[normalized_ref] = instance
                continue
            children = instance.get("children")
            normalized_children = (
                {name: normalize_instance_ref(child_ref) for name, child_ref in children.items()}
                if isinstance(children, dict)
                else children
            )
            type_ref = instance.get("type_ref")
            normalized_type_ref = (
                {"module_name": type_ref.get("module_name")}
                if isinstance(type_ref, dict)
                else type_ref
            )
            instances[normalized_ref] = {
                "children": normalized_children,
                "kind": instance.get("kind"),
                "reference_designator": instance.get("reference_designator"),
                "type_ref": normalized_type_ref,
            }

    nets: dict[str, Any] = {}
    raw_nets = schematic.get("nets", {})
    if isinstance(raw_nets, dict):
        for ref, net in raw_nets.items():
            if not isinstance(net, dict):
                nets[ref] = net
                continue
            ports = net.get("ports", [])
            nets[ref] = {
                "kind": net.get("kind"),
                "name": net.get("name"),
                "ports": (
                    sorted(normalize_instance_ref(port) for port in ports)
                    if isinstance(ports, list)
                    else ports
                ),
            }

    normalized = {
        "root_ref": normalize_instance_ref(root_ref),
        "instances": instances,
        "nets": nets,
    }

    canonical = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def inspect_schematic(schematic: dict[str, Any]) -> dict[str, Any]:
    """Return stable structural facts suitable for a toolchain smoke report."""

    instances = schematic["instances"]
    root_ref = schematic["root_ref"]
    root = instances.get(root_ref, {}) if isinstance(instances, dict) else {}
    children = root.get("children", {}) if isinstance(root, dict) else {}
    positions = root.get("symbol_positions", {}) if isinstance(root, dict) else {}
    physical_components = sum(
        1
        for instance in instances.values()
        if isinstance(instance, dict) and instance.get("reference_designator")
    )
    return {
        "root_ref": root_ref,
        "instance_count": len(instances),
        "physical_component_count": physical_components,
        "net_count": len(schematic["nets"]),
        "root_children": sorted(children),
        "root_position_count": len(positions),
        "connectivity_digest": connectivity_digest(schematic),
    }
