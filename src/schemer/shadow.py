"""Materialize buildable, source-preserving proposal workspaces."""

from __future__ import annotations

import json
import shutil
import tomllib
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from schemer.toolchain import ToolchainError

_EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".pcb",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "docs",
    "layout",
    "models",
    "output",
    "review",
}
_EXCLUDED_SUFFIXES = {
    ".jpeg",
    ".jpg",
    ".kicad_pcb",
    ".kicad_prl",
    ".kicad_pro",
    ".pdf",
    ".png",
    ".step",
    ".stp",
    ".svg",
    ".wrl",
    ".zip",
}


@dataclass(frozen=True)
class ProposalShadow:
    """Paths and provenance for a buildable Schemer-owned proposal."""

    workspace: Path
    entrypoint: Path
    updated_sources: tuple[Path, ...]


def _read_manifest(path: Path) -> dict[str, object]:
    try:
        parsed = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ToolchainError(f"cannot read Zener manifest {path}: {error}") from error
    if not isinstance(parsed, dict):
        raise ToolchainError(f"Zener manifest was not a table: {path}")
    return parsed


def find_workspace_root(entrypoint: Path) -> Path:
    """Find the nearest ancestor manifest that declares a Zener workspace."""

    entrypoint = entrypoint.expanduser().resolve()
    for directory in (entrypoint.parent, *entrypoint.parents):
        manifest = directory / "pcb.toml"
        if manifest.is_file() and "workspace" in _read_manifest(manifest):
            return directory
    raise ToolchainError(f"no enclosing Zener [workspace] found for {entrypoint}")


def _find_package_root(source: Path, workspace: Path) -> Path:
    for directory in (source.parent, *source.parents):
        if directory == workspace:
            return workspace
        if (directory / "pcb.toml").is_file():
            return directory
    raise ToolchainError(f"source is not contained in a manifested package: {source}")


def _copy_source_tree(source: Path, destination: Path) -> None:
    """Copy compiler-relevant package sources without bulky physical assets."""

    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in _EXCLUDED_DIRECTORY_NAMES for part in relative.parts):
            continue
        if path.is_dir():
            continue
        if path.suffix.lower() in _EXCLUDED_SUFFIXES:
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def _local_dependency_roots(package: Path, workspace: Path) -> tuple[Path, ...]:
    manifest_path = package / "pcb.toml"
    if not manifest_path.is_file():
        return ()
    dependencies = _read_manifest(manifest_path).get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ToolchainError(f"manifest dependencies were not a table: {manifest_path}")

    local: list[Path] = []
    for dependency in dependencies:
        if not isinstance(dependency, str):
            continue
        candidate = (workspace / dependency).resolve()
        if not candidate.is_relative_to(workspace):
            raise ToolchainError(f"local dependency escapes the Zener workspace: {dependency!r}")
        if candidate.is_dir():
            local.append(candidate)
    return tuple(local)


def materialize_proposal_shadow(
    entrypoint: Path,
    updates: dict[Path, str],
    destination: Path,
    *,
    file_overrides: dict[Path, str] | None = None,
) -> ProposalShadow:
    """Create a minimal buildable workspace containing proposed source updates."""

    entrypoint = entrypoint.expanduser().resolve()
    workspace = find_workspace_root(entrypoint)
    destination = destination.expanduser().resolve()
    if destination == workspace or destination.is_relative_to(workspace):
        raise ToolchainError("proposal shadow must be outside the source Zener workspace")
    if workspace.is_relative_to(destination):
        raise ToolchainError("proposal shadow may not contain the source Zener workspace")

    normalized_updates: dict[Path, str] = {}
    for source, content in {**updates, **(file_overrides or {})}.items():
        source = source.expanduser().resolve()
        if not source.is_relative_to(workspace):
            raise ToolchainError(f"proposed source is outside the Zener workspace: {source}")
        normalized_updates[source] = content

    package_root = _find_package_root(entrypoint, workspace)
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(workspace / "pcb.toml", destination / "pcb.toml")

    pending = deque([package_root])
    copied: set[Path] = set()
    while pending:
        package = pending.popleft().resolve()
        if package in copied:
            continue
        if not package.is_relative_to(workspace):
            raise ToolchainError(f"package escapes the Zener workspace: {package}")
        relative_package = package.relative_to(workspace)
        _copy_source_tree(package, destination / relative_package)
        copied.add(package)
        pending.extend(_local_dependency_roots(package, workspace))

    updated_relatives: list[Path] = []
    for source, content in sorted(normalized_updates.items()):
        relative = source.relative_to(workspace)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        updated_relatives.append(relative)

    shadow_entrypoint = destination / entrypoint.relative_to(workspace)
    if not shadow_entrypoint.is_file():
        raise ToolchainError(f"proposal shadow entrypoint was not copied: {shadow_entrypoint}")

    metadata = {
        "source_entrypoint": str(entrypoint),
        "source_workspace": str(workspace),
        "shadow_entrypoint": str(shadow_entrypoint),
        "updated_sources": [str(relative) for relative in updated_relatives],
    }
    (destination / ".schemer-proposal.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )
    return ProposalShadow(
        workspace=destination,
        entrypoint=shadow_entrypoint,
        updated_sources=tuple(destination / relative for relative in updated_relatives),
    )
