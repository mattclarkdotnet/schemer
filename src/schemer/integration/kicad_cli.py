from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from schemer.analysis.connectivity import validate_exported_netlist
from schemer.core.errors import KiCadSchematicError
from schemer.kicad.editor import FileSchematic

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
        editor.save_as(source)
        return verify_native_project_connectivity(schematic, source, executable)


def verify_native_project_connectivity(
    schematic: dict[str, Any], root_file: Path, executable: Path = DEFAULT_KICAD_CLI,
) -> int:
    """Verify the complete hierarchy by exporting its root with KiCad.

    Per-sheet checks cannot detect a missing sheet pin or a short between two
    sheets. Keep the complete staged file tree in place during this export.
    """

    if not executable.is_file():
        raise KiCadSchematicError(f"KiCad CLI required for connectivity verification: {executable}")
    if not root_file.is_file():
        raise KiCadSchematicError(f"missing root schematic: {root_file}")
    with TemporaryDirectory(prefix="schemer-project-netlist-") as directory:
        output = Path(directory) / "drawing.net"
        process = subprocess.run(
            [str(executable), "sch", "export", "netlist", "--format", "kicadxml",
             "--output", str(output), str(root_file.resolve())],
            capture_output=True, text=True, check=False,
        )
        if process.returncode or not output.is_file():
            raise KiCadSchematicError("KiCad netlist export failed: " + process.stderr.strip())
        return validate_exported_netlist(schematic, output.read_text())
