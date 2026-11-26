"""Resolve Python runtime documentation for a qualified expression.

The receiver type is inferred from the AST of the queried file plus imported
objects only: the queried file is parsed and never executed, string
(forward-reference) annotations are never evaluated, and a name whose type is
not provable from a binding at or before the queried line is reported as
unresolvable instead of guessed. Supported bindings are plain/annotated/
augmented assignment, walrus, sequence unpacking, ``for``/``async for`` targets,
``with``/``async with`` targets (the ``__enter__``/``__aenter__`` return type),
``except``/``except*`` targets (visible only inside the handler), ``match``
captures, and module-level assignments visible from a function.

Control flow is merged per path instead of being applied in source order: a
``break`` leaves the loop immediately and skips its ``else`` block, and a
``with`` block whose ``__exit__``/``__aexit__`` may swallow an exception only
guarantees the bindings made before its first statement that can raise.
"""

from __future__ import annotations

import ast
import builtins
import collections.abc as collections_abc
import contextlib
import dis
import importlib
import inspect
import sys
import typing
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


@dataclass(frozen=True)
class _TypeRef:
    """A statically inferred type: a concrete class plus generic arguments.

    ``args`` holds positional type arguments for parametrized annotations such
    as ``Iterator[int]`` or ``ContextManager[list]``. An argument is ``None``
    when that parameter could not be resolved; callers must treat such a
    reference as unknown rather than guessing.
    """

    origin: type[Any]
    args: tuple[_TypeRef | None, ...] = ()


_NUMERIC_RANK = {bool: 1, int: 2, float: 3, complex: 4}
_RANK_TYPES = {1: int, 2: int, 3: float, 4: complex}
_TRY_STAR: Any = getattr(ast, "TryStar", None)


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else None
    return None


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


def _resolve_name_object(name: str, source: Source) -> Any | None:
    candidate = _resolve_imported_name(name, source.aliases)
    if candidate is not None:
        return candidate
    return getattr(builtins, name, None)


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


# ---------------------------------------------------------------------------
# Annotation resolution.
#
# Annotations are resolved by importing the referenced names only; string
# (forward-reference) annotations are never evaluated, so a source file is
# never executed on behalf of the query.
# ---------------------------------------------------------------------------


def _type_ref_from_object(value: Any) -> _TypeRef | None:
    if isinstance(value, type):
        return _TypeRef(value)
    origin = typing.get_origin(value)
    if isinstance(origin, type):
        return _TypeRef(origin, tuple(_type_ref_from_object(item) for item in typing.get_args(value)))
    return None


def _type_ref(node: ast.expr | None, source: Source) -> _TypeRef | None:
    if node is None:
        return None
    if isinstance(node, ast.Subscript):
        base = _type_ref(node.value, source)
        if base is None:
            return None
        slice_node = node.slice
        elements = slice_node.elts if isinstance(slice_node, ast.Tuple) else [slice_node]
        return _TypeRef(base.origin, tuple(_type_ref(element, source) for element in elements))
    if isinstance(node, ast.Constant):
        # String / forward-reference annotations are intentionally refused.
        return None
    name = _dotted_name(node)
    if name is None:
        return None
    candidate = _resolve_name_object(name, source)
    if candidate is None:
        return None
    if isinstance(candidate, type) or typing.get_origin(candidate) is not None:
        return _type_ref_from_object(candidate)
    return None


def _element_ref(ref: _TypeRef | None) -> _TypeRef | None:
    """Element type yielded by iterating ``ref``, when it is statically known."""
    if ref is None:
        return None
    if ref.args:
        return ref.args[0]
    if ref.origin is range:
        return _TypeRef(int)
    if ref.origin is str:
        return _TypeRef(str)
    if ref.origin is bytes:
        return _TypeRef(int)
    return None


def _returns_self(function: Any) -> bool:
    """True when ``function`` is literally ``def f(self): return self``.

    Both the annotation-free context-manager convention (``__enter__`` returning
    the manager itself, as ``mujoco.viewer.Handle`` does) and "no annotation"
    are indistinguishable from the signature alone, so the body is checked at
    the bytecode level: a bare load of the receiver followed by a return is
    provable without executing any source.
    """
    code = getattr(function, "__code__", None)
    if code is None:
        return False
    try:
        parameters = list(inspect.signature(function).parameters)
    except (TypeError, ValueError):
        return False
    if not parameters:
        return False
    receiver = parameters[0]
    try:
        instructions = [
            instruction
            for instruction in dis.get_instructions(code)
            if instruction.opname not in {"RESUME", "NOP", "CACHE", "COPY_FREE_VARS"}
        ]
    except (TypeError, ValueError):
        return False
    if len(instructions) != 2:
        return False
    load, ret = instructions
    return (
        load.opname in {"LOAD_FAST", "LOAD_DEREF"}
        and load.argval == receiver
        and ret.opname == "RETURN_VALUE"
    )


