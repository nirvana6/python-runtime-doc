"""Resolve Python runtime documentation for a qualified expression."""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class UnresolvedExpression(Exception):
    """Raised when an expression cannot be resolved from imports or source."""


@dataclass(frozen=True)
class Source:
    path: Path
    tree: ast.Module
    aliases: dict[str, str]


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


def _target_names(node: ast.expr) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        names: set[str] = set()
        for element in node.elts:
            names.update(_target_names(element))
        return names
    return set()


def _import_aliases(tree: ast.Module) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                parts = item.name.split(".")
                binding = item.asname or parts[0]
                aliases[binding] = item.name if item.asname else parts[0]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{node.module}.{item.name}"
    return aliases


def _load_source(path: str | Path) -> Source:
    source_path = Path(path)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    return Source(source_path, tree, _import_aliases(tree))


def _expand_alias(name: str, aliases: dict[str, str]) -> str:
    parts = name.split(".")
    return ".".join([aliases.get(parts[0], parts[0]), *parts[1:]])


def _resolve_imported_name(name: str, aliases: dict[str, str]) -> Any | None:
    expanded = _expand_alias(name, aliases)
    parts = expanded.split(".")
    for split_at in range(len(parts), 0, -1):
        module_name = ".".join(parts[:split_at])
        try:
            value: Any = importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # A missing transitive dependency is a real import failure; only
            # try a shorter module prefix when the requested prefix is absent.
            if exc.name and (module_name == exc.name or module_name.startswith(exc.name + ".")):
                continue
            return None
        except ImportError:
            continue
        try:
            for attribute in parts[split_at:]:
                value = getattr(value, attribute)
        except AttributeError:
            continue
        return value
    return None


def _scope_nodes(tree: ast.Module, line: int) -> tuple[ast.AST, ast.ClassDef | None]:
    containing: list[ast.AST] = []
    for node in ast.walk(tree):
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if isinstance(start, int) and isinstance(end, int) and start <= line <= end:
            containing.append(node)

    functions = [
        node
        for node in containing
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    ]
    classes = [node for node in containing if isinstance(node, ast.ClassDef)]

    def span(node: ast.AST) -> int:
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if not isinstance(start, int) or not isinstance(end, int):
            return sys.maxsize
        return end - start

    scope: ast.AST = min(functions, key=span) if functions else tree
    if not functions and classes:
        scope = min(classes, key=span)
    owner = min(classes, key=span) if classes else None
    return scope, owner


