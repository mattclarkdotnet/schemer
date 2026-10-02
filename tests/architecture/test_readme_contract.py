from __future__ import annotations

import ast
import inspect
import json
import re

from schemer.analysis.roles import RoleSource, component_roles
from schemer.source.hints import parse_hints
from tests.paths import REPO


def test_readme_lists_every_supported_role():
    tree = ast.parse(inspect.getsource(component_roles))
    roles = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "kind":
            continue
        for item in ast.walk(node.comparators[0]):
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                roles.add(item.value)
    readme = (REPO / "README.md").read_text()
    table = readme.split("| Role |", 1)[1].split("\n\n", 1)[0]
    assert set(re.findall(r"^\| `([^`]+)`", table, re.MULTILINE)) == roles


def test_readme_comment_hints_and_role_examples_parse():
    readme = (REPO / "README.md").read_text()
    hints = "\n".join(line for line in readme.splitlines() if line.startswith("# schemer:hint "))
    assert {hint.kind for hint in parse_hints(hints)} == {"local-return", "pin-exit", "right-of"}
    for line in readme.splitlines():
        if not line.startswith('{"role":'):
            continue
        payload = json.loads(line)
        source = {"attributes": {"schematic_properties": {"Json": payload}}}
        parsed = component_roles({}, (RoleSource("PART", source),))
        assert parsed[0].kind == payload["role"]


def test_repository_skill_documentation_links_resolve():
    skill = REPO / ".agents/skills/schemer/SKILL.md"
    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", skill.read_text())
    assert links
    for link in links:
        path, _, anchor = link.partition("#")
        target = (skill.parent / path).resolve()
        assert target.is_relative_to(REPO), link
        assert target.is_file(), link
        if anchor:
            headings = re.findall(r"^#{1,6} (.+)$", target.read_text(), re.MULTILINE)
            anchors = {
                re.sub(r"[^\w -]", "", heading.lower()).replace(" ", "-")
                for heading in headings
            }
            assert anchor in anchors, link
