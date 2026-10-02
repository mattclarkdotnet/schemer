from __future__ import annotations

import ast

import pytest

from tests.paths import REPO

SOURCE = REPO / "src" / "schemer"


@pytest.mark.parametrize("module", [
    "analysis/circuits", "kicad/geometry/library", "native/packing", "native/routing",
])
def test_shared_layout_services_do_not_depend_on_orchestrator(module):
    tree = ast.parse((SOURCE / f"{module}.py").read_text())
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "schemer.native.pipeline" not in imports
    assert "schemer.workflow.native_project" not in imports


def test_project_assembly_only_uses_public_layout_entrypoint():
    tree = ast.parse((SOURCE / "workflow/native_project.py").read_text())
    imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and node.module == "schemer.native.pipeline" for alias in node.names]
    assert imports == ["layout_kicad_from_zener"]


def test_placement_uses_public_routing_queries():
    tree = ast.parse((SOURCE / "native/pipeline.py").read_text())
    imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               and node.module == "schemer.native.routing" for alias in node.names]
    assert imports
    assert all(not name.startswith("_") for name in imports)
