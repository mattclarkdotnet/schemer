from __future__ import annotations

import subprocess
import sys
import tomllib

import pytest

from schemer.cli import build_parser, main
from tests.paths import REPO

COMMANDS = (
    "prepare", "check-preparation", "plan-sheets", "inspect-kicad",
    "layout-kicad", "layout-project",
)


@pytest.mark.parametrize("command", ["layout", "render", "doctor"])
def test_removed_commands_are_rejected(command, capsys):
    with pytest.raises(SystemExit) as error:
        main([command, "source.zen"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("command", COMMANDS)
def test_supported_command_help(command, capsys):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([command, "--help"])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert f"schemer {command}" in help_text
    assert "--chrome" not in help_text
    assert "--extension" not in help_text


def test_native_cli_imports_without_browser_or_raster_dependencies():
    script = '''
import importlib.abc
import sys

class NoViewerDependencies(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'playwright', 'PIL'}:
            raise AssertionError(f'Legacy dependency imported: {fullname}')

sys.meta_path.insert(0, NoViewerDependencies())
from schemer.cli import main
main(['--help'])
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "layout-project" in result.stdout
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert not any(name.startswith(("playwright", "pillow"))
                   for name in project["dependencies"])
