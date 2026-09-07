"""Combined semantic and hierarchical schematic quality findings."""

from __future__ import annotations

from itertools import combinations
from typing import Any

from schemer.layout_metrics import (
    Envelope,
    placed_component_bodies,
    top_level_group_envelopes,
)
from schemer.symbol_geometry import QualityFinding, schematic_quality_findings
from schemer.toolchain import ToolchainError


def top_level_block_envelopes(schematic: dict[str, Any]) -> dict[str, Envelope]:
    """Return the same complete group envelopes used by the final packer."""

    return top_level_group_envelopes(schematic)


def top_level_signal_adjacencies(
    schematic: dict[str, Any],
) -> set[frozenset[str]]:
    """Return pairs of top-level blocks joined by an ordinary signal net."""

    root_ref = schematic.get("root_ref")
    nets = schematic.get("nets")
    if not isinstance(root_ref, str) or not isinstance(nets, dict):
        raise ToolchainError("schematic root or nets are invalid")

    adjacencies: set[frozenset[str]] = set()
    for net in nets.values():
        if not isinstance(net, dict) or net.get("kind") != "Net":
            continue
        ports = net.get("ports")
        if not isinstance(ports, list):
            continue
        blocks = {
            port.removeprefix(root_ref + ".").split(".", 1)[0]
            for port in ports
            if isinstance(port, str) and port.startswith(root_ref + ".")
        }
        if len(blocks) == 2:
            adjacencies.add(frozenset(blocks))
    return adjacencies


def top_level_block_clearance_findings(
    schematic: dict[str, Any],
    *,
    minimum_clearance: float = 100.0,
) -> tuple[QualityFinding, ...]:
    """Report signal-connected side-by-side blocks with no annotation corridor."""

    if minimum_clearance < 0:
        raise ToolchainError("minimum top-level block clearance must be non-negative")
    root_ref = schematic.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic root reference is invalid")
    envelopes = top_level_block_envelopes(schematic)
    findings: list[QualityFinding] = []
    for adjacency in sorted(top_level_signal_adjacencies(schematic), key=sorted):
        if len(adjacency) != 2:
            continue
        first_name, second_name = sorted(adjacency)
        first = envelopes.get(first_name)
        second = envelopes.get(second_name)
        if first is None or second is None:
            continue
        if first.max_x <= second.min_x:
            clearance = second.min_x - first.max_x
        elif second.max_x <= first.min_x:
            clearance = first.min_x - second.max_x
        else:
            # Stacked blocks and true overlaps are handled by their own rules.
            continue
        if clearance + 1e-6 >= minimum_clearance:
            continue
        findings.append(
            QualityFinding(
                code="top-level-block-clearance",
                module_ref=root_ref,
                symbol_ids=(f"comp:{first_name}", f"comp:{second_name}"),
                measured=clearance,
                threshold=minimum_clearance,
                message="signal-connected functional blocks lack a readable corridor",
            )
        )
    return tuple(findings)


def top_level_block_overlap_findings(
    schematic: dict[str, Any],
    *,
    overlap_tolerance: float = 1.0,
) -> tuple[QualityFinding, ...]:
    """Report top-level blocks whose physical symbol envelopes overlap."""

    root_ref = schematic.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic root reference is invalid")
    envelopes = top_level_block_envelopes(schematic)
    findings: list[QualityFinding] = []
    for first_name, second_name in combinations(sorted(envelopes), 2):
        first = envelopes[first_name]
        second = envelopes[second_name]
        overlap_x = min(first.max_x, second.max_x) - max(first.min_x, second.min_x)
        overlap_y = min(first.max_y, second.max_y) - max(first.min_y, second.min_y)
        if overlap_x <= overlap_tolerance or overlap_y <= overlap_tolerance:
            continue
        findings.append(
            QualityFinding(
                code="top-level-block-overlap",
                module_ref=root_ref,
                symbol_ids=(f"comp:{first_name}", f"comp:{second_name}"),
                measured=min(overlap_x, overlap_y),
                threshold=overlap_tolerance,
                message="top-level functional block envelopes overlap",
            )
        )
    return tuple(findings)


