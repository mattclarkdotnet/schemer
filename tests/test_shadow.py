from pathlib import Path

import pytest

from schemer.shadow import find_workspace_root, materialize_proposal_shadow
from schemer.toolchain import ToolchainError


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _fixture_workspace(root: Path) -> Path:
    _write(root / "pcb.toml", '[workspace]\npcb-version = "0.4"\n')
    board = root / "boards" / "demo"
    _write(
        board / "pcb.toml",
        '[board]\nname = "Demo"\npath = "Demo.zen"\n\n[dependencies]\n"packages/a" = "0.1.0"\n',
    )
    _write(board / "Demo.zen", 'Module("Support.zen")\n')
    _write(board / "Support.zen", "# support\n")
    _write(board / "layout" / "large.kicad_pcb", "layout output")

    _write(
        root / "packages" / "a" / "pcb.toml",
        '[dependencies]\n"packages/b" = "0.1.0"\n',
    )
    _write(root / "packages" / "a" / "A.zen", "# package a\n")
    _write(root / "packages" / "a" / "docs" / "large.pdf", "not a real PDF")
    _write(root / "packages" / "b" / "pcb.toml", "[dependencies]\n")
    _write(root / "packages" / "b" / "B.zen", "# package b\n")
    return board / "Demo.zen"


def test_materialized_shadow_preserves_tree_and_recursive_local_dependencies(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    entrypoint = _fixture_workspace(source_root)
    proposal_text = 'Module("Support.zen")\n\n# pcb:sch R1.R x=10 y=20 rot=0\n'

    shadow = materialize_proposal_shadow(
        entrypoint,
        {entrypoint: proposal_text},
        tmp_path / "proposal",
    )

    assert find_workspace_root(entrypoint) == source_root
    assert shadow.entrypoint == tmp_path / "proposal" / "boards" / "demo" / "Demo.zen"
    assert shadow.entrypoint.read_text() == proposal_text
    assert (shadow.workspace / "boards" / "demo" / "Support.zen").is_file()
    assert (shadow.workspace / "packages" / "a" / "A.zen").is_file()
    assert (shadow.workspace / "packages" / "b" / "B.zen").is_file()
    assert not (shadow.workspace / "boards" / "demo" / "layout").exists()
    assert not (shadow.workspace / "packages" / "a" / "docs").exists()
    assert entrypoint.read_text() == 'Module("Support.zen")\n'
    assert (shadow.workspace / ".schemer-proposal.json").is_file()


def test_shadow_refuses_to_write_inside_source_workspace(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    entrypoint = _fixture_workspace(source_root)

    with pytest.raises(ToolchainError, match="outside the source Zener workspace"):
        materialize_proposal_shadow(entrypoint, {}, source_root / "proposal")
