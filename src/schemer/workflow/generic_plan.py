from __future__ import annotations

import re
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

from schemer.analysis.visibility import focus_module
from schemer.core.errors import ToolchainError
from schemer.core.layout import LayoutPlan, ModuleLayout, Position, resolve_module_position_ids
from schemer.integration.toolchain import Toolchain, evaluate_zener
from schemer.integration.viewer import auto_place_schematic

AutoPlacer = Callable[[dict[str, Any], Toolchain], dict[str, Position]]
Evaluator = Callable[[Path, Path], dict[str, Any]]


def _signature_net_bindings(instance: dict[str, Any]) -> dict[str, str]:
    """Return module parameter names mapped to their evaluated net names."""

    attributes = instance.get("attributes")
    signature = attributes.get("__signature") if isinstance(attributes, dict) else None
    payload = signature.get("Json") if isinstance(signature, dict) else None
    parameters = payload.get("parameters") if isinstance(payload, dict) else None
    if not isinstance(parameters, list):
        return {}
    result: dict[str, str] = {}
    for parameter in parameters:
        if not isinstance(parameter, dict) or not isinstance(parameter.get("name"), str):
            continue
        value = parameter.get("value")
        net = value.get("Net") if isinstance(value, dict) else None
        name = net.get("name") if isinstance(net, dict) else None
        if isinstance(name, str):
            result[parameter["name"]] = name
    return result


def _configured_source_net_names(
    source_schematic: dict[str, Any], target_schematic: dict[str, Any], target_ref: str,
) -> dict[str, str]:
    """Map configured parent net names back to the child source's port names."""

    source_root = source_schematic.get("root_ref")
    source_instances = source_schematic.get("instances")
    target_instances = target_schematic.get("instances")
    source = source_instances.get(source_root) if isinstance(source_instances, dict) else None
    target = target_instances.get(target_ref) if isinstance(target_instances, dict) else None
    if not isinstance(source, dict) or not isinstance(target, dict):
        return {}
    source_bindings = _signature_net_bindings(source)
    target_bindings = _signature_net_bindings(target)
    return {
        target_name: source_bindings[parameter]
        for parameter, target_name in target_bindings.items()
        if parameter in source_bindings and source_bindings[parameter] != target_name
    }


def _natural_key(value: str) -> tuple[object, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)
    )


def _reference_family(reference: str) -> str:
    match = re.match(r"[^0-9]+", reference)
    return match.group(0).casefold() if match else reference.casefold()


def _component_groups(
    schematic: dict[str, Any], module_ref: str
) -> dict[tuple[str, str, str], list[tuple[str, str]]]:
    """Index physical descendants by generic type and reference family."""

    instances = schematic.get("instances")
    if not isinstance(instances, dict):
        raise ToolchainError("schematic instances are invalid")
    prefix = module_ref + "."
    groups: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for instance_ref, instance in instances.items():
        if (
            not isinstance(instance_ref, str)
            or not instance_ref.startswith(prefix)
            or not isinstance(instance, dict)
            or instance.get("kind") != "Component"
        ):
            continue
        reference = instance.get("reference_designator")
        type_ref = instance.get("type_ref")
        if not isinstance(reference, str) or not isinstance(type_ref, dict):
            raise ToolchainError(f"physical component lacks evaluated identity: {instance_ref}")
        source_path = type_ref.get("source_path")
        module_name = type_ref.get("module_name")
        if not isinstance(source_path, str) or not isinstance(module_name, str):
            raise ToolchainError(f"physical component lacks evaluated type: {instance_ref}")
        relative_ref = instance_ref.removeprefix(prefix)
        key = (_reference_family(reference), str(Path(source_path).resolve()), module_name)
        groups.setdefault(key, []).append((reference, relative_ref))
    for group in groups.values():
        group.sort(key=lambda item: (_natural_key(item[0]), _natural_key(item[1])))
    return groups


def _translate_component_ids(
    positions: dict[str, Position],
    source_schematic: dict[str, Any],
    target_schematic: dict[str, Any],
    target_module_ref: str,
) -> dict[str, Position]:
    """Translate source-local physical IDs to one configured parent instance."""

    source_root = source_schematic.get("root_ref")
    if not isinstance(source_root, str):
        raise ToolchainError("module evaluation has no root reference")
    source_groups = _component_groups(source_schematic, source_root)
    target_groups = _component_groups(target_schematic, target_module_ref)
    if source_groups.keys() != target_groups.keys() or any(
        len(source_groups[key]) != len(target_groups[key]) for key in source_groups
    ):
        raise ToolchainError("standalone and instantiated module components do not correspond")

    replacements: dict[str, str] = {}
    for key in source_groups:
        for (_, source_relative), (_, target_relative) in zip(
            source_groups[key], target_groups[key], strict=True
        ):
            replacements[f"comp:{source_relative}"] = f"comp:{target_relative}"

    translated: dict[str, Position] = {}
    for symbol_id, position in positions.items():
        translated_id = symbol_id
        if symbol_id.startswith("comp:"):
            for source_id, target_id in replacements.items():
                if symbol_id == source_id or symbol_id.startswith(source_id + "@"):
                    translated_id = target_id + symbol_id.removeprefix(source_id)
                    break
            else:
                raise ToolchainError(
                    f"viewer component does not resolve in configured module: {symbol_id}"
                )
        if translated_id in translated:
            raise ToolchainError(f"multiple viewer positions resolve to {translated_id}")
        translated[translated_id] = position
    return translated


