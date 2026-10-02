import pytest

from schemer.core.attributes import attribute_string


@pytest.mark.parametrize(("attributes", "expected"), [
    (None, None),
    ([], None),
    ({}, None),
    ({"role": "MiXeD"}, "MiXeD"),
    ({"role": ""}, ""),
    ({"role": {"String": "owner"}}, "owner"),
    ({"role": {"String": 7}}, None),
    ({"role": {"Number": 7}}, None),
    ({"role": False}, None),
])
def test_attribute_string_preserves_source_text(attributes, expected):
    assert attribute_string({"attributes": attributes}, "role") == expected


def test_attribute_string_accepts_missing_attributes():
    assert attribute_string({}, "role") is None
