from schemer.view_policy import (
    clean_schematic_labels,
    electrical_view,
    focus_module,
    hide_root_children,
)


def test_electrical_view_suppresses_service_groups_from_generic_metadata() -> None:
    root = "Circuit.zen:<root>"
    service = root + ".CALIBRATION"
    signal = root + ".FUNCTION"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {
                "children": {"CALIBRATION": service, "FUNCTION": signal},
                "symbol_positions": {},
            },
            service: {"kind": "Module", "children": {"POINT": service + ".POINT"}},
            service + ".POINT": {
                "kind": "Component",
                "children": {"1": service + ".POINT.1"},
                "attributes": {
                    "skip_bom": {"Boolean": True},
                    "skip_pos": {"Boolean": True},
                },
            },
            service + ".POINT.1": {"kind": "Port", "children": {}},
            signal: {"kind": "Module", "children": {"PART": signal + ".PART"}},
            signal + ".PART": {
                "kind": "Component",
                "children": {"1": signal + ".PART.1"},
                "attributes": {"type": {"String": "connector"}},
            },
            signal + ".PART.1": {"kind": "Port", "children": {}},
        },
        "nets": {
            "NET": {"ports": [service + ".POINT.1", signal + ".PART.1"]},
        },
    }

    filtered = electrical_view(schematic)

    assert filtered["instances"][root]["children"] == {"FUNCTION": signal}
    assert not any(ref.startswith(service) for ref in filtered["instances"])
    assert filtered["nets"]["NET"]["ports"] == [signal + ".PART.1"]
    assert schematic["instances"][root]["children"] == {
        "CALIBRATION": service,
        "FUNCTION": signal,
    }


def test_hide_root_children_prunes_subtree_ports_and_positions() -> None:
    root = "Board.zen:<root>"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {
                "children": {"R1": root + ".R1", "H1": root + ".H1"},
                "symbol_positions": {
                    "comp:R1.R": {"x": 1, "y": 2},
                    "comp:H1.MH": {"x": 3, "y": 4},
                    "sym:GND#1": {"x": 5, "y": 6},
                },
            },
            root + ".R1": {"children": {}},
            root + ".H1": {"children": {"MH": root + ".H1.MH"}},
            root + ".H1.MH": {"children": {"1": root + ".H1.MH.1"}},
            root + ".H1.MH.1": {"children": {}},
        },
        "nets": {
            "GND": {
                "ports": [root + ".R1.1", root + ".H1.MH.1"],
            }
        },
    }

    filtered = hide_root_children(schematic, {"H1"}, root_symbol_ids={"sym:GND#1"})

    assert filtered["instances"][root]["children"] == {"R1": root + ".R1"}
    assert set(filtered["instances"]) == {root, root + ".R1"}
    assert filtered["nets"]["GND"]["ports"] == [root + ".R1.1"]
    assert filtered["instances"][root]["symbol_positions"] == {"comp:R1.R": {"x": 1, "y": 2}}
    assert schematic["instances"][root]["children"]["H1"] == root + ".H1"


def test_focus_module_rebases_presentation_to_one_existing_subtree() -> None:
    root = "Board.zen:<root>"
    a = root + ".A"
    b = root + ".B"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {"children": {"A": a, "B": b}},
            a: {"children": {"R1": a + ".R1"}, "symbol_positions": {}},
            a + ".R1": {"children": {}},
            b: {"children": {"R2": b + ".R2"}},
            b + ".R2": {"children": {}},
        },
        "nets": {
            "SHARED": {"ports": [a + ".R1.1", b + ".R2.1"]},
            "B_ONLY": {"ports": [b + ".R2.2"]},
        },
    }

    focused = focus_module(schematic, a)

    assert focused["root_ref"] == a
    assert set(focused["instances"]) == {a, a + ".R1"}
    assert focused["nets"]["SHARED"]["ports"] == [a + ".R1.1"]
    assert focused["nets"]["B_ONLY"]["ports"] == []
    assert schematic["root_ref"] == root


def test_clean_schematic_labels_uses_shortest_unambiguous_net_suffixes() -> None:
    root = "Board.zen:<root>"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {
                "symbol_positions": {
                    "sym:LEFT.DATA#0": {"x": 1, "y": 2},
                    "sym:RIGHT.DATA#0": {"x": 3, "y": 4},
                    "sym:LEFT.CLOCK#0": {"x": 5, "y": 6},
                }
            }
        },
        "nets": {
            "LEFT.DATA": {"id": 1, "name": "LEFT.DATA", "ports": ["U1.1"]},
            "RIGHT.DATA": {"id": 2, "name": "RIGHT.DATA", "ports": ["U2.1"]},
            "LEFT.CLOCK": {"id": 3, "name": "LEFT.CLOCK", "ports": ["U1.2"]},
        },
    }

    cleaned = clean_schematic_labels(schematic)

    assert set(cleaned["nets"]) == {"LEFT.DATA", "RIGHT.DATA", "CLOCK"}
    assert cleaned["nets"]["CLOCK"]["name"] == "CLOCK"
    assert cleaned["instances"][root]["symbol_positions"] == {
        "sym:LEFT.DATA#0": {"x": 1, "y": 2},
        "sym:RIGHT.DATA#0": {"x": 3, "y": 4},
        "sym:CLOCK#0": {"x": 5, "y": 6},
    }
    assert set(schematic["nets"]) == {"LEFT.DATA", "RIGHT.DATA", "LEFT.CLOCK"}


