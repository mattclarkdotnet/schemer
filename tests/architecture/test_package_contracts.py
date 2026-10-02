from __future__ import annotations

import ast
import importlib
import pkgutil
import shutil
import subprocess

import schemer
from tests.paths import REPO


def test_dependency_contracts():
    executable = shutil.which("lint-imports")
    assert executable, "Install development dependencies with uv sync"
    result = subprocess.run(
        [executable, "--no-cache"], cwd=REPO, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_package_module_imports_without_running_a_workflow():
    modules = pkgutil.walk_packages(schemer.__path__, prefix="schemer.")
    for module in modules:
        if not module.name.endswith(".__main__"):
            importlib.import_module(module.name)


def test_shared_services_have_public_names_and_no_self_imports():
    source = REPO / "src" / "schemer"
    for path in source.rglob("*.py"):
        module = ".".join(path.relative_to(source.parent).with_suffix("").parts)
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not (node.module or "").startswith(
                "schemer"
            ):
                continue
            assert node.module != module, f"{module}:{node.lineno} imports itself"
            private = [alias.name for alias in node.names if alias.name.startswith("_")]
            assert not private, f"{module}:{node.lineno} imports private services: {private}"
