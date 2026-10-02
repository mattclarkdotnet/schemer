from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from schemer import cli
from schemer.analysis.topology import connectivity_digest
from schemer.core.errors import ToolchainError
from schemer.workflow import preparation


@pytest.fixture
def prepared(tmp_path):
    workspace = tmp_path / "source"
    workspace.mkdir()
    entry = workspace / "Circuit.zen"
    entry.write_text('"""Fixture circuit."""\n')
    schematic = {"root_ref": "root", "instances": {
        "root": {"kind": "Module"},
        "root.R": {"kind": "Module", "attributes": {"schematic_properties": {"Json": {
            "role": "shunt", "group": "input", "at": "INPUT",
        }}}},
        "root.R.R": {"kind": "Component", "reference_designator": "R1"},
    }, "nets": {}}
    review = {"version": 1, "workspace": "source", "entrypoint": "source/Circuit.zen",
              "baseline_connectivity": connectivity_digest(schematic),
              "prepared_source_digest": None, "unresolved": [], "components": [
                  {"path": "R.R", "reviewed": True, "intent": "Input shunt",
                   "symbol_review": "Fixture two-terminal resistor"},
              ]}
    path = tmp_path / "preparation-review.json"
    path.write_text(json.dumps(review))
    return path, entry, schematic, review


def save_review(prepared, review):
    prepared[0].write_text(json.dumps(review))


def test_review_must_be_complete_and_sealed(prepared):
    path, entry, schematic, _ = prepared
    with pytest.raises(ToolchainError, match="check-preparation"):
        preparation.validate_preparation(path, entry, schematic)
    preparation.validate_preparation(path, entry, schematic, seal=True)
    preparation.require_preparation(SimpleNamespace(
        entrypoint=entry, preparation_review=path), schematic)


@pytest.mark.parametrize("mutation, error", [
    (lambda r: r["components"][0].update(reviewed=False), "incomplete"),
    (lambda r: r["components"][0].update(intent=""), "incomplete"),
    (lambda r: r["components"][0].update(symbol_review=""), "incomplete"),
    (lambda r: r["components"].append(r["components"][0]), "inventory"),
    (lambda r: r.update(components=[]), "inventory"),
    (lambda r: r["components"][0].update(path=[]), "worklist"),
    (lambda r: r.update(unresolved=["Choose representation"]), "unresolved"),
    (lambda r: r.update(entrypoint=42), "paths"),
    (lambda r: r.update(entrypoint="source/Other.zen"), "another entrypoint"),
])
def test_rejects_incomplete_or_invalid_reviews(prepared, mutation, error):
    path, entry, schematic, review = prepared
    mutation(review)
    save_review(prepared, review)
    with pytest.raises(ToolchainError, match=error):
        preparation.validate_preparation(path, entry, schematic, seal=True)


def test_no_fake_roles_required_for_mechanical_or_service_items(prepared):
    path, entry, schematic, review = prepared
    schematic["instances"]["root.R"].pop("attributes")
    with pytest.raises(ToolchainError, match="missing semantic annotation"):
        preparation.validate_preparation(path, entry, schematic, seal=True)
    review["components"][0]["annotation_not_needed"] = "Mechanical item; no electrical role"
    save_review(prepared, review)
    preparation.validate_preparation(path, entry, schematic, seal=True)


def test_rejects_connectivity_changes_and_invalid_roles(prepared):
    path, entry, schematic, _ = prepared
    modified = deepcopy(schematic)
    modified["instances"]["root.R.R"]["reference_designator"] = "R2"
    with pytest.raises(ToolchainError, match="inventory or connectivity"):
        preparation.validate_preparation(path, entry, modified, seal=True)
    props = schematic["instances"]["root.R"]["attributes"]["schematic_properties"]["Json"]
    props["role"] = "made-up-role"
    with pytest.raises(ToolchainError, match="unsupported"):
        preparation.validate_preparation(path, entry, schematic, seal=True)