def test_clean_schematic_labels_keeps_descriptions_out_of_annotations() -> None:
    root = "Board.zen:<root>"
    component = root + ".J1"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {},
            component: {
                "kind": "Component",
                "attributes": {
                    "description": {"String": "1234 cable direct-solder termination"},
                    "mpn": {"String": "1234"},
                    "value": {"String": "1234 cable direct-solder termination"},
                },
            },
        },
        "nets": {},
    }

    cleaned = clean_schematic_labels(schematic)

    attributes = cleaned["instances"][component]["attributes"]
    assert "description" not in attributes
    assert attributes["value"] == {"String": "1234"}
    assert schematic["instances"][component]["attributes"]["description"] == {
        "String": "1234 cable direct-solder termination"
    }


def test_clean_schematic_labels_preserves_electrical_values() -> None:
    root = "Board.zen:<root>"
    component = root + ".R1"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {},
            component: {
                "kind": "Component",
                "attributes": {
                    "description": {"String": "10k"},
                    "mpn": {"String": "RC0603FR-0710KL"},
                    "package": {"String": "0603"},
                    "value": {"String": "10k"},
                },
            },
        },
        "nets": {},
    }

    cleaned = clean_schematic_labels(schematic)

    assert cleaned["instances"][component]["attributes"]["value"] == {"String": "10k"}
    assert "package" not in cleaned["instances"][component]["attributes"]
    assert schematic["instances"][component]["attributes"]["package"] == {"String": "0603"}


def test_clean_schematic_labels_hides_both_package_attribute_spellings() -> None:
    root = "Board.zen:<root>"
    lower = root + ".R1"
    title = root + ".C1"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {},
            lower: {
                "kind": "Component",
                "attributes": {
                    "package": {"String": "0402"},
                    "value": {"String": "1k"},
                },
            },
            title: {
                "kind": "Component",
                "attributes": {
                    "Package": {"String": "0603"},
                    "Value": {"String": "100nF"},
                },
            },
        },
        "nets": {},
    }

    cleaned = clean_schematic_labels(schematic)

    assert cleaned["instances"][lower]["attributes"] == {"value": {"String": "1k"}}
    assert cleaned["instances"][title]["attributes"] == {"Value": {"String": "100nF"}}
    assert schematic["instances"][lower]["attributes"]["package"] == {"String": "0402"}
    assert schematic["instances"][title]["attributes"]["Package"] == {"String": "0603"}


def test_clean_schematic_labels_separates_functional_pin_names_from_numbers() -> None:
    root = "Board.zen:<root>"
    component = root + ".U1"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {},
            component: {
                "kind": "Component",
                "children": {
                    "DATA_A6": component + ".DATA_A6",
                    "DATA_B6": component + ".DATA_B6",
                    "MODE_7": component + ".MODE_7",
                    "Pin_1": component + ".Pin_1",
                    "Pin_2": component + ".Pin_2",
                },
                "attributes": {
                    "__symbol_value": {
                        "String": """
                            (symbol "U"
                              (pin bidirectional line (at 0 0 0)
                                (name "DATA_A6") (number "A6"))
                              (pin bidirectional line (at 0 2.54 0)
                                (name "DATA_B6") (number "B6"))
                              (pin input line (at 0 5.08 0)
                                (name "MODE_7") (number "7"))
                              (pin passive line (at 0 7.62 0)
                                (name "Pin_1") (number "1"))
                              (pin passive line (at 0 10.16 0)
                                (name "Pin_2") (number "2")))
                        """
                    }
                },
            },
        },
        "nets": {},
    }

    cleaned = clean_schematic_labels(schematic)

    instance = cleaned["instances"][component]
    symbol = instance["attributes"]["__symbol_value"]["String"]
    assert symbol.count('(name "DATA")') == 2
    assert '(name "DATA_A6")' not in symbol
    assert '(name "DATA_B6")' not in symbol
    assert '(name "MODE_7")' in symbol
    assert '(name "Pin_1")' in symbol
    assert '(name "Pin_2")' in symbol
    assert '(number "A6")' in symbol
    assert '(number "B6")' in symbol
    assert instance["children"] == schematic["instances"][component]["children"]


