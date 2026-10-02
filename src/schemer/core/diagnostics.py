from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_issues: ContextVar[list[dict] | None] = ContextVar("layout_draft_issues", default=None)


@contextmanager
def capture_layout_issues(draft: bool) -> Iterator[list[dict]]:
    issues: list[dict] = []
    token = _issues.set(issues if draft else None)
    try:
        yield issues
    finally:
        _issues.reset(token)


def record_draft_issue(code: str, message: str, subjects: tuple[str, ...] = ()) -> bool:
    """Return False outside draft mode; callers must retain their normal error."""
    issues = _issues.get()
    if issues is None:
        return False
    issue = {"code": code, "message": message, "subjects": list(subjects)}
    if issue not in issues:
        issues.append(issue)
    return True