def component_body_overlap_findings(
    schematic: dict[str, Any],
    *,
    overlap_tolerance: float = 0.0,
) -> tuple[QualityFinding, ...]:
    """Report any painted-body overlap between distinct physical components.

    This deliberately excludes pin strokes and endpoints because those are not
    body geometry. Pin collisions remain a separate wire-path defect; any
    positive-area collision between painted component bodies is a hard finding.
    """

    if overlap_tolerance < 0:
        raise ToolchainError("component body overlap tolerance must be non-negative")

    root_ref = schematic.get("root_ref")
    if not isinstance(root_ref, str):
        raise ToolchainError("schematic root reference is invalid")
    findings: list[QualityFinding] = []
    reported_pairs: set[tuple[str, str]] = set()
    for first, second in combinations(placed_component_bodies(schematic), 2):
        if first.instance_ref == second.instance_ref:
            continue
        overlap_x = min(first.envelope.max_x, second.envelope.max_x) - max(
            first.envelope.min_x, second.envelope.min_x
        )
        overlap_y = min(first.envelope.max_y, second.envelope.max_y) - max(
            first.envelope.min_y, second.envelope.min_y
        )
        if overlap_x <= overlap_tolerance or overlap_y <= overlap_tolerance:
            continue
        pair = tuple(sorted((first.instance_ref, second.instance_ref)))
        if pair in reported_pairs:
            continue
        reported_pairs.add(pair)
        findings.append(
            QualityFinding(
                code="component-body-overlap",
                module_ref=root_ref,
                symbol_ids=pair,
                measured=min(overlap_x, overlap_y),
                threshold=overlap_tolerance,
                message="distinct physical component bodies overlap",
            )
        )
    return tuple(findings)


def require_no_component_body_overlaps(schematic: dict[str, Any]) -> None:
    """Reject a review candidate containing any painted-body collision."""

    findings = component_body_overlap_findings(schematic)
    if not findings:
        return
    first = findings[0]
    raise ToolchainError(
        "schematic contains overlapping physical components: " + " and ".join(first.symbol_ids)
    )


def require_no_top_level_block_overlaps(schematic: dict[str, Any]) -> None:
    """Reject a review candidate whose functional block envelopes overlap."""

    findings = top_level_block_overlap_findings(schematic)
    if not findings:
        return
    first = findings[0]
    raise ToolchainError("top-level functional blocks overlap: " + " and ".join(first.symbol_ids))


def require_top_level_block_clearance(
    schematic: dict[str, Any],
    *,
    minimum_clearance: float = 100.0,
) -> None:
    """Reject a review candidate whose connected blocks are visually jammed."""

    findings = top_level_block_clearance_findings(
        schematic,
        minimum_clearance=minimum_clearance,
    )
    if not findings:
        return
    first = findings[0]
    raise ToolchainError(
        "signal-connected top-level blocks lack clearance: " + " and ".join(first.symbol_ids)
    )


def require_no_internal_signal_net_symbols(schematic: dict[str, Any]) -> None:
    """Reject label fragments where an entirely local signal should be wired."""

    findings = tuple(
        finding
        for finding in schematic_quality_findings(schematic)
        if finding.code == "internal-signal-net-symbol"
    )
    if not findings:
        return
    first = findings[0]
    raise ToolchainError(
        "entirely local signal net is split by a label: " + " and ".join(first.symbol_ids)
    )


def require_no_detached_connected_components(schematic: dict[str, Any]) -> None:
    """Reject bodies or signal terminals outside the visual attachment radius."""

    findings = tuple(
        finding
        for finding in schematic_quality_findings(schematic)
        if finding.code in {"detached-connected-component", "detached-local-signal"}
    )
    if not findings:
        return
    first = findings[0]
    raise ToolchainError(
        "connected component or signal terminal has no nearby visual peer: "
        + " and ".join(first.symbol_ids)
    )


def all_quality_findings(schematic: dict[str, Any]) -> tuple[QualityFinding, ...]:
    """Return all generic local-geometry and block-hierarchy findings."""

    findings = (
        *schematic_quality_findings(schematic),
        *component_body_overlap_findings(schematic),
        *top_level_block_overlap_findings(schematic),
        *top_level_block_clearance_findings(schematic),
    )
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                finding.module_ref,
                finding.code,
                finding.symbol_ids,
            ),
        )
    )
