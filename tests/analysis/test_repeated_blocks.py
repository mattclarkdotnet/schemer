from __future__ import annotations

from copy import deepcopy

from schemer.analysis.inventory import inventory_markdown, structural_inventory
from schemer.analysis.repetition import repeated_owner_blocks
from tests.support.repeated import SYMBOL, fixture


def test_equivalent_blocks_ignore_names_values_and_reference_order():
    source, groups = fixture()
    before = deepcopy(source)
    family, = repeated_owner_blocks(source, groups)
    assert len(family) == 2
    assert set(family[0]) == set(family[1])
    assert family[0]["anchor"] == "root.NET.A.IC"
    assert family[1]["anchor"] == "root.NET.B.IC"
    assert source == before


def test_changed_pin_mapping_does_not_reuse_layout():
    source, groups = fixture()
    source["nets"]["B0"]["ports"] = ["root.NET.B.IC.2", "root.NET.B.PART.2"]
    source["nets"]["B1"]["ports"] = ["root.NET.B.IC.1"]
    assert not repeated_owner_blocks(source, groups)


def test_changed_role_or_symbol_does_not_reuse_layout():
    for field, value in (("__symbol_value", {"String": SYMBOL.replace('"R"', '"Different"')}),
                         ("schematic_properties", {"Json": {
                             "role": "current-limit", "owner": "U2", "pin": "1",
                             "group": "B",
                         }})):
        source, groups = fixture()
        source["instances"]["root.NET.B.PART"]["attributes"][field] = value
        assert not repeated_owner_blocks(source, groups)


def test_local_reuse_does_not_require_independent_representation_or_groups():
    source, groups = fixture()
    source["instances"]["root.NET"]["attributes"] = {}
    groups = {ref: "shared-connected-circuit" for ref in groups}
    before = deepcopy(groups)
    assert len(repeated_owner_blocks(source, groups)) == 1
    inventory = structural_inventory(source)
    assert any(f.kind == "owner-block" for f in inventory.families)
    assert len(repeated_owner_blocks(source, groups, inventory)) == 1
    assert groups == before


def test_inventory_is_deterministic_read_only_and_shared_with_reuse():
    source, groups = fixture()
    before = deepcopy(source)
    inventory = structural_inventory(source)
    assert source == before
    source["instances"] = dict(reversed(list(source["instances"].items())))
    source["nets"] = dict(reversed(list(source["nets"].items())))
    assert structural_inventory(source) == inventory
    family, = repeated_owner_blocks(source, groups, inventory)
    assert family is inventory.families[0].members or family == inventory.families[0].members
    text = inventory_markdown(inventory)
    assert "U98 / U2" in text
    assert "NOT circuit" in text


def test_same_symbols_do_not_claim_matching_circuits():
    source, _ = fixture()
    source["nets"]["B0"]["ports"] = ["root.NET.B.IC.2", "root.NET.B.PART.2"]
    inventory = structural_inventory(source)
    assert not any(f.kind == "owner-block" for f in inventory.families)
    assert any(f.kind == "same-symbol" for f in inventory.families)


def test_ambiguous_owned_parts_are_reported_not_paired_by_name():
    source, groups = fixture()
    for suffix in ("A", "B"):
        ref = f"root.NET.{suffix}.DUPLICATE"
        source["instances"][ref] = deepcopy(source["instances"][f"root.NET.{suffix}.PART"])
        source["instances"][ref]["reference_designator"] = "DUP" + suffix
        groups[ref] = suffix
    inventory = structural_inventory(source)
    assert len(inventory.unmatched) == 2
    assert all(i["reason"] == "ambiguous correspondence" for i in inventory.unmatched)
    assert not repeated_owner_blocks(source, groups, inventory)


def test_authored_groups_match_without_ownership_or_representation():
    source, _ = fixture()
    source["instances"]["root.NET"]["attributes"] = {}
    for suffix in ("A", "B"):
        for order, name in enumerate(("IC", "PART")):
            source["instances"][f"root.NET.{suffix}.{name}"]["attributes"][
                "schematic_properties"] = {"Json": {
                    "role": "series", "group": suffix + "-chain", "order": order,
                }}
    inventory = structural_inventory(source)
    assert [f.kind for f in inventory.families] == ["authored-group", "same-symbol"]


def test_owner_cycles_and_unknown_owners_are_reported():
    source, _ = fixture()
    for ref in ("root.NET.A.IC", "root.NET.B.IC"):
        source["instances"][ref]["attributes"]["schematic_properties"] = {
            "Json": {"role": "bypass", "owner": "R9" if ".A." in ref else "MISSING"},
        }
    inventory = structural_inventory(source)
    assert len(inventory.unmatched) == 4
    assert not any(f.kind == "owner-block" for f in inventory.families)


def test_distinct_symbols_disambiguate_same_role_on_same_pins():
    source, groups = fixture()
    for suffix in ("A", "B"):
        ref = f"root.NET.{suffix}.DIODE"
        source["instances"][ref] = deepcopy(source["instances"][f"root.NET.{suffix}.PART"])
        source["instances"][ref]["reference_designator"] = "D" + suffix
        source["instances"][ref]["attributes"]["__symbol_value"] = {
            "String": SYMBOL.replace('"R"', '"Diode"'),
        }
        groups[ref] = suffix
    inventory = structural_inventory(source)
    assert not inventory.unmatched
    family, = repeated_owner_blocks(source, groups, inventory)
    assert all(len(member) == 3 for member in family)


def test_authored_net_attachment_matches_topology_not_net_spelling():
    source, groups = fixture()
    for suffix in ("A", "B"):
        source["instances"][f"root.NET.{suffix}.PART"]["attributes"][
            "schematic_properties"]["Json"]["at"] = suffix + "0"
    assert len(repeated_owner_blocks(source, groups)) == 1
    source["instances"]["root.NET.B.PART"]["attributes"][
        "schematic_properties"]["Json"]["at"] = "B2"
    assert not repeated_owner_blocks(source, groups)
    source["instances"]["root.NET.B.PART"]["attributes"][
        "schematic_properties"]["Json"]["at"] = "MISSING"
    assert structural_inventory(source).unmatched[0]["reason"] == (
        "unresolved authored net attachment"
    )


def test_external_connection_is_part_of_match():
    source, groups = fixture()
    del source["nets"]["B2"]
    source["nets"]["A0"]["ports"].append("root.NET.B.PART.1")
    assert not repeated_owner_blocks(source, groups)