def _walk_scope(node: ast.AST):
    """Walk one lexical scope without entering nested scopes."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield from _walk_scope(child)


def _annotation_type(annotation: ast.expr | None, source: Source) -> type[Any] | None:
    if annotation is None:
        return None
    name = _dotted_name(annotation)
    if name:
        candidate = _resolve_imported_name(name, source.aliases)
        if isinstance(candidate, type):
            return candidate
        builtin = getattr(builtins, name, None)
        return builtin if isinstance(builtin, type) else None
    if isinstance(annotation, ast.Subscript):
        return _annotation_type(annotation.value, source)
    return None


def _expression_type(expression: ast.expr | None, source: Source) -> type[Any] | None:
    if expression is None:
        return None
    if isinstance(expression, (ast.List, ast.ListComp)):
        return list
    if isinstance(expression, (ast.Dict, ast.DictComp)):
        return dict
    if isinstance(expression, (ast.Set, ast.SetComp)):
        return set
    if isinstance(expression, ast.Tuple):
        return tuple
    if isinstance(expression, ast.Constant):
        return type(expression.value)
    if isinstance(expression, ast.Call):
        name = _dotted_name(expression.func)
        if name:
            candidate = getattr(builtins, name, None) if "." not in name else None
            if not isinstance(candidate, type):
                candidate = _resolve_imported_name(name, source.aliases)
            return candidate if isinstance(candidate, type) else None
    if isinstance(expression, ast.IfExp):
        left = _expression_type(expression.body, source)
        right = _expression_type(expression.orelse, source)
        return left if left is right else None
    return None


def _assignment_for_name(scope: ast.AST, name: str, line: int) -> ast.AST | None:
    candidates: list[ast.AST] = []
    for node in _walk_scope(scope):
        if getattr(node, "lineno", line + 1) >= line:
            continue
        is_assignment = isinstance(node, ast.Assign) and any(
            name in _target_names(target) for target in node.targets
        )
        is_annotated_assignment = isinstance(node, ast.AnnAssign) and name in _target_names(node.target)
        is_named_expression = isinstance(node, ast.NamedExpr) and name in _target_names(node.target)
        if is_assignment or is_annotated_assignment or is_named_expression:
            candidates.append(node)
    return max(candidates, key=lambda item: item.lineno, default=None)


def _infer_name_type(name: str, source: Source, line: int, scope: ast.AST, owner: ast.ClassDef | None) -> type[Any] | None:
    if name in {"self", "cls"} and owner is not None:
        return owner

    assignment = _assignment_for_name(scope, name, line)
    if isinstance(assignment, ast.Assign):
        return _expression_type(assignment.value, source)
    if isinstance(assignment, ast.AnnAssign):
        inferred = _expression_type(assignment.value, source)
        return inferred or _annotation_type(assignment.annotation, source)
    if isinstance(assignment, ast.NamedExpr):
        return _expression_type(assignment.value, source)

    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        arguments = scope.args
        all_args = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        if arguments.vararg:
            all_args.append(arguments.vararg)
        if arguments.kwarg:
            all_args.append(arguments.kwarg)
        for argument in all_args:
            if argument.arg == name:
                return _annotation_type(argument.annotation, source)

    if scope is not source.tree:
        module_assignment = _assignment_for_name(source.tree, name, line)
        if isinstance(module_assignment, ast.Assign):
            return _expression_type(module_assignment.value, source)
        if isinstance(module_assignment, ast.AnnAssign):
            inferred = _expression_type(module_assignment.value, source)
            return inferred or _annotation_type(module_assignment.annotation, source)
    return None


def _infer_receiver_type(expression: str, source: Source, line: int) -> type[Any] | None:
    scope, owner = _scope_nodes(source.tree, line)
    parts = expression.split(".")
    receiver_type = _infer_name_type(parts[0], source, line, scope, owner)
    if receiver_type is None:
        return None

    for attribute in parts[1:]:
        try:
            descriptor = inspect.getattr_static(receiver_type, attribute)
        except AttributeError:
            descriptor = None
        if isinstance(descriptor, property):
            annotation = getattr(descriptor.fget, "__annotations__", {}).get("return") if descriptor.fget else None
            receiver_type = annotation if isinstance(annotation, type) else None
        elif descriptor is None:
            annotation = getattr(receiver_type, "__annotations__", {}).get(attribute)
            receiver_type = annotation if isinstance(annotation, type) else None
        else:
            member = getattr(receiver_type, attribute, None)
            if isinstance(member, type):
                receiver_type = member
            else:
                annotation = getattr(receiver_type, "__annotations__", {}).get(attribute)
                receiver_type = annotation if isinstance(annotation, type) else None
        if receiver_type is None:
            return None
    return receiver_type


def _format_method(type_obj: type[Any], method_name: str, expression: str) -> str:
    descriptor = inspect.getattr_static(type_obj, method_name)
    method = getattr(type_obj, method_name)
    if not callable(method):
        raise UnresolvedExpression(f"{expression!r} is not a callable method")

    title = f"{type_obj.__module__}.{type_obj.__qualname__}.{method_name}"
    lines = [f"{title}    <method of {type_obj.__qualname__}>", "-" * max(20, min(len(title) + 20, 78)), ""]
    try:
        signature = inspect.signature(method)
        if not isinstance(descriptor, (staticmethod, classmethod)):
            parameters = list(signature.parameters.values())
            if parameters and parameters[0].kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                signature = signature.replace(parameters=parameters[1:])
        lines.append(f"{method_name}{signature}")
    except (TypeError, ValueError):
        pass

    doc = inspect.getdoc(method) or inspect.getdoc(descriptor)
    if doc:
        lines.extend(["", *doc.splitlines()])
    else:
        lines.append("(No docstring available.)")
    return "\n".join(lines)


def _resolve_dotted_module(expression: str, source: Source) -> str | None:
    parts = expression.split(".")
    if len(parts) < 2 or parts[0] not in source.aliases:
        return None
    value = _resolve_imported_name(expression, source.aliases)
    if value is None:
        return None
    kind = type(value).__name__
    title = _expand_alias(expression, source.aliases)
    doc = inspect.getdoc(value)
    try:
        signature = inspect.signature(value)
    except (TypeError, ValueError):
        signature = None
    lines = [f"{title}    <{kind}>", "-" * max(20, min(len(title) + len(kind) + 8, 78)), ""]
    if signature is not None:
        lines.append(f"{parts[-1]}{signature}")
    if doc:
        lines.extend(["", *doc.splitlines()])
    elif signature is None:
        lines.append("(No docstring or signature available.)")
    return "\n".join(lines)


def lookup(path: str | Path, expression: str, line: int) -> str:
    source = _load_source(path)
    expression = expression.strip().strip(".")
    parts = expression.split(".")

    if len(parts) > 1:
        imported_result = _resolve_dotted_module(expression, source)
        if imported_result is not None:
            return imported_result

        receiver_type = _infer_receiver_type(".".join(parts[:-1]), source, line)
        if receiver_type is not None:
            try:
                return _format_method(receiver_type, parts[-1], expression)
            except (AttributeError, TypeError, UnresolvedExpression) as exc:
                raise UnresolvedExpression(f"Cannot resolve method {expression!r}: {exc}") from exc
        raise UnresolvedExpression(
            f"Cannot infer the receiver type in {expression!r}. "
            "Add a simple assignment or type annotation before this line."
        )

    imported_result = _resolve_imported_name(expression, source.aliases)
    if imported_result is not None:
        title = _expand_alias(expression, source.aliases)
        doc = inspect.getdoc(imported_result)
        try:
            signature = inspect.signature(imported_result)
        except (TypeError, ValueError):
            signature = None
        lines = [f"{title}    <{type(imported_result).__name__}>", "-" * max(20, len(title) + 10), ""]
        if signature is not None:
            lines.append(f"{expression}{signature}")
        if doc:
            lines.extend(["", *doc.splitlines()])
        return "\n".join(lines)

    builtin = getattr(builtins, expression, None)
    if builtin is not None:
        doc = inspect.getdoc(builtin) or "(No docstring available.)"
        return f"{expression}    <{type(builtin).__name__}>\n\n{doc}"

    scope, owner = _scope_nodes(source.tree, line)
    inferred = _infer_name_type(expression, source, line, scope, owner)
    if inferred is not None:
        return f"{expression}    <{inferred.__module__}.{inferred.__qualname__}>\n\n{inspect.getdoc(inferred) or '(No docstring available.)'}"
    raise UnresolvedExpression(f"Cannot resolve {expression!r} from imports or source assignments.")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print("Usage: runtime_doc.py SOURCE_FILE EXPRESSION LINE", file=sys.stderr)
        return 2
    path, expression, line_text = args
    try:
        result = lookup(path, expression, int(line_text))
    except (OSError, SyntaxError, UnresolvedExpression, ValueError) as exc:
        print(str(exc))
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
