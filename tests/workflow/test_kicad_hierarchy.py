from __future__ import annotations

import pytest

from schemer.core.errors import KiCadSchematicError
from schemer.integration.kicad_cli import DEFAULT_KICAD_CLI, verify_native_project_connectivity
from tests.support.hierarchy import _intent, _project


@pytest.mark.skipif(not DEFAULT_KICAD_CLI.is_file(), reason="requires KiCad CLI")
def test_complete_hierarchy_matches_source(tmp_path):
    assert verify_native_project_connectivity(_intent(), _project(tmp_path)) == 4


@pytest.mark.skipif(not DEFAULT_KICAD_CLI.is_file(), reason="requires KiCad CLI")
def test_identical_local_names_in_parent_and_child_do_not_join_nets(tmp_path):
    assert verify_native_project_connectivity(
        _intent(), _project(tmp_path, child_output="INPUT"),
    ) == 4


@pytest.mark.skipif(not DEFAULT_KICAD_CLI.is_file(), reason="requires KiCad CLI")
def test_child_port_mismatch_is_an_open_even_when_both_sheets_are_locally_connected(tmp_path):
    with pytest.raises(KiCadSchematicError, match="opens=.*LINK"):
        verify_native_project_connectivity(_intent(), _project(tmp_path, child_port="OTHER"))


@pytest.mark.skipif(not DEFAULT_KICAD_CLI.is_file(), reason="requires KiCad CLI")
def test_parent_sheet_short_is_detected_across_the_hierarchy(tmp_path):
    with pytest.raises(KiCadSchematicError, match="shorts=.*INPUT.*LINK"):
        verify_native_project_connectivity(_intent(), _project(tmp_path, short=True))
