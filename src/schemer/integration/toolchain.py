from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from schemer.analysis.topology import bind_component_pin_numbers
from schemer.core.errors import ToolchainError

DEFAULT_PCB_COMPILER = Path(os.environ.get(
    "SCHEMER_COMPILER", shutil.which("pcb") or shutil.which("pcbc")
    or str(Path.home() / ".local/bin/pcb"),
)).expanduser()


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
