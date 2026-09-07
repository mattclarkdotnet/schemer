from pathlib import Path

from schemer.toolchain import connectivity_digest, find_zener_extension, inspect_schematic


def _make_extension(root: Path, version: str) -> Path:
    extension = root / f"diode-inc.zener-{version}"
    wasm = extension / "wasm"
    wasm.mkdir(parents=True)
    for asset in (
        "schematic_viewer.js",
        "schematic_viewer_bg.wasm",
        "worker.js",
        "worker_bg.wasm",
    ):
        (wasm / asset).touch()
    return extension


def test_find_zener_extension_uses_numeric_version_order(tmp_path: Path) -> None:
    _make_extension(tmp_path, "2.1.9")
    expected = _make_extension(tmp_path, "2.1.41")

    assert find_zener_extension(tmp_path) == expected


def test_connectivity_digest_ignores_symbol_positions() -> None:
    base = {
        "instances": {
            "root": {
                "kind": "Module",
                "symbol_positions": {"comp:R1": {"x": 1, "y": 2, "rotation": 0}},
            }
        },
        "nets": {"N": {"ports": ["R1.1", "R2.1"]}},
    }
    moved = {
        "instances": {
            "root": {
                "kind": "Module",
                "symbol_positions": {"comp:R1": {"x": 100, "y": -50, "rotation": 90}},
            }
        },
        "nets": {"N": {"ports": ["R1.1", "R2.1"]}},
    }

    assert connectivity_digest(base) == connectivity_digest(moved)


def test_connectivity_digest_changes_with_connectivity() -> None:
    left = {"instances": {"root": {}}, "nets": {"N": {"ports": ["R1.1"]}}}
    right = {"instances": {"root": {}}, "nets": {"N": {"ports": ["R1.2"]}}}

    assert connectivity_digest(left) != connectivity_digest(right)


def test_connectivity_digest_ignores_compiler_local_net_ids() -> None:
    left = {
        "instances": {
            "root": {
                "attributes": {"__signature": {"Json": {"default_value": {"Net": {"id": 91}}}}}
            }
        },
        "nets": {"N": {"id": 91, "kind": "Signal", "name": "N", "ports": ["R1.1"]}},
    }
    right = {
        "instances": {
            "root": {
                "attributes": {"__signature": {"Json": {"default_value": {"Net": {"id": 347}}}}}
            }
        },
        "nets": {"N": {"id": 347, "kind": "Signal", "name": "N", "ports": ["R1.1"]}},
    }

    assert connectivity_digest(left) == connectivity_digest(right)


def test_connectivity_digest_ignores_source_relocation() -> None:
    def schematic(root: str, source: str) -> dict:
        return {
            "root_ref": root,
            "instances": {
                root: {
                    "children": {"R1": root + ".R1"},
                    "kind": "Module",
                    "type_ref": {"module_name": "<root>", "source_path": source},
                },
                root + ".R1": {
                    "children": {},
                    "kind": "Component",
                    "reference_designator": "R1",
                    "type_ref": {
                        "module_name": "R",
                        "source_path": source + "/stdlib/Resistor.zen",
                    },
                },
            },
            "nets": {
                "INPUT": {
                    "kind": "Net",
                    "name": "INPUT",
                    "ports": [root + ".R1.1"],
                }
            },
        }

    original = schematic("/original/Board.zen:<root>", "/original/Board.zen")
    shadow = schematic("/shadow/Board.zen:<root>", "/shadow/Board.zen")

    assert connectivity_digest(original) == connectivity_digest(shadow)


def test_inspect_schematic_counts_physical_components() -> None:
    schematic = {
        "root_ref": "root",
        "instances": {
            "root": {"children": {"R1": "root.R1"}},
            "root.R1": {"reference_designator": "R1"},
            "root.R1.P1": {"reference_designator": None},
        },
        "nets": {"N": {}},
        "symbols": {},
    }

    facts = inspect_schematic(schematic)

    assert facts["physical_component_count"] == 1
