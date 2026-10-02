from __future__ import annotations

from dataclasses import dataclass

from schemer.symbols.model import Point


@dataclass(frozen=True)
class SeriesFeedGeometry:
    """Resolved endpoints for one directed two-terminal series branch."""

    upstream_feed: Point
    upstream_pin: Point
    downstream_pin: Point
    downstream_feed: Point


@dataclass(frozen=True)
class QualityFinding:
    """One machine-readable geometric concern for review, not an ERC result."""

    code: str
    module_ref: str
    symbol_ids: tuple[str, ...]
    measured: float
    threshold: float
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "measured": round(self.measured, 4),
            "message": self.message,
            "module_ref": self.module_ref,
            "symbol_ids": list(self.symbol_ids),
            "threshold": self.threshold,
        }