@pytest.mark.parametrize("filename", ["Circuit.zen", "part.kicad_sym", "pcb.toml"])
def test_source_and_symbol_changes_invalidate_review(prepared, filename):
    path, entry, schematic, _ = prepared
    preparation.validate_preparation(path, entry, schematic, seal=True)
    (entry.parent / filename).write_text("changed\n")
    with pytest.raises(ToolchainError, match="sources changed"):
        preparation.validate_preparation(path, entry, schematic)


def test_coordinate_only_iterations_reuse_preparation(prepared):
    path, entry, schematic, _ = prepared
    preparation.validate_preparation(path, entry, schematic, seal=True)
    entry.write_text(entry.read_text() + "# pcb:sch comp:R x=20 y=10\n")
    preparation.validate_preparation(path, entry, schematic)


def test_resolved_tool_managed_symbol_changes_invalidate_review(prepared):
    path, entry, schematic, _ = prepared
    preparation.validate_preparation(path, entry, schematic, seal=True)
    schematic["instances"]["root.R.R"]["attributes"] = {
        "__symbol_value": {"String": "different dependency symbol"},
    }
    with pytest.raises(ToolchainError, match="sources changed"):
        preparation.validate_preparation(path, entry, schematic)


def test_compiler_local_module_signature_does_not_stale_review(prepared):
    path, entry, schematic, _ = prepared
    preparation.validate_preparation(path, entry, schematic, seal=True)
    schematic["instances"]["root.R"]["attributes"]["__signature"] = {
        "Json": {"parameters": [{"value": {"Net": {"id": 812}}}]},
    }
    preparation.validate_preparation(path, entry, schematic)


def test_explicit_spatial_hints_are_blocked(prepared):
    path, entry, schematic, _ = prepared
    entry.write_text('# schemer:hint ' + json.dumps({
        "version": 1, "id": "placement", "kind": "right-of", "blocks": ["A", "B"],
        "reason": "Put block B to the right",
    }) + "\n")
    with pytest.raises(ToolchainError, match="spatial hints"):
        preparation.validate_preparation(path, entry, schematic, seal=True)


def test_prepare_creates_pending_inventory_without_layout(tmp_path, monkeypatch, prepared):
    source = tmp_path / "original.zen"
    source.write_text("original circuit\n")
    schematic = prepared[2]

    def copy_only(entrypoint, positions, destination):
        assert positions == {}
        destination.mkdir(parents=True)
        copied = destination / entrypoint.name
        copied.write_text(entrypoint.read_text())
        return SimpleNamespace(entrypoint=copied)

    monkeypatch.setattr(preparation, "materialize_proposal_shadow", copy_only)
    monkeypatch.setattr(preparation, "evaluate_zener", lambda *_: schematic)
    path = preparation.prepare_source(source, tmp_path / "run", Path("compiler"))
    review = json.loads(path.read_text())
    assert review["prepared_source_digest"] is None
    assert review["components"][0]["reviewed"] is False
    assert source.read_text() == "original circuit\n"
    assert not list(path.parent.rglob("*.kicad_sch"))
    with pytest.raises(ToolchainError, match="new or empty"):
        preparation.prepare_source(source, path.parent, Path("compiler"))


@pytest.mark.parametrize("args", [
    ["layout", "source.zen", "--proposal-dir", "out"],
    ["layout-kicad", "source.zen", "seed.kicad_sch", "--output", "out.kicad_sch"],
    ["layout-project", "source.zen", "seed.kicad_sch", "--output", "out"],
])
def test_all_layout_commands_stop_without_preparation(monkeypatch, capsys, args):
    import importlib

    for module in ("schemer.cli.commands", "schemer.cli.main", "schemer.workflow.proposal"):
        monkeypatch.setattr(importlib.import_module(module), "evaluate_zener", lambda *_: {})
    monkeypatch.setattr("schemer.workflow.proposal.resolve_toolchain",
                        lambda **_: SimpleNamespace(compiler="compiler"))
    assert cli.main(args) != 0
    assert "requires source preparation" in capsys.readouterr().err
