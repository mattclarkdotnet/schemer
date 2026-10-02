from __future__ import annotations


class KiCadSchematicError(ValueError):
    """The schematic is malformed or lacks an object required by an edit."""


class ToolchainError(RuntimeError):
    """A local compiler or viewer dependency is missing or unusable."""
