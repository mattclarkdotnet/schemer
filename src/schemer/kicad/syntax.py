from __future__ import annotations

import json
from dataclasses import dataclass

from schemer.core.errors import KiCadSchematicError


@dataclass(frozen=True)
class Atom:
    value: str
    start: int
    end: int
    quoted: bool = False


@dataclass(frozen=True)
class ListExpr:
    children: tuple[Expr, ...]
    start: int
    end: int

    @property
    def tag(self) -> str | None:
        if not self.children or not isinstance(self.children[0], Atom):
            return None
        return self.children[0].value

    def lists(self, tag: str) -> tuple[ListExpr, ...]:
        return tuple(
            child for child in self.children[1:] if isinstance(child, ListExpr) and child.tag == tag
        )

    def first_list(self, tag: str) -> ListExpr | None:
        return next(iter(self.lists(tag)), None)


type Expr = Atom | ListExpr


@dataclass(frozen=True)
class Edit:
    start: int
    end: int
    replacement: str


class Parser:
    def __init__(self, source: str):
        self.source = source
        self.index = 0

    def parse(self) -> ListExpr:
        self._skip_space()
        expression = self._expression()
        self._skip_space()
        if self.index != len(self.source):
            raise KiCadSchematicError(f"unexpected trailing input at offset {self.index}")
        if not isinstance(expression, ListExpr):
            raise KiCadSchematicError("expected a parenthesized KiCad document")
        return expression

    def _skip_space(self) -> None:
        while self.index < len(self.source) and self.source[self.index].isspace():
            self.index += 1

    def _expression(self) -> Expr:
        self._skip_space()
        if self.index >= len(self.source):
            raise KiCadSchematicError("unexpected end of input")
        if self.source[self.index] == "(":
            return self._list()
        if self.source[self.index] == '"':
            return self._string()
        return self.sexpr_atom()

    def _list(self) -> ListExpr:
        start = self.index
        self.index += 1
        children: list[Expr] = []
        while True:
            self._skip_space()
            if self.index >= len(self.source):
                raise KiCadSchematicError(f"unterminated list at offset {start}")
            if self.source[self.index] == ")":
                self.index += 1
                return ListExpr(tuple(children), start, self.index)
            children.append(self._expression())

    def _string(self) -> Atom:
        start = self.index
        self.index += 1
        escaped = False
        while self.index < len(self.source):
            character = self.source[self.index]
            self.index += 1
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                token = self.source[start : self.index]
                try:
                    value = json.loads(token)
                except json.JSONDecodeError as error:
                    raise KiCadSchematicError(
                        f"invalid quoted string at offset {start}: {error.msg}"
                    ) from error
                return Atom(value, start, self.index, quoted=True)
        raise KiCadSchematicError(f"unterminated string at offset {start}")

    def sexpr_atom(self) -> Atom:
        start = self.index
        while self.index < len(self.source):
            character = self.source[self.index]
            if character.isspace() or character in "()":
                break
            self.index += 1
        if start == self.index:
            raise KiCadSchematicError(f"unexpected token at offset {start}")
        return Atom(self.source[start : self.index], start, self.index)


def sexpr_atom(expression: ListExpr, index: int, context: str) -> Atom:
    try:
        item = expression.children[index]
    except IndexError as error:
        raise KiCadSchematicError(f"{context} is missing atom {index}") from error
    if not isinstance(item, Atom):
        raise KiCadSchematicError(f"{context} atom {index} is a list")
    return item


def sexpr_number(atom: Atom, context: str) -> float:
    try:
        return float(atom.value)
    except ValueError as error:
        raise KiCadSchematicError(f"{context} is not numeric: {atom.value!r}") from error


def sexpr_at(expression: ListExpr, context: str) -> tuple[float, float, float]:
    at = expression.first_list("at")
    if at is None:
        raise KiCadSchematicError(f"{context} has no at expression")
    x = sexpr_number(sexpr_atom(at, 1, context), context)
    y = sexpr_number(sexpr_atom(at, 2, context), context)
    rotation = sexpr_number(sexpr_atom(at, 3, context), context) if len(at.children) > 3 else 0.0
    return x, y, rotation


def uuid(expression: ListExpr) -> str:
    uuid = expression.first_list("uuid")
    return sexpr_atom(uuid, 1, "uuid").value if uuid is not None else ""


def descendant(expression: ListExpr, path: tuple[str, ...]) -> ListExpr | None:
    current = expression
    for tag in path:
        child = current.first_list(tag)
        if child is None:
            return None
        current = child
    return current


def format_number(value: float) -> str:
    if value <= 0:
        raise KiCadSchematicError("text size must be positive")
    return f"{value:.6f}".rstrip("0").rstrip(".")


def apply_edits(source: str, edits: list[Edit]) -> str:
    ordered = sorted(edits, key=lambda edit: edit.start, reverse=True)
    previous_start = len(source) + 1
    result = source
    for edit in ordered:
        if edit.end > previous_start:
            raise KiCadSchematicError("overlapping KiCad source edits")
        result = result[: edit.start] + edit.replacement + result[edit.end :]
        previous_start = edit.start
    return result