def test_clean_schematic_labels_carries_functional_pin_names_to_nets() -> None:
    root = "Board.zen:<root>"
    connector = root + ".J1"
    isolator = root + ".U1"
    resistor = root + ".R1"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {
                "symbol_positions": {
                    "sym:USB.HOST_DP_RAW#0": {"x": 1, "y": 2},
                    "sym:USB.HOST_DP_ISOLATOR#0": {"x": 3, "y": 4},
                }
            },
            connector: {
                "kind": "Component",
                "children": {
                    "D+_A6": connector + ".D+_A6",
                    "D+_B6": connector + ".D+_B6",
                },
                "attributes": {
                    "type": "connector",
                    "__symbol_value": {
                        "String": """
                            (symbol "J"
                              (pin bidirectional line (at 0 0 0)
                                (name "D+_A6") (number "A6"))
                              (pin bidirectional line (at 0 2.54 0)
                                (name "D+_B6") (number "B6")))
                        """
                    },
                },
            },
            isolator: {
                "kind": "Component",
                "children": {"UD+": isolator + ".UD+"},
                "attributes": {
                    "type": "integrated_circuit",
                    "__symbol_value": {
                        "String": """
                            (symbol "U"
                              (pin bidirectional line (at 0 0 0)
                                (name "UD+") (number "7")))
                        """
                    },
                },
            },
            resistor: {
                "kind": "Component",
                "children": {"1": resistor + ".1", "2": resistor + ".2"},
                "attributes": {"type": "resistor"},
            },
        },
        "nets": {
            "USB.HOST_DP_RAW": {
                "name": "USB.HOST_DP_RAW",
                "ports": [connector + ".D+_A6", connector + ".D+_B6", resistor + ".1"],
            },
            "USB.HOST_DP_ISOLATOR": {
                "name": "USB.HOST_DP_ISOLATOR",
                "ports": [isolator + ".UD+", resistor + ".2"],
            },
        },
    }

    cleaned = clean_schematic_labels(schematic)

    assert set(cleaned["nets"]) == {"D+", "UD+"}
    assert cleaned["nets"]["D+"]["ports"] == schematic["nets"]["USB.HOST_DP_RAW"]["ports"]
    assert cleaned["instances"][root]["symbol_positions"] == {
        "sym:D+#0": {"x": 1, "y": 2},
        "sym:UD+#0": {"x": 3, "y": 4},
    }
    assert set(schematic["nets"]) == {"USB.HOST_DP_RAW", "USB.HOST_DP_ISOLATOR"}


def test_clean_schematic_labels_never_renames_a_rail_after_one_active_pin() -> None:
    root = "Board.zen:<root>"
    transistor = root + ".Q1"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {"symbol_positions": {"sym:DIGITAL_GND#0": {"x": 1, "y": 2}}},
            transistor: {
                "kind": "Component",
                "children": {"S": transistor + ".S"},
                "attributes": {
                    "__symbol_value": {
                        "String": """
                            (symbol "Q"
                              (pin passive line (at 0 0 0)
                                (name "S") (number "3")))
                        """
                    },
                },
            },
        },
        "nets": {
            "DIGITAL_GND": {
                "kind": "Ground",
                "name": "DIGITAL_GND",
                "ports": [transistor + ".S"],
            }
        },
    }

    cleaned = clean_schematic_labels(schematic)

    assert set(cleaned["nets"]) == {"DIGITAL_GND"}
    assert cleaned["instances"][root]["symbol_positions"] == {"sym:DIGITAL_GND#0": {"x": 1, "y": 2}}


def test_clean_schematic_labels_does_not_replace_a_net_with_one_letter_pin_name() -> None:
    root = "Board.zen:<root>"
    transistor = root + ".Q1"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {"symbol_positions": {"sym:RESET_REQUEST#0": {"x": 1, "y": 2}}},
            transistor: {
                "kind": "Component",
                "children": {"G": transistor + ".G"},
                "attributes": {
                    "__symbol_value": {
                        "String": """
                            (symbol "Q"
                              (pin input line (at 0 0 0)
                                (name "G") (number "1")))
                        """
                    },
                },
            },
        },
        "nets": {
            "RESET_REQUEST": {
                "kind": "Net",
                "name": "RESET_REQUEST",
                "ports": [transistor + ".G"],
            }
        },
    }

    cleaned = clean_schematic_labels(schematic)

    assert set(cleaned["nets"]) == {"RESET_REQUEST"}
    assert cleaned["instances"][root]["symbol_positions"] == {
        "sym:RESET_REQUEST#0": {"x": 1, "y": 2}
    }


def test_clean_schematic_labels_keeps_net_names_when_pin_names_collide() -> None:
    root = "Board.zen:<root>"
    left = root + ".U1"
    right = root + ".U2"
    symbol = """
        (symbol "U"
          (pin output line (at 0 0 0) (name "DATA") (number "1")))
    """
    schematic = {
        "root_ref": root,
        "instances": {
            root: {},
            left: {
                "kind": "Component",
                "children": {"DATA": left + ".DATA"},
                "attributes": {"type": "integrated_circuit", "__symbol_value": symbol},
            },
            right: {
                "kind": "Component",
                "children": {"DATA": right + ".DATA"},
                "attributes": {"type": "integrated_circuit", "__symbol_value": symbol},
            },
        },
        "nets": {
            "LEFT.DATA_NET": {"name": "LEFT.DATA_NET", "ports": [left + ".DATA"]},
            "RIGHT.DATA_NET": {"name": "RIGHT.DATA_NET", "ports": [right + ".DATA"]},
        },
    }

    cleaned = clean_schematic_labels(schematic)

    assert set(cleaned["nets"]) == {"LEFT.DATA_NET", "RIGHT.DATA_NET"}