def _enter_ref(ref: _TypeRef | None, is_async: bool) -> _TypeRef | None:
    """Type bound by ``with``/``async with``: the ``__enter__`` return type."""
    if ref is None:
        return None
    origin = ref.origin
    if not is_async and origin is contextlib.AbstractContextManager:
        return ref.args[0] if ref.args else None
    if is_async and origin is contextlib.AbstractAsyncContextManager:
        return ref.args[0] if ref.args else None
    method = getattr(origin, "__aenter__" if is_async else "__enter__", None)
    if method is None or not callable(method):
        return None
    try:
        annotation = inspect.signature(method).return_annotation
    except (TypeError, ValueError):
        return None
    if annotation is inspect.Signature.empty:
        return _TypeRef(origin) if _returns_self(method) else None
    if isinstance(annotation, str):
        return None
    return _type_ref_from_object(annotation)


def _constant_return_values(code: Any) -> list[Any] | None:
    """Constants every ``return`` in ``code`` yields, or None when dynamic."""
    values: list[Any] = []
    previous: Any = None
    for instruction in dis.get_instructions(code):
        if instruction.opname == "RETURN_CONST":
            values.append(instruction.argval)
            previous = instruction
            continue
        if instruction.opname == "RETURN_VALUE":
            if previous is None or previous.opname != "LOAD_CONST":
                return None
            values.append(previous.argval)
        previous = instruction
    return values or None


def _may_suppress(ref: _TypeRef | None, is_async: bool) -> bool:
    """True unless the manager's exit method provably never returns truthy.

    ``__exit__`` returning a true value swallows the exception raised in the
    block and execution continues on the line after the block, so a statement
    that never ran must not be treated as a binding there. Only a provably
    falsy exit result rules that path out; an unknown exit method (including a
    C-level one) is assumed able to swallow.
    """
    if ref is None:
        return True
    method = getattr(ref.origin, "__aexit__" if is_async else "__exit__", None)
    if method is None or not callable(method):
        return True
    try:
        annotation = inspect.signature(method).return_annotation
    except (TypeError, ValueError):
        annotation = inspect.Signature.empty
    if annotation is None or annotation is type(None):
        # `-> None` returns None, which is falsy: the exception propagates.
        return False
    code = getattr(method, "__code__", None)
    if code is None:
        return True
    values = _constant_return_values(code)
    if values is None:
        return True
    return any(bool(value) for value in values)


def _is_inert_value(expression: ast.expr | None) -> bool:
    """True when evaluating ``expression`` cannot raise.

    Only literal displays are provable: no call, no attribute access, no name
    load (which may be unbound), no arithmetic (which may divide by zero).
    """
    if expression is None:
        return True
    if isinstance(expression, ast.Constant):
        return True
    if isinstance(expression, (ast.List, ast.Tuple, ast.Set)):
        return all(_is_inert_value(element) for element in expression.elts)
    if isinstance(expression, ast.Dict):
        for key, value in zip(expression.keys, expression.values):
            if key is None or not _is_inert_value(key) or not _is_inert_value(value):
                return False
        return True
    if isinstance(expression, ast.UnaryOp) and isinstance(
        expression.op, (ast.UAdd, ast.USub, ast.Invert, ast.Not)
    ):
        return _is_inert_value(expression.operand)
    return False


def _may_raise(statement: ast.stmt) -> bool:
    """True when ``statement`` may raise before it completes.

    Assigning a literal display cannot raise, so the binding it makes is
    guaranteed even inside a block whose ``__exit__`` may swallow an
    exception; every other statement is a possible raise point.
    """
    if isinstance(statement, (ast.Pass, ast.Global, ast.Nonlocal)):
        return False
    if isinstance(statement, ast.Expr):
        return not _is_inert_value(statement.value)
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not all(
            isinstance(target, (ast.Name, ast.Tuple, ast.List, ast.Starred))
            for target in targets
        ):
            # Storing into an attribute or subscript may raise.
            return True
        return not _is_inert_value(statement.value)
    return True


def _augmented_ref(
    previous: _TypeRef | None, operator: ast.operator, right: _TypeRef | None
) -> _TypeRef | None:
    """Result type of an augmented assignment, or ``None`` when not provable."""
    if previous is None or right is None:
        return None
    left_type, right_type = previous.origin, right.origin
    left_rank, right_rank = _NUMERIC_RANK.get(left_type), _NUMERIC_RANK.get(right_type)
    if isinstance(operator, (ast.Add, ast.Sub, ast.Mult)):
        if left_rank and right_rank:
            return _TypeRef(_RANK_TYPES[max(left_rank, right_rank)])
        if isinstance(operator, ast.Add) and left_type is str and right_type is str:
            return _TypeRef(str)
        if left_type is right_type and left_type in (list, tuple, bytes):
            return _TypeRef(left_type)
        return None
    if isinstance(operator, ast.Div):
        if left_rank and right_rank:
            return _TypeRef(complex if max(left_rank, right_rank) == 4 else float)
        return None
    if isinstance(operator, (ast.FloorDiv, ast.Mod, ast.Pow)):
        if left_rank and right_rank:
            if max(left_rank, right_rank) == 4:
                return _TypeRef(complex)
            if max(left_rank, right_rank) == 3:
                return _TypeRef(float)
            return _TypeRef(int)
        if isinstance(operator, ast.Mod) and left_type is str and right_type is str:
            return _TypeRef(str)
        return None
    if isinstance(operator, (ast.BitAnd, ast.BitOr, ast.BitXor, ast.LShift, ast.RShift)):
        if left_rank and right_rank and max(left_rank, right_rank) <= 2:
            return _TypeRef(int)
        if left_type is right_type and left_type in (set, frozenset, dict):
            return _TypeRef(left_type)
        return None
    return None


