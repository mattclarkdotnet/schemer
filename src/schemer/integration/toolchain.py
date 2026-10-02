from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from schemer.analysis.topology import bind_component_pin_numbers
from schemer.core.errors import ToolchainError

DEFAULT_PCB_COMPILER = Path(os.environ.get(
    "SCHEMER_COMPILER", shutil.which("pcb") or shutil.which("pcbc")
    or str(Path.home() / ".local/bin/pcb"),
)).expanduser()
DEFAULT_EXTENSION_ROOT = Path(os.environ.get(
    "SCHEMER_EXTENSION_ROOT", str(Path.home() / ".vscode/extensions"),
)).expanduser()
DEFAULT_CHROME = Path(os.environ.get(
    "SCHEMER_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)).expanduser()
REQUIRED_VIEWER_ASSETS = (
    "schematic_viewer.js",
    "schematic_viewer_bg.wasm",
    "worker.js",
    "worker_bg.wasm",
)


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
    bind_component_pin_numbers(schematic)
    return schematic


def viewer_evaluation(schematic: dict[str, Any]) -> dict[str, Any]:
    """Wrap a compiler netlist in the payload expected by the VS Code viewer."""

    return {
        "success": True,
        "schematic": schematic,
        "diagnostics": [],
    }
