"""Check the private AstrBot runner surface used by the compatibility guard."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


def _class_node(module: ast.Module, name: str) -> ast.ClassDef:
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"missing class {name}")


def _method_node(
    class_node: ast.ClassDef,
    name: str,
    expected_type: type[ast.FunctionDef] | type[ast.AsyncFunctionDef],
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in class_node.body:
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            if not isinstance(node, expected_type):
                raise AssertionError(f"{class_node.name}.{name} changed function kind")
            return node
    raise AssertionError(f"missing {class_node.name}.{name}")


def _self_attribute_assignment_lines(node: ast.AST, attribute: str) -> list[int]:
    lines: list[int] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        for target in child.targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr == attribute
            ):
                lines.append(child.lineno)
    return lines


def _self_method_call_lines(node: ast.AST, method: str) -> list[int]:
    lines: list[int] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
            continue
        if (
            isinstance(child.func.value, ast.Name)
            and child.func.value.id == "self"
            and child.func.attr == method
        ):
            lines.append(child.lineno)
    return lines


def _base_class_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _class_inherits_method(
    classes: dict[str, ast.ClassDef],
    class_name: str,
    method_name: str,
    seen: set[str] | None = None,
) -> bool:
    visited = set() if seen is None else seen
    if class_name in visited:
        return False
    visited.add(class_name)
    class_node = classes.get(class_name)
    if class_node is None:
        return False
    if any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
        for node in class_node.body
    ):
        return True
    return any(
        base_name is not None
        and _class_inherits_method(classes, base_name, method_name, visited)
        for base_name in (_base_class_name(base) for base in class_node.bases)
    )


def _find_class(package_root: Path, class_name: str) -> ast.ClassDef:
    for path in package_root.rglob("*.py"):
        try:
            module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node
    raise AssertionError(f"missing class {class_name} in {package_root}")


def _declared_fields(class_node: ast.ClassDef) -> set[str]:
    fields: set[str] = set()
    for node in class_node.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            fields.add(node.target.id)
        elif isinstance(node, ast.Assign):
            fields.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    return fields


def check_contract(astrbot_root: Path) -> None:
    """Fail when a supported AstrBot source tree no longer matches the guard."""
    package_root = astrbot_root.resolve() / "astrbot"
    runner_path = (
        package_root / "core" / "agent" / "runners" / "tool_loop_agent_runner.py"
    )
    tool_path = package_root / "core" / "agent" / "tool.py"
    message_path = package_root / "core" / "agent" / "message.py"
    for required in (runner_path, tool_path, message_path):
        if not required.is_file():
            raise AssertionError(f"missing AstrBot runtime source: {required}")

    runner_text = runner_path.read_text(encoding="utf-8")
    runner_module = ast.parse(runner_text, filename=str(runner_path))
    runner_class = _class_node(runner_module, "ToolLoopAgentRunner")
    generation = _method_node(runner_class, "_iter_llm_responses", ast.AsyncFunctionDef)
    _method_node(runner_class, "_func_tool_for_provider", ast.FunctionDef)
    if not _self_method_call_lines(generation, "_func_tool_for_provider"):
        raise AssertionError(
            "generation no longer resolves tools through guarded provider method"
        )
    fallback = _method_node(
        runner_class,
        "_iter_llm_responses_with_fallback",
        ast.AsyncFunctionDef,
    )
    provider_assignments = _self_attribute_assignment_lines(fallback, "provider")
    guarded_calls = _self_method_call_lines(fallback, "_iter_llm_responses")
    if not provider_assignments or not guarded_calls:
        raise AssertionError(
            "fallback no longer switches provider through guarded method"
        )
    if min(provider_assignments) >= max(guarded_calls):
        raise AssertionError(
            "fallback provider is not selected before guarded generation"
        )
    if "_skill_like_raw_tool_set" not in runner_text:
        raise AssertionError("skills-like raw ToolSet marker is no longer available")

    tool_module = ast.parse(
        tool_path.read_text(encoding="utf-8"), filename=str(tool_path)
    )
    tool_class = _class_node(tool_module, "ToolSet")
    _method_node(tool_class, "get_light_tool_set", ast.FunctionDef)
    _method_node(tool_class, "get_param_only_tool_set", ast.FunctionDef)

    message_module = ast.parse(
        message_path.read_text(encoding="utf-8"), filename=str(message_path)
    )
    message_classes = {
        node.name: node
        for node in message_module.body
        if isinstance(node, ast.ClassDef)
    }
    if not _class_inherits_method(message_classes, "TextPart", "mark_as_temp"):
        raise AssertionError("TextPart no longer inherits mark_as_temp")

    provider_request = _find_class(package_root / "core", "ProviderRequest")
    required_fields = {
        "extra_user_content_parts",
        "func_tool",
        "model",
        "system_prompt",
    }
    missing_fields = required_fields.difference(_declared_fields(provider_request))
    if missing_fields:
        raise AssertionError(
            "ProviderRequest fields unavailable: " + ", ".join(sorted(missing_fields))
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("astrbot_root", type=Path)
    arguments = parser.parse_args()
    check_contract(arguments.astrbot_root)
    print(f"AstrBot runner contract OK: {arguments.astrbot_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