def _exception_tuple_ref(nodes: list[ast.expr], source: Source) -> _TypeRef | None:
    """Nearest common exception ancestor of ``except (A, B) as e``, or None."""
    classes: list[Any] = []
    for node in nodes:
        ref = _type_ref(node, source)
        if ref is None or not issubclass(ref.origin, BaseException):
            return None
        classes.append(ref.origin)
    common = classes[0]
    for candidate in classes[1:]:
        if issubclass(candidate, common):
            continue
        for base in common.__mro__:
            if issubclass(candidate, base):
                common = base
                break
        else:
            return None
    return _TypeRef(common)


def _try_fields(
    statement: ast.stmt,
) -> tuple[list[ast.stmt], list[ast.stmt], list[ast.stmt], list[ast.ExceptHandler]] | None:
    """``(body, orelse, finalbody, handlers)`` for ``try``/``try*``, else None."""
    if isinstance(statement, ast.Try):
        node: Any = statement
    elif _TRY_STAR is not None and isinstance(statement, _TRY_STAR):
        node = statement
    else:
        return None
    return node.body, node.orelse, node.finalbody, node.handlers


def _in_body(statements: list[ast.stmt], line: int) -> bool:
    if not statements:
        return False
    start = getattr(statements[0], "lineno", None)
    end = getattr(statements[-1], "end_lineno", None)
    return isinstance(start, int) and isinstance(end, int) and start <= line <= end


def _is_irrefutable(pattern: ast.pattern) -> bool:
    """True when ``pattern`` matches every subject (``case _:`` / ``case x:``)."""
    if isinstance(pattern, ast.MatchAs):
        return pattern.pattern is None
    if isinstance(pattern, ast.MatchOr):
        return any(_is_irrefutable(sub) for sub in pattern.patterns)
    return False


def _walrus_nodes(expression: ast.expr) -> list[ast.NamedExpr]:
    """``NamedExpr`` nodes that are bound when ``expression`` is evaluated.

    A lambda body runs when the lambda is *called* and a generator body runs
    when the iterator is *advanced* (PEP 572), so a walrus inside either is not
    bound at the line that merely creates the object. Lambda default arguments
    and a generator's first iterable are evaluated eagerly and are still
    visited.
    """
    found: list[ast.NamedExpr] = []
    stack: list[ast.AST] = [expression]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Lambda):
            stack.extend(node.args.defaults)
            stack.extend(value for value in node.args.kw_defaults if value is not None)
            continue
        if isinstance(node, ast.GeneratorExp):
            if node.generators:
                stack.append(node.generators[0].iter)
            continue
        if isinstance(node, ast.NamedExpr):
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


@dataclass
class _Flow:
    """How control leaves the statement that was just applied.

    ``normal`` is the environment that continues on the next line, or ``None``
    when the statement never completes (a ``break``/``continue`` or a body
    that always jumps). ``breaks`` holds the environment at every ``break``
    seen, which targets the enclosing loop: a loop merges those paths *without*
    its ``else`` block because ``break`` jumps over it.
    """

    normal: dict[str, _TypeRef | None] | None
    breaks: list[dict[str, _TypeRef | None]]


