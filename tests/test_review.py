from schemer.review import direct_child_review_targets
from schemer.symbol_geometry import QualityFinding


def test_review_targets_resolve_named_children_and_stable_slugs() -> None:
    root = "Board.zen:<root>"
    schematic = {
        "root_ref": root,
        "instances": {
            root: {"children": {"DSP_CORE": root + ".DSP_CORE", "USB": root + ".USB"}},
            root + ".DSP_CORE": {"children": {}},
            root + ".USB": {"children": {}},
        },
    }

    targets = direct_child_review_targets(schematic, ["DSP_CORE", "USB"])

    assert [(target.slug, target.instance_ref) for target in targets] == [
        ("dsp-core", root + ".DSP_CORE"),
        ("usb", root + ".USB"),
    ]


def test_quality_finding_has_stable_manifest_shape() -> None:
    finding = QualityFinding(
        code="series-bank-pitch",
        module_ref="Board:<root>.USB",
        symbol_ids=("comp:R1.R", "comp:R2.R"),
        measured=42.12567,
        threshold=100,
        message="too close",
    )

    assert finding.as_dict() == {
        "code": "series-bank-pitch",
        "measured": 42.1257,
        "message": "too close",
        "module_ref": "Board:<root>.USB",
        "symbol_ids": ["comp:R1.R", "comp:R2.R"],
        "threshold": 100,
    }
