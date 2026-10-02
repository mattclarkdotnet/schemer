"""Stable fixture roots independent of a test module's package depth."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