class _Binder:
    """Flow-sensitive evaluation of the bindings visible at one source line.

    Only statically provable bindings are recorded; anything dynamic is stored
    as ``None`` (bound but unknown) so a later lookup refuses instead of
    guessing. Values are computed in source order so a binding may refer to an
    earlier binding in the same scope.
    """

    def __init__(self, source: Source) -> None:
        self.source = source

    # -- expression types ---------------------------------------------------
    def value_ref(self, expression: ast.expr | None, env: dict[str, _TypeRef | None]) -> _TypeRef | None:
        if expression is None:
            return None
        if isinstance(expression, ast.Constant):
            return _TypeRef(type(expression.value))
        if isinstance(expression, (ast.List, ast.ListComp)):
            if isinstance(expression, ast.List):
                return _TypeRef(list, (self._homogeneous_element(expression.elts, env),))
            return _TypeRef(list)
        if isinstance(expression, (ast.Dict, ast.DictComp)):
            return _TypeRef(dict)
        if isinstance(expression, (ast.Set, ast.SetComp)):
            if isinstance(expression, ast.Set):
                return _TypeRef(set, (self._homogeneous_element(expression.elts, env),))
            return _TypeRef(set)
        if isinstance(expression, ast.Tuple):
            return _TypeRef(tuple, tuple(self.value_ref(element, env) for element in expression.elts))
        if isinstance(expression, ast.Name):
            return env.get(expression.id)
        if isinstance(expression, ast.IfExp):
            body = self.value_ref(expression.body, env)
            other = self.value_ref(expression.orelse, env)
            return body if body is not None and body == other else None
        if isinstance(expression, ast.Call):
            return self._call_ref(expression, env)
        if isinstance(expression, ast.JoinedStr):
            return _TypeRef(str)
        return None

    def _homogeneous_element(
        self, elements: list[ast.expr], env: dict[str, _TypeRef | None]
    ) -> _TypeRef | None:
        """Element type of a list/set display, only when every element agrees."""
        refs = [
            self.value_ref(element, env)
            for element in elements
            if not isinstance(element, ast.Starred)
        ]
        if not refs or any(ref is None for ref in refs):
            return None
        first = refs[0]
        return first if all(ref == first for ref in refs) else None

    def _call_ref(self, call: ast.Call, env: dict[str, _TypeRef | None]) -> _TypeRef | None:
        name = _dotted_name(call.func)
        if name is None or name.split(".")[0] in env:
            # A locally bound callee has no signature evidence.
            return None
        target = _resolve_name_object(name, self.source)
        if target is None:
            return None
        if isinstance(target, type):
            return _TypeRef(target)
        try:
            annotation = inspect.signature(target).return_annotation
        except (TypeError, ValueError):
            return None
        if annotation is inspect.Signature.empty or isinstance(annotation, str):
            return None
        return _type_ref_from_object(annotation)

    def iter_element_ref(
        self, expression: ast.expr | None, env: dict[str, _TypeRef | None]
    ) -> _TypeRef | None:
        ref = self.value_ref(expression, env)
        if ref is not None and ref.origin is tuple and ref.args:
            first = ref.args[0]
            if first is not None and all(argument == first for argument in ref.args):
                return first
            return None
        return _element_ref(ref)

    # -- binding statements -------------------------------------------------
    def parameter_refs(self, scope: ast.AST) -> dict[str, _TypeRef | None]:
        arguments = getattr(scope, "args", None)
        if not isinstance(arguments, ast.arguments):
            return {}
        env: dict[str, _TypeRef | None] = {}
        all_args = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        if arguments.vararg:
            all_args.append(arguments.vararg)
        if arguments.kwarg:
            all_args.append(arguments.kwarg)
        for argument in all_args:
            # A default value says nothing about the parameter type.
            env[argument.arg] = _type_ref(argument.annotation, self.source)
        return env

    def bind_walrus(self, expression: ast.expr | None, env: dict[str, _TypeRef | None]) -> None:
        if expression is None:
            return
        for node in _walrus_nodes(expression):
            if isinstance(node.target, ast.Name):
                env[node.target.id] = self.value_ref(node.value, env)

    # -- statement flow -----------------------------------------------------
    def apply(self, statement: ast.stmt, env: dict[str, _TypeRef | None]) -> None:
        """Record the effect of a statement that is entirely before the query."""
        flow = self._flow(statement, env)
        if flow.normal is not None and flow.normal is not env:
            env.clear()
            env.update(flow.normal)

    def _block_flow(
        self, statements: list[ast.stmt], env: dict[str, _TypeRef | None]
    ) -> _Flow:
        """Apply a statement list in source order, stopping at a jump.

        ``break``/``continue`` end the block; the environment at a ``break``
        is reported so the enclosing loop can model the path that skips its
        ``else`` block.
        """
        breaks: list[dict[str, _TypeRef | None]] = []
        for child in statements:
            if isinstance(child, ast.Break):
                breaks.append(dict(env))
                return _Flow(None, breaks)
            if isinstance(child, ast.Continue):
                return _Flow(dict(env), breaks)
            flow = self._flow(child, env)
            breaks.extend(flow.breaks)
            if flow.normal is None:
                return _Flow(None, breaks)
        return _Flow(env, breaks)

    def _flow(self, statement: ast.stmt, env: dict[str, _TypeRef | None]) -> _Flow:
        if isinstance(statement, ast.Break):
            return _Flow(None, [dict(env)])
        if isinstance(statement, ast.Continue):
            return _Flow(None, [])
        if isinstance(statement, ast.Assign):
            value = self.value_ref(statement.value, env)
            self.bind_walrus(statement.value, env)
            for target in statement.targets:
                self.unpack(target, value, env)
        elif isinstance(statement, ast.AnnAssign):
            if not isinstance(statement.target, ast.Name):
                return _Flow(env, [])
            declared = _type_ref(statement.annotation, self.source)
            inferred = self.value_ref(statement.value, env) if statement.value is not None else None
            env[statement.target.id] = inferred if inferred is not None else declared
        elif isinstance(statement, ast.AugAssign):
            if not isinstance(statement.target, ast.Name):
                return _Flow(env, [])
            # Augmented assignment reads before it writes: with no prior
            # binding the result type is unknown, never the right-hand side.
            previous = env.get(statement.target.id)
            right = self.value_ref(statement.value, env)
            env[statement.target.id] = _augmented_ref(previous, statement.op, right)
        elif isinstance(statement, ast.Delete):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    env.pop(target.id, None)
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            return self._flow_for(statement, env)
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            return self._flow_with(statement, env)
        elif isinstance(statement, ast.If):
            return self._flow_if(statement, env)
        elif isinstance(statement, ast.While):
            return self._flow_while(statement, env)
        elif (try_fields := _try_fields(statement)) is not None:
            return self._flow_try(statement, try_fields, env)
        elif isinstance(statement, ast.Match):
            return self._flow_match(statement, env)
        elif isinstance(statement, ast.Expr):
            self.bind_walrus(statement.value, env)
        return _Flow(env, [])

    def _flow_for(self, statement: ast.For | ast.AsyncFor, env: dict[str, _TypeRef | None]) -> _Flow:
        """Model every way out of a ``for`` loop.

        `else` runs after zero iterations and after a body that completed
        without ``break``; ``break`` jumps over it, so the loop target and any
        body binding may hold different types on the line after the loop and
        are merged as three separate paths.
        """
        self.bind_walrus(statement.iter, env)
        element = self.iter_element_ref(statement.iter, env)
        paths: list[dict[str, _TypeRef | None]] = []
        # Path 1: zero iterations. The target is not bound; `else` runs.
        unentered = self._block_flow(statement.orelse, dict(env))
        if unentered.normal is not None:
            paths.append(unentered.normal)
        paths.extend(unentered.breaks)
        # Path 2: at least one iteration; the target holds the element type and
        # a normally completed body continues into `else`.
        entered = dict(env)
        self.unpack(statement.target, element, entered)
        body = self._block_flow(statement.body, entered)
        if body.normal is not None:
            completed = self._block_flow(statement.orelse, dict(body.normal))
            if completed.normal is not None:
                paths.append(completed.normal)
            paths.extend(completed.breaks)
        # Path 3: `break` leaves the loop immediately and skips `else`, so the
        # state at the break point is merged as it is.
        paths.extend(body.breaks)
        if not paths:
            return _Flow(None, [])
        self._merge_envs(env, paths)
        return _Flow(env, [])

    def _flow_with(self, statement: ast.With | ast.AsyncWith, env: dict[str, _TypeRef | None]) -> _Flow:
        """Model a ``with`` block whose ``__exit__`` may swallow an exception."""
        is_async = isinstance(statement, ast.AsyncWith)
        suppressing = False
        for item in statement.items:
            self.bind_walrus(item.context_expr, env)
            context = self.value_ref(item.context_expr, env)
            if item.optional_vars is not None:
                # Reaching the line after the statement proves the block was
                # entered, so the target holds the `__enter__`/`__aenter__`
                # result. An unprovable result is still a rebind: it must never
                # fall back to the prior type.
                self.unpack(item.optional_vars, _enter_ref(context, is_async), env)
            suppressing = suppressing or _may_suppress(context, is_async)
        # A swallowing `__exit__` lets the block stop at its first statement
        # that raises and still continue after the block, so only the bindings
        # made before that point are guaranteed there. The state after the
        # block merges "the body ran to completion" with that prefix.
        prefix = dict(env)
        for child in statement.body:
            if isinstance(child, (ast.Break, ast.Continue)) or _may_raise(child):
                break
            self.apply(child, prefix)
        body = self._block_flow(statement.body, dict(env))
        if body.normal is None:
            # A jump inside the block: only the prefix exits normally.
            self._merge_envs(env, [prefix])
            return _Flow(None, body.breaks)
        self._merge_envs(env, [prefix, body.normal] if suppressing else [body.normal])
        return _Flow(env, body.breaks)

    def _flow_if(self, statement: ast.If, env: dict[str, _TypeRef | None]) -> _Flow:
        self.bind_walrus(statement.test, env)
        body = self._block_flow(statement.body, dict(env))
        other = self._block_flow(statement.orelse, dict(env))
        breaks = [*body.breaks, *other.breaks]
        paths = [flow.normal for flow in (body, other) if flow.normal is not None]
        if not paths:
            return _Flow(None, breaks)
        self._merge_envs(env, paths)
        return _Flow(env, breaks)

    def _flow_while(self, statement: ast.While, env: dict[str, _TypeRef | None]) -> _Flow:
        self.bind_walrus(statement.test, env)
        body = self._block_flow(statement.body, dict(env))
        paths: list[dict[str, _TypeRef | None]] = []
        # Zero iterations: the test was false immediately, `else` runs.
        unentered = self._block_flow(statement.orelse, dict(env))
        if unentered.normal is not None:
            paths.append(unentered.normal)
        paths.extend(unentered.breaks)
        if body.normal is not None:
            # The body completed and the test is evaluated again; `else` runs
            # when it becomes false.
            completed = self._block_flow(statement.orelse, dict(body.normal))
            if completed.normal is not None:
                paths.append(completed.normal)
            paths.extend(completed.breaks)
        # `break` leaves the loop and skips `else`.
        paths.extend(body.breaks)
        if not paths:
            return _Flow(None, [])
        self._merge_envs(env, paths)
        return _Flow(env, [])

    def _flow_try(
        self,
        statement: ast.stmt,
        try_fields: tuple[list[ast.stmt], list[ast.stmt], list[ast.stmt], list[ast.ExceptHandler]],
        env: dict[str, _TypeRef | None],
    ) -> _Flow:
        body, orelse, finalbody, handlers = try_fields
        # Reaching the line after the statement means one path completed: the
        # body (then `else`) or one handler. Bindings made on only one of those
        # paths are not guaranteed afterwards.
        paths: list[dict[str, _TypeRef | None]] = []
        breaks: list[dict[str, _TypeRef | None]] = []
        success = self._block_flow(body, dict(env))
        breaks.extend(success.breaks)
        if success.normal is not None:
            tail = self._block_flow(orelse, success.normal)
            breaks.extend(tail.breaks)
            if tail.normal is not None:
                paths.append(tail.normal)
        for handler in handlers:
            handled = dict(env)
            if handler.name:
                handled[handler.name] = self._handler_ref(statement, handler)
            flow = self._block_flow(handler.body, handled)
            if handler.name:
                # CPython deletes the handler name when the handler exits.
                if flow.normal is not None:
                    flow.normal.pop(handler.name, None)
                for exit_env in flow.breaks:
                    exit_env.pop(handler.name, None)
            breaks.extend(flow.breaks)
            if flow.normal is not None:
                paths.append(flow.normal)
        if not paths:
            for exit_env in breaks:
                self._block_flow(finalbody, exit_env)
            return _Flow(None, breaks)
        self._merge_envs(env, paths)
        if not finalbody:
            return _Flow(env, breaks)
        # `finally` runs on every exit, including a `break` out of the loop.
        for exit_env in breaks:
            self._block_flow(finalbody, exit_env)
        tail = self._block_flow(finalbody, env)
        return _Flow(tail.normal, [*breaks, *tail.breaks])

    def _flow_match(self, statement: ast.Match, env: dict[str, _TypeRef | None]) -> _Flow:
        subject = self.value_ref(statement.subject, env)
        self.bind_walrus(statement.subject, env)
        # Cases are alternatives, never a sequence: each starts from the
        # environment before the statement and they are merged, so the last
        # case written cannot win.
        paths: list[dict[str, _TypeRef | None]] = []
        breaks: list[dict[str, _TypeRef | None]] = []
        irrefutable = False
        for case in statement.cases:
            case_env = dict(env)
            self.bind_pattern(case.pattern, subject, case_env)
            flow = self._block_flow(case.body, case_env)
            breaks.extend(flow.breaks)
            if flow.normal is not None:
                paths.append(flow.normal)
            irrefutable = irrefutable or _is_irrefutable(case.pattern)
        if not irrefutable:
            # No case matched: nothing in the statement body ran.
            paths.append(dict(env))
        if not paths:
            return _Flow(None, breaks)
        self._merge_envs(env, paths)
        return _Flow(env, breaks)

    def _merge_envs(
        self,
        env: dict[str, _TypeRef | None],
        paths: list[dict[str, _TypeRef | None]],
    ) -> None:
        """Replace ``env`` with what every one of ``paths`` agrees on.

        A name left unbound on some path, or bound to different types on
        different paths, is unknown at the merge point and is recorded as bound
        but unknown so the lookup refuses instead of picking a path.
        """
        names: set[str] = set()
        for path in paths:
            names.update(path)
        merged: dict[str, _TypeRef | None] = {}
        for name in names:
            if all(name in path for path in paths):
                values = [path[name] for path in paths]
                if all(value == values[0] for value in values):
                    merged[name] = values[0]
                    continue
            merged[name] = None
        env.clear()
        env.update(merged)

    def unpack(
        self, target: ast.expr, ref: _TypeRef | None, env: dict[str, _TypeRef | None]
    ) -> None:
        if isinstance(target, ast.Name):
            env[target.id] = ref
            return
        if isinstance(target, ast.Starred):
            element = _element_ref(ref)
            self.unpack(
                target.value,
                _TypeRef(list, (element,)) if element is not None else _TypeRef(list),
                env,
            )
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element_target, element_ref in zip(
                target.elts, self._unpack_elements(ref, target.elts)
            ):
                if isinstance(element_target, ast.Starred):
                    inner = _element_ref(element_ref)
                    self.unpack(
                        element_target.value,
                        _TypeRef(list, (inner,)) if inner is not None else _TypeRef(list),
                        env,
                    )
                else:
                    self.unpack(element_target, element_ref, env)

    def _unpack_elements(
        self, ref: _TypeRef | None, targets: list[ast.expr]
    ) -> list[_TypeRef | None]:
        count = len(targets)
        unknown: list[_TypeRef | None] = [None] * count
        if ref is None or ref.origin not in (tuple, list):
            return unknown
        if ref.origin is list:
            element = _element_ref(ref)
            return [
                None if isinstance(target, ast.Starred) else element for target in targets
            ]
        if not ref.args:
            return unknown
        elements = list(ref.args)
        starred_at = next(
            (index for index, target in enumerate(targets) if isinstance(target, ast.Starred)),
            None,
        )
        if starred_at is None:
            return elements if len(elements) == count else unknown
        tail = count - starred_at - 1
        if len(elements) < starred_at + tail:
            return unknown
        head = elements[:starred_at]
        rest = elements[len(elements) - tail :] if tail else []
        return [*head, None, *rest]

    # -- pattern matching ---------------------------------------------------
    def bind_pattern(
        self, pattern: ast.pattern, subject: _TypeRef | None, env: dict[str, _TypeRef | None]
    ) -> None:
        if isinstance(pattern, ast.MatchAs):
            if pattern.pattern is not None:
                self.bind_pattern(pattern.pattern, subject, env)
            if pattern.name:
                # A capture binds the subject itself; no type narrowing.
                env[pattern.name] = subject
        elif isinstance(pattern, ast.MatchStar):
            if pattern.name:
                env[pattern.name] = _TypeRef(list)
        elif isinstance(pattern, ast.MatchMapping):
            for sub_pattern in pattern.patterns:
                self.bind_pattern(sub_pattern, None, env)
            if pattern.rest:
                env[pattern.rest] = _TypeRef(dict)
        elif isinstance(pattern, ast.MatchSequence):
            element = _element_ref(subject)
            for sub_pattern in pattern.patterns:
                if isinstance(sub_pattern, ast.MatchStar):
                    self.bind_pattern(sub_pattern, _TypeRef(list), env)
                else:
                    self.bind_pattern(sub_pattern, element, env)
        elif isinstance(pattern, ast.MatchClass):
            for sub_pattern in [*pattern.patterns, *pattern.kwd_patterns]:
                self.bind_pattern(sub_pattern, None, env)
        elif isinstance(pattern, ast.MatchOr):
            # Merging captures of differing types is not attempted.
            for sub_pattern in pattern.patterns:
                self.bind_pattern(sub_pattern, None, env)

    # -- line navigation ----------------------------------------------------
    def env_at(
        self, statements: list[ast.stmt], env: dict[str, _TypeRef | None], line: int
    ) -> dict[str, _TypeRef | None]:
        env = dict(env)
        for statement in statements:
            start = getattr(statement, "lineno", None)
            if not isinstance(start, int):
                continue
            if start > line:
                break
            end = getattr(statement, "end_lineno", start) or start
            if end < line:
                self.apply(statement, env)
                continue
            inner = self.descend(statement, env, line)
            if inner is not None:
                return inner
            return env
        return env

    def descend(
        self, statement: ast.stmt, env: dict[str, _TypeRef | None], line: int
    ) -> dict[str, _TypeRef | None] | None:
        """Environment inside the block of ``statement`` that contains ``line``."""
        if isinstance(statement, (ast.For, ast.AsyncFor)):
            if _in_body(statement.body, line):
                inner = dict(env)
                element = self.iter_element_ref(statement.iter, env)
                self.unpack(statement.target, element, inner)
                return self.env_at(statement.body, inner, line)
            if _in_body(statement.orelse, line):
                # `else` runs after zero iterations or after a body that
                # completed without `break`, so only those two states are
                # visible there (a body-only binding is not guaranteed).
                entered = dict(env)
                self.unpack(
                    statement.target, self.iter_element_ref(statement.iter, env), entered
                )
                body = self._block_flow(statement.body, entered)
                inner = dict(env)
                paths = [dict(env)]
                if body.normal is not None:
                    paths.append(body.normal)
                self._merge_envs(inner, paths)
                return self.env_at(statement.orelse, inner, line)
            return None
        if isinstance(statement, (ast.With, ast.AsyncWith)):
            inner = dict(env)
            is_async = isinstance(statement, ast.AsyncWith)
            for item in statement.items:
                self.bind_walrus(item.context_expr, inner)
                if item.optional_vars is not None:
                    entered = _enter_ref(self.value_ref(item.context_expr, inner), is_async)
                    self.unpack(item.optional_vars, entered, inner)
            if _in_body(statement.body, line):
                return self.env_at(statement.body, inner, line)
            return None
        if (try_fields := _try_fields(statement)) is not None:
            body, orelse, finalbody, handlers = try_fields
            if _in_body(body, line):
                return self.env_at(body, dict(env), line)
            for handler in handlers:
                if _in_body(handler.body, line):
                    inner = dict(env)
                    for child in body:
                        self.apply(child, inner)
                    if handler.name:
                        inner[handler.name] = self._handler_ref(statement, handler)
                    return self.env_at(handler.body, inner, line)
            for branch in (orelse, finalbody):
                if _in_body(branch, line):
                    inner = dict(env)
                    for child in body:
                        self.apply(child, inner)
                    return self.env_at(branch, inner, line)
            return None
        if isinstance(statement, ast.Match):
            subject = self.value_ref(statement.subject, env)
            for case in statement.cases:
                if _in_body(case.body, line):
                    inner = dict(env)
                    self.bind_pattern(case.pattern, subject, inner)
                    return self.env_at(case.body, inner, line)
            return None
        if isinstance(statement, (ast.If, ast.While)):
            inner = dict(env)
            self.bind_walrus(statement.test, inner)
            if _in_body(statement.body, line):
                return self.env_at(statement.body, inner, line)
            if _in_body(statement.orelse, line):
                return self.env_at(statement.orelse, inner, line)
            return None
        return None

    def _handler_ref(
        self, statement: ast.stmt, handler: ast.ExceptHandler
    ) -> _TypeRef | None:
        if _TRY_STAR is not None and isinstance(statement, _TRY_STAR):
            # `except*` always binds an ExceptionGroup at this granularity.
            return _TypeRef(ExceptionGroup)
        if handler.type is None:
            return None
        if isinstance(handler.type, ast.Tuple):
            return _exception_tuple_ref(handler.type.elts, self.source)
        ref = _type_ref(handler.type, self.source)
        if ref is None or not issubclass(ref.origin, BaseException):
            return None
        return ref