def _module_source_path(
    schematic: dict[str, Any], instance_ref: str, *, root_entrypoint: Path
) -> Path:
    if instance_ref == schematic.get("root_ref"):
        return root_entrypoint
    instances = schematic.get("instances")
    instance = instances.get(instance_ref) if isinstance(instances, dict) else None
    type_ref = instance.get("type_ref") if isinstance(instance, dict) else None
    source_path = type_ref.get("source_path") if isinstance(type_ref, dict) else None
    if not isinstance(source_path, str):
        raise ToolchainError(f"layout module has no source path: {instance_ref}")
    return Path(source_path).expanduser().resolve()


def _fresh_module_view(schematic: dict[str, Any], instance_ref: str) -> dict[str, Any]:
    """Focus one module and discard only its stored placement before auto-place."""

    focused = (
        deepcopy(schematic)
        if instance_ref == schematic.get("root_ref")
        else focus_module(schematic, instance_ref)
    )
    instances = focused.get("instances")
    module = instances.get(instance_ref) if isinstance(instances, dict) else None
    if not isinstance(module, dict):
        raise ToolchainError(f"layout module is absent from schematic: {instance_ref}")
    module["symbol_positions"] = {}
    return focused


def generic_layout_plan(
    entrypoint: Path,
    schematic: dict[str, Any],
    toolchain: Toolchain,
    *,
    auto_placer: AutoPlacer = auto_place_schematic,
    evaluator: Evaluator = evaluate_zener,
) -> LayoutPlan:
    """Seed the root and its visible opaque child blocks without design knowledge.

    A descendant receives its own source layout exactly when the root viewer
    represents it as one opaque ``comp:<path>`` block. Transparent
    wrappers remain part of their parent's sheet. The rule depends only on the
    evaluated hierarchy and viewer output.
    """

    entrypoint = entrypoint.expanduser().resolve()
    root_ref = schematic.get("root_ref")
    instances = schematic.get("instances")
    if not isinstance(root_ref, str) or not isinstance(instances, dict):
        raise ToolchainError("schematic root or instances are invalid")
    root = instances.get(root_ref)
    children = root.get("children") if isinstance(root, dict) else None
    if not isinstance(children, dict):
        raise ToolchainError("schematic root children are invalid")

    root_positions = auto_placer(_fresh_module_view(schematic, root_ref), toolchain)
    modules = [ModuleLayout(root_ref, entrypoint, root_positions)]
    seen_sources = {entrypoint}

    for symbol_id in root_positions:
        if not symbol_id.startswith("comp:") or "@" in symbol_id:
            continue
        child_ref = root_ref + "." + symbol_id.removeprefix("comp:")
        child = instances.get(child_ref)
        if not isinstance(child, dict) or child.get("kind") == "Component":
            continue
        source_path = _module_source_path(
            schematic,
            child_ref,
            root_entrypoint=entrypoint,
        )
        if source_path in seen_sources:
            continue
        source_schematic: dict[str, Any] | None = None
        try:
            source_schematic = evaluator(source_path, toolchain.compiler)
        except ToolchainError:
            positions = auto_placer(_fresh_module_view(schematic, child_ref), toolchain)
        else:
            source_root = source_schematic.get("root_ref")
            if not isinstance(source_root, str):
                raise ToolchainError("module evaluation has no root reference")
            source_positions = auto_placer(
                _fresh_module_view(source_schematic, source_root),
                toolchain,
            )
            try:
                positions = _translate_component_ids(
                    source_positions,
                    source_schematic,
                    schematic,
                    child_ref,
                )
                # Parent wiring can rename or merge a child's standalone nets.
                # Use the configured view if its standalone rail IDs no longer resolve.
                resolve_module_position_ids(
                    ModuleLayout(child_ref, source_path, positions), schematic,
                )
            except ToolchainError:
                positions = auto_placer(_fresh_module_view(schematic, child_ref), toolchain)
        source_net_names = (
            _configured_source_net_names(source_schematic, schematic, child_ref)
            if source_schematic is not None
            else {}
        )
        modules.append(ModuleLayout(
            child_ref, source_path, positions, source_net_names=source_net_names,
        ))
        seen_sources.add(source_path)

    return LayoutPlan(tuple(modules))
