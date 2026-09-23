"""Authored schematic roles carried by Zener component-module properties."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from schemer.toolchain import ToolchainError


class _RoleSource(Protocol):
    ref: str
    instance: dict[str, Any]


@dataclass(frozen=True)
class RoleSource:
    ref: str
    instance: dict[str, Any]


@dataclass(frozen=True)
class ComponentRole:
    """Non-geometric design intent attached to one physical component."""

    component_ref: str
    kind: str
    group: str
    order: int | None = None
    at: str | None = None
    owner: str | None = None
    pin: str | None = None
    return_pin: str | None = None
    other_pin: str | None = None


@dataclass(frozen=True)
class ModuleFunction:
    """The authored purpose of one module instance."""

    instance_ref: str
    function: str


def schematic_properties(source: dict[str, Any]) -> dict[str, Any] | None:
    attributes = source.get("attributes")
    raw = attributes.get("schematic_properties") if isinstance(attributes, dict) else None
    if raw is None:
        return None
    if isinstance(raw, dict) and isinstance(raw.get("Json"), dict):
        payload = raw["Json"]
    elif isinstance(raw, dict) and isinstance(raw.get("String"), str):
        try:
            payload = json.loads(raw["String"])
        except json.JSONDecodeError as error:
            raise ToolchainError("schematic_properties is not valid JSON") from error
    else:
        raise ToolchainError("schematic_properties must evaluate to an object")
    if not isinstance(payload, dict):
        raise ToolchainError("schematic_properties must evaluate to an object")
    return payload


def module_functions(instances: dict[str, Any]) -> tuple[ModuleFunction, ...]:
    """Read contextual functions declared on module instantiation calls."""

    result = []
    for instance_ref, instance in instances.items():
        if not isinstance(instance, dict):
            continue
        payload = schematic_properties(instance)
        if payload is None or "role" in payload or "function" not in payload:
            continue
        unknown = set(payload) - {"function"}
        if unknown:
            raise ToolchainError(
                f"{instance_ref}: unsupported module schematic_properties fields: "
                + ", ".join(sorted(unknown))
            )
        function = payload.get("function")
        if instance.get("kind") != "Module" or not isinstance(function, str) or not function:
            raise ToolchainError(
                f"{instance_ref}: module schematic_properties requires a function"
            )
        result.append(ModuleFunction(instance_ref, function))
    return tuple(result)


def component_roles(
    instances: dict[str, Any], components: tuple[_RoleSource, ...],
) -> tuple[ComponentRole, ...]:
    """Read and strictly validate roles from component or wrapper attributes."""

    result = []
    for component in components:
        wrapper = instances.get(component.ref.rsplit(".", 1)[0], {})
        sources = (component.instance, wrapper if isinstance(wrapper, dict) else {})
        payload = next((
            found
            for source in sources
            if (found := schematic_properties(source)) is not None and "role" in found
        ), None)
        if payload is None:
            continue
        unknown = set(payload) - {"role", "group", "order", "at", "owner", "pin",
                                  "return_pin", "other_pin"}
        if unknown:
            raise ToolchainError(
                f"{component.ref}: unsupported component schematic_properties fields: "
                + ", ".join(sorted(unknown))
            )
        kind = payload.get("role")
        group = payload.get("group")
        if not all(isinstance(item, str) and item for item in (kind, group)):
            raise ToolchainError(
                f"{component.ref}: schematic role requires a group"
            )
        for field in ("owner", "pin", "at", "return_pin", "other_pin"):
            if field in payload and (not isinstance(payload[field], str) or not payload[field]):
                raise ToolchainError(f"{component.ref}: {field} must be a non-empty string")
        if payload.get("return_pin") and kind != "shunt":
            raise ToolchainError(f"{component.ref}: return_pin requires a shunt role")
        if payload.get("other_pin") and kind != "pin-bridge":
            raise ToolchainError(f"{component.ref}: other_pin requires a pin-bridge role")
        if kind == "series":
            raw_order = payload.get("order")
            order = (
                raw_order
                if isinstance(raw_order, int) and raw_order >= 0
                else int(raw_order)
                if isinstance(raw_order, str) and raw_order.isdigit()
                else None
            )
            if (
                order is None
                or payload.get("at")
                or bool(payload.get("owner")) != bool(payload.get("pin"))
            ):
                raise ToolchainError(
                    f"{component.ref}: series role requires a non-negative order and no at net"
                )
            result.append(ComponentRole(
                component.ref, kind, group, order=order,
                owner=payload.get("owner"), pin=payload.get("pin"),
            ))
        elif kind == "shunt":
            at = payload.get("at")
            pin = payload.get("pin")
            owner = payload.get("owner")
            if (
                bool(at) == bool(pin)
                or not isinstance(at or pin, str)
                or payload.get("order") is not None
                or (pin and not owner)
                or (payload.get("return_pin") and not owner)
            ):
                raise ToolchainError(
                    f"{component.ref}: shunt role requires an at net or owned pin, and no order"
                )
            result.append(ComponentRole(
                component.ref, kind, group, at=at, owner=owner, pin=pin,
                return_pin=payload.get("return_pin"),
            ))
        elif kind == "pin-bridge":
            if (not all(isinstance(payload.get(key), str) and payload[key]
                        for key in ("owner", "pin", "other_pin"))
                    or payload.get("at") or payload.get("order") is not None):
                raise ToolchainError(f"{component.ref}: pin-bridge requires owner and two pins")
            result.append(ComponentRole(
                component.ref, kind, group, owner=payload["owner"], pin=payload["pin"],
                other_pin=payload["other_pin"],
            ))
        elif kind in {
            "pullup",
            "pulldown",
            "bypass",
            "power-feed",
            "series-termination",
            "current-limit",
            "ac-coupling",
            "source-impedance",
        }:
            owner = payload.get("owner")
            pin = payload.get("pin")
            if (
                not isinstance(owner, str)
                or not owner
                or not isinstance(pin, str)
                or not pin
                or payload.get("order") is not None
                or payload.get("at")
            ):
                raise ToolchainError(
                    f"{component.ref}: {kind} role requires owner and pin, "
                    "and no order or at net"
                )
            result.append(ComponentRole(
                component.ref, kind, group, owner=owner, pin=pin,
            ))
        elif kind == "divider":
            raw_order = payload.get("order")
            order = (
                raw_order
                if isinstance(raw_order, int) and raw_order >= 0
                else int(raw_order)
                if isinstance(raw_order, str) and raw_order.isdigit()
                else None
            )
            owner = payload.get("owner")
            if (
                order is None
                or not isinstance(owner, str)
                or not owner
                or payload.get("at")
                or payload.get("pin")
            ):
                raise ToolchainError(
                    f"{component.ref}: divider role requires owner and non-negative order"
                )
            result.append(ComponentRole(
                component.ref, kind, group, order=order, owner=owner,
            ))
        else:
            raise ToolchainError(f"{component.ref}: unsupported schematic role {kind!r}")
    return tuple(result)