def _name_binding(
    name: str, source: Source, line: int, scope: ast.AST
) -> tuple[bool, _TypeRef | None]:
    """``(bound_in_scope, type at line)`` for ``name`` in the lexical scope.

    The flag separates "the queried file assigns this name before the line"
    from "the file never mentions it", so a caller may fall back to an import
    or a builtin only when the file binds nothing of that name.

    ``self``/``cls`` are deliberately not resolved: the class defined by the
    queried file cannot be imported without executing that file, so a member
    of it is refused rather than guessed.
    """
    binder = _Binder(source)
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        env = binder.parameter_refs(scope)
        if isinstance(scope, ast.Lambda):
            if name in env:
                return True, env[name]
        else:
            local = binder.env_at(scope.body, env, line)
            if name in local:
                return True, local[name]
        module_env = binder.env_at(source.tree.body, {}, line)
        return name in module_env, module_env.get(name)
    if isinstance(scope, ast.ClassDef):
        class_env = binder.env_at(scope.body, {}, line)
        return name in class_env, class_env.get(name)
    module_env = binder.env_at(source.tree.body, {}, line)
    return name in module_env, module_env.get(name)


def _name_ref(name: str, source: Source, line: int, scope: ast.AST) -> _TypeRef | None:
    """Type of ``name`` at ``line`` inside the lexical ``scope``."""
    return _name_binding(name, source, line, scope)[1]


