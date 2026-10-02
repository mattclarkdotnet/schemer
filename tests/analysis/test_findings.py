from __future__ import annotations

from schemer.analysis.findings import QualityFinding


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
