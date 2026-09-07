import ast
from pathlib import Path

PRODUCTION = Path(__file__).parents[1] / "src" / "schemer"
CORPUS_TOKENS = {
    "SampleBoard",
    "sample-board",
    "DigitalAbx",
    "digital_abx",
    "DSP_CORE",
    "PICO_VSYS",
    "USB_HOST_GND",
    "SPDIF_TX_COAX",
    "ADUM3160",
    "74HC14",
}


def test_production_layout_has_no_corpus_specific_rules() -> None:
    offenders: list[str] = []
    for path in sorted(PRODUCTION.glob("*.py")):
        source = path.read_text()
        for token in CORPUS_TOKENS:
            if token in source:
                offenders.append(f"{path.name}: corpus token {token!r}")

        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if isinstance(value, ast.Dict) and any(
                    isinstance(target, ast.Name) and target.id.endswith("_POSITIONS")
                    for target in targets
                ):
                    offenders.append(f"{path.name}:{node.lineno}: stored position table")
            if (
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Attribute)
                and node.left.attr == "name"
                and any(
                    isinstance(comparator, ast.Constant)
                    and isinstance(comparator.value, str)
                    and comparator.value.endswith(".zen")
                    for comparator in node.comparators
                )
            ):
                offenders.append(f"{path.name}:{node.lineno}: entrypoint filename branch")

    assert offenders == []


def test_layout_principles_are_not_a_board_specific_acceptance_contract() -> None:
    principles = (PRODUCTION.parents[1] / "docs/schematic-layout-principles.md").read_text()
    corpus_terms = CORPUS_TOKENS | {
        "ABX", "Pico", "J301", "Q101", "R101", "A101", "DSP core", "750 ohm", "110 ohm",
    }
    assert not [term for term in corpus_terms if term in principles]
