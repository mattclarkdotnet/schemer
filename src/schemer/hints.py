"""Semantic layout hints stored ahead of pcb:sch positions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from schemer.layout import position_block_start
from schemer.toolchain import ToolchainError

PREFIX = "# schemer:hint "
KINDS = {"local-return": 2, "pin-exit": 1, "right-of": 2}
# Type approval does not make a relationship an automatic placement default.
APPROVED_KINDS = frozenset({"local-return", "pin-exit"})


@dataclass(frozen=True, order=True)
class Endpoint:
    component: str
    pin: str


@dataclass(frozen=True)
class Hint:
    id: str
    kind: str
    endpoints: tuple[Endpoint, ...]
    reason: str
    blocks: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result = {
            "version": 1,
            "id": self.id,
            "kind": self.kind,
            "endpoints": [
                {"component": endpoint.component, "pin": endpoint.pin}
                for endpoint in self.endpoints
            ],
            "reason": self.reason,
        }
        if self.kind == "right-of":
            result.pop("endpoints")
            result["blocks"] = list(self.blocks)
        return result


def parse_hints(content: str) -> tuple[Hint, ...]:
    """Read strict semantic records; unknown fields cannot smuggle coordinates."""
    hints = []
    seen_ids: set[str] = set()
    seen_requests: set[tuple[str, tuple[Endpoint, ...] | tuple[str, ...]]] = set()
    for number, line in enumerate(content.splitlines(), 1):
        if not line.strip().startswith("# schemer:hint"):
            continue
        try:
            if not line.strip().startswith(PREFIX):
                raise ValueError("expected '# schemer:hint ' followed by JSON")
            data = json.loads(line.strip()[len(PREFIX) :])
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            member_field = "blocks" if data.get("kind") == "right-of" else "endpoints"
            if set(data) != {
                "version",
                "id",
                "kind",
                member_field,
                "reason",
            }:
                raise ValueError(f"expected version, id, kind, {member_field} and reason only")
            if type(data["version"]) is not int or data["version"] != 1:
                raise ValueError("unsupported hint version")
            if any(
                not isinstance(data[key], str) or not data[key].strip()
                for key in ("id", "kind", "reason")
            ):
                raise ValueError("id, kind and reason must be nonempty strings")
            if data["kind"] not in KINDS:
                raise ValueError(f"unsupported hint kind: {data['kind']}")
            members = data[member_field]
            if not isinstance(members, list) or len(members) != KINDS[data["kind"]]:
                raise ValueError("wrong endpoint count")
            endpoints = []
            blocks = ()
            if member_field == "blocks":
                if any(not isinstance(name, str) or not name.strip() for name in members):
                    raise ValueError("blocks must be nonempty source-local names")
                if len(set(members)) != len(members):
                    raise ValueError("a block cannot be right of itself")
                blocks = tuple(members)
                members = []
            for member in members:
                if not isinstance(member, dict) or set(member) != {"component", "pin"}:
                    raise ValueError("endpoints require component and pin only")
                if any(
                    not isinstance(value, str) or not value.strip() for value in member.values()
                ):
                    raise ValueError("component and pin must be nonempty strings")
                endpoints.append(Endpoint(member["component"], member["pin"]))
            endpoints = tuple(sorted(endpoints))
            request = data["kind"], blocks or endpoints
            if len(set(endpoints)) != len(endpoints):
                raise ValueError("duplicate endpoint")
            if data["id"] in seen_ids or request in seen_requests:
                raise ValueError("duplicate hint ID or semantic request")
            seen_ids.add(data["id"])
            seen_requests.add(request)
            hints.append(Hint(data["id"], data["kind"], endpoints, data["reason"], blocks))
        except (ValueError, TypeError) as error:
            raise ToolchainError(f"invalid layout hint at line {number}: {error}") from error
    return tuple(hints)


def replace_hint_preamble(content: str, hint_content: str) -> str:
    """Replace semantic records, preserving electrical text and position records."""
    hints = parse_hints(hint_content)
    if any(
        line.strip() and not line.lstrip().startswith("#") for line in hint_content.splitlines()
    ):
        raise ToolchainError("hint input must contain comments only")
    clean = "".join(
        line
        for line in content.splitlines(keepends=True)
        if not line.strip().startswith("# schemer:hint")
    )
    start = position_block_start(clean)
    prefix, positions = clean[:start], clean[start:]
    if not hints:
        return clean
    records = "".join(
        PREFIX + json.dumps(hint.as_dict(), separators=(",", ":")) + "\n" for hint in hints
    )
    separator = "" if not prefix or prefix.endswith("\n") else "\n"
    return prefix + separator + records + positions


@dataclass
class HintSet:
    hints: tuple[Hint, ...] = ()
    applied: set[str] = field(default_factory=set)

    @classmethod
    def from_source(cls, path: Path, *, allow_experimental: bool) -> HintSet:
        hints = parse_hints(path.read_text()) if path.is_file() else ()
        pending = sorted({hint.kind for hint in hints} - APPROVED_KINDS)
        if pending and not allow_experimental:
            raise ToolchainError(
                f"{path}: hint types await user review ({', '.join(pending)}); "
                "use --experimental-hints for this run"
            )
        return cls(hints)

    def take(self, kind: str, *endpoints: Endpoint) -> bool:
        for hint in self.hints:
            if hint.kind == kind and hint.endpoints == tuple(sorted(endpoints)):
                if hint.id in self.applied:
                    raise ToolchainError(f"hint was consumed twice: {hint.id}")
                self.applied.add(hint.id)
                return True
        return False

    def require_all_applied(self) -> None:
        pending = [hint.id for hint in self.hints if hint.id not in self.applied]
        if pending:
            raise ToolchainError(
                "unresolved or unsupported layout hints: "
                + ", ".join(pending)
                + "; check source-local component paths, terminal names and supported topology"
            )
