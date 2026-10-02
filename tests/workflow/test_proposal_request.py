from dataclasses import fields
from pathlib import Path

from schemer.cli.parser import build_parser
from schemer.workflow.proposal import LayoutRequest


def test_cli_supplies_every_proposal_request_field():
    args = build_parser().parse_args(["layout", "Board.zen"])
    request = LayoutRequest(
        **{field.name: getattr(args, field.name) for field in fields(LayoutRequest)}
    )
    assert request.entrypoint == Path("Board.zen")
    assert request.write is False