def _infer_receiver_type(expression: str, source: Source, line: int) -> type[Any] | None:
    scope, _owner = _scope_nodes(source.tree, line)
    parts = expression.split(".")
    ref = _name_ref(parts[0], source, line, scope)
    if ref is None:
        return None

    receiver_type: type[Any] | None = ref.origin
    for attribute in parts[1:]:
        if receiver_type is None:
            return None
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


def _search_terms(module_name: str, symbol: str) -> str:
    """Build a short query from the owning package and canonical API symbol."""
    package = module_name.split(".", maxsplit=1)[0]
    if package == "builtins":
        return f"Python {symbol}"
    return f"{package} {symbol}"


def _search_terms_for_object(value: Any, fallback_symbol: str) -> str | None:
    """Return package + symbol for an imported runtime object, if identifiable."""
    if inspect.ismodule(value):
        module_name = value.__name__
        return _search_terms(module_name, module_name.rsplit(".", maxsplit=1)[-1])

    owner = getattr(value, "__objclass__", None)
    module_name = getattr(owner, "__module__", None) or getattr(value, "__module__", None)
    if not isinstance(module_name, str) or not module_name:
        return None
    symbol = getattr(value, "__name__", None) or fallback_symbol
    return _search_terms(module_name, symbol)


def search_query(path: str | Path, expression: str, line: int) -> str:
    """Return concise search terms for the canonical API under the cursor.

    Caller-local receiver names, signatures, and package versions are excluded.
    The queried source is parsed and its imports are resolved using the same
    runtime environment as :func:`lookup`.
    """
    source = _load_source(path)
    expression = expression.strip().strip(".")
    parts = expression.split(".")

    if len(parts) > 1:
        imported = _resolve_imported_name(expression, source.aliases)
        if imported is not None:
            query = _search_terms_for_object(imported, parts[-1])
            if query is not None:
                return query

        receiver_type = _infer_receiver_type(".".join(parts[:-1]), source, line)
        if receiver_type is None:
            raise UnresolvedExpression(
                f"Cannot identify the API behind {expression!r}; its receiver type is unknown."
            )
        return _search_terms(receiver_type.__module__, parts[-1])

    scope, _owner = _scope_nodes(source.tree, line)
    bound, inferred = _name_binding(expression, source, line, scope)
    if bound:
        if inferred is None:
            raise UnresolvedExpression(
                f"Cannot identify the API behind {expression!r}; its type is unknown."
            )
        return _search_terms(inferred.origin.__module__, inferred.origin.__name__)

    imported = _resolve_imported_name(expression, source.aliases)
    if imported is not None:
        query = _search_terms_for_object(imported, expression)
        if query is not None:
            return query

    builtin = getattr(builtins, expression, None)
    if builtin is not None:
        query = _search_terms_for_object(builtin, expression)
        if query is not None:
            return query

    raise UnresolvedExpression(f"Cannot identify the API behind {expression!r}.")


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

    scope, _owner = _scope_nodes(source.tree, line)
    bound, inferred = _name_binding(expression, source, line, scope)
    if bound:
        # A name this file binds shadows imports and builtins: the same binding
        # that answers `name.attr` must answer a bare `name`. An unknown
        # binding refuses instead of falling back to an unrelated object.
        if inferred is None:
            raise UnresolvedExpression(
                f"Cannot infer the type of {expression!r} at line {line}. "
                "Its binding is not statically provable."
            )
        origin = inferred.origin
        return f"{expression}    <{origin.__module__}.{origin.__qualname__}>\n\n{inspect.getdoc(origin) or '(No docstring available.)'}"

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

    raise UnresolvedExpression(f"Cannot resolve {expression!r} from imports or source assignments.")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 4 and args[0] == "--search-query":
        _, path, expression, line_text = args
        try:
            result = search_query(path, expression, int(line_text))
        except (OSError, SyntaxError, UnresolvedExpression, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(result)
        return 0
    if len(args) != 3:
        print("Usage: runtime_doc.py SOURCE_FILE EXPRESSION LINE", file=sys.stderr)
        print("       runtime_doc.py --search-query SOURCE_FILE EXPRESSION LINE", file=sys.stderr)
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
