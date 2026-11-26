from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).parents[1] / "python" / "runtime_doc.py"
SPEC = importlib.util.spec_from_file_location("runtime_doc", MODULE_PATH)
assert SPEC and SPEC.loader
runtime_doc = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime_doc
SPEC.loader.exec_module(runtime_doc)


class RuntimeDocTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "sample.py"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_append_from_local_list_assignment(self) -> None:
        source = "history_positions = []\nhistory_positions.append(value)\n"
        self.path.write_text(source, encoding="utf-8")
        result = runtime_doc.lookup(self.path, "history_positions.append", 2)
        self.assertIn("append(object, /)", result)
        self.assertIn("Append object to the end of the list.", result)

    def test_module_function_remains_supported(self) -> None:
        self.path.write_text("import math\nmath.factorial(5)\n", encoding="utf-8")
        result = runtime_doc.lookup(self.path, "math.factorial", 2)
        self.assertIn("factorial(n, /)", result)
        self.assertIn("Find n!.", result)

    def test_search_query_uses_canonical_module_name_for_aliased_function(self) -> None:
        path = self._write("import math as m\nm.factorial(5)\n")
        self.assertEqual("math factorial", runtime_doc.search_query(path, "m.factorial", 2))

    def test_search_query_uses_package_and_method_not_local_receiver(self) -> None:
        fixture = Path(self.temp_dir.name) / "doc_fixture.py"
        fixture.write_text(
            "class Handle:\n"
            "    def is_running(self) -> bool:\n"
            "        return True\n",
            encoding="utf-8",
        )
        sys.path.insert(0, self.temp_dir.name)
        self.addCleanup(sys.path.remove, self.temp_dir.name)
        importlib.invalidate_caches()
        self.addCleanup(sys.modules.pop, "doc_fixture", None)
        path = self._write(
            "from doc_fixture import Handle\n"
            "def check(local_viewer: Handle) -> None:\n"
            "    local_viewer.is_running()\n"
        )
        self.assertEqual(
            "doc_fixture is_running",
            runtime_doc.search_query(path, "local_viewer.is_running", 3),
        )

    def test_google_search_markdown_link_appends_to_lookup_result(self) -> None:
        self._write("import math as m\nm.factorial(5)\n")
        link = runtime_doc.google_search_link(self.path, "m.factorial", 2)
        self.assertEqual(
            "[Google 搜索：math factorial](https://www.google.com/search?q=math%20factorial)",
            link,
        )

    def test_search_query_refuses_unknown_api_receiver(self) -> None:
        path = self._write("unknown.is_running()\n")
        with self.assertRaises(runtime_doc.UnresolvedExpression):
            runtime_doc.search_query(path, "unknown.is_running", 1)

    def test_with_search_link_cli_returns_lookup_text_and_link(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        path = self._write("import math as m\nm.factorial(5)\n")
        output = StringIO()
        with redirect_stdout(output):
            code = runtime_doc.main(["--with-search-link", str(path), "m.factorial", "2"])
        self.assertEqual(0, code)
        raw = output.getvalue()
        length_text, rest = raw.split("\n", 1)
        length = int(length_text)
        rest_bytes = rest.encode("utf-8")
        text = rest_bytes[:length].decode("utf-8")
        link = rest_bytes[length + 1 :].decode("utf-8").removesuffix("\n")
        self.assertEqual(runtime_doc.lookup(path, "m.factorial", 2), text)
        self.assertEqual(runtime_doc.google_search_link(path, "m.factorial", 2), link)

    def test_with_search_link_cli_roundtrips_non_ascii_text(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        fixture = Path(self.temp_dir.name) / "uni_mod.py"
        fixture.write_text('def ping():\n    """你好 café"""\n    return 1\n', encoding="utf-8")
        sys.path.insert(0, self.temp_dir.name)
        self.addCleanup(sys.path.remove, self.temp_dir.name)
        importlib.invalidate_caches()
        self.addCleanup(sys.modules.pop, "uni_mod", None)
        path = self._write("import uni_mod\nuni_mod.ping()\n")
        output = StringIO()
        with redirect_stdout(output):
            code = runtime_doc.main(["--with-search-link", str(path), "uni_mod.ping", "2"])
        self.assertEqual(0, code)
        raw = output.getvalue()
        length_text, rest = raw.split("\n", 1)
        length = int(length_text)
        rest_bytes = rest.encode("utf-8")
        text = rest_bytes[:length].decode("utf-8")
        self.assertEqual(runtime_doc.lookup(path, "uni_mod.ping", 2), text)
        self.assertIn("你好 café", text)

    def test_with_search_link_cli_keeps_lookup_error_without_a_link(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        path = self._write("unknown.append(value)\n")
        output = StringIO()
        with redirect_stdout(output):
            code = runtime_doc.main(["--with-search-link", str(path), "unknown.append", "1"])
        self.assertEqual(1, code)
        raw = output.getvalue()
        length_text, rest = raw.split("\n", 1)
        length = int(length_text)
        rest_bytes = rest.encode("utf-8")
        text = rest_bytes[:length].decode("utf-8")
        link = rest_bytes[length + 1 :].decode("utf-8").removesuffix("\n")
        self.assertIn("Cannot infer the receiver type", text)
        self.assertEqual("", link)

    def test_search_query_cli_prints_only_search_terms(self) -> None:
        from contextlib import redirect_stdout
        from io import StringIO

        path = self._write("import math as m\nm.factorial(5)\n")
        output = StringIO()
        with redirect_stdout(output):
            result = runtime_doc.main(["--search-query", str(path), "m.factorial", "2"])
        self.assertEqual(0, result)
        self.assertEqual("math factorial\n", output.getvalue())

    def test_nested_object_method_with_imported_type_annotation(self) -> None:
        self.path.write_text(
            "from pathlib import Path\n"
            "def read_file(path: Path):\n"
            "    path.exists()\n",
            encoding="utf-8",
        )
        result = runtime_doc.lookup(self.path, "path.exists", 3)
        self.assertIn("exists(*, follow_symlinks=True)", result)
        self.assertIn("Whether this path exists.", result)

    def test_unknown_receiver_fails_with_actionable_message(self) -> None:
        self.path.write_text("unknown.append(value)\n", encoding="utf-8")
        with self.assertRaises(runtime_doc.UnresolvedExpression) as ctx:
            runtime_doc.lookup(self.path, "unknown.append", 1)
        self.assertIn("Cannot infer the receiver type", str(ctx.exception))

    # ------------------------------------------------------------------
    # Name-binding inference regression tests.
    #
    # Contract: runtime-doc-bindings base.md (the `with ... as viewer`
    # failure) and taxonomy.md sections 3.5/4. Supported forms must resolve
    # the *binding construct* without guessing dynamic types; unsupported
    # forms must stay unresolved instead of falling back to a plausible type.
    # ------------------------------------------------------------------

    def _write(self, source: str) -> Path:
        self.path.write_text(source, encoding="utf-8")
        return self.path

    def _assert_unresolved(self, expression: str, line: int) -> None:
        with self.assertRaises(runtime_doc.UnresolvedExpression) as ctx:
            runtime_doc.lookup(self.path, expression, line)
        self.assertIn("Cannot", str(ctx.exception))

    def _install_cm_fixture(self) -> None:
        """Make a temporary module whose context manager enters to ``list``."""
        fixture = Path(self.temp_dir.name) / "cm_fixture.py"
        fixture.write_text(
            "class Session:\n"
            "    def __enter__(self) -> list:\n"
            "        return []\n"
            "\n"
            "    def close(self) -> None:\n"
            '        """Close the session."""\n'
            "\n"
            "\n"
            "def open_session() -> Session:\n"
            "    return Session()\n",
            encoding="utf-8",
        )
        sys.path.insert(0, self.temp_dir.name)
        self.addCleanup(sys.path.remove, self.temp_dir.name)
        importlib.invalidate_caches()
        self.addCleanup(sys.modules.pop, "cm_fixture", None)

    def test_plain_assignment_from_known_name_binds_its_type(self) -> None:
        path = self._write("history = []\nrows = history\nrows.append(1)\n")
        result = runtime_doc.lookup(path, "rows.append", 3)
        self.assertIn("Append object to the end of the list.", result)

    def test_annotated_assignment_without_value_uses_declared_type(self) -> None:
        """`count: int` declares a type; it does not bind a runtime value.

        PEP 526 leaves the name unbound, so this answer is the *declared*
        type's documentation — never evidence that a live object of that type
        exists at the query line.
        """
        path = self._write("count: int\ncount.bit_length()\n")
        result = runtime_doc.lookup(path, "count.bit_length", 2)
        self.assertIn("Number of bits necessary to represent self in binary.", result)

    def test_augmented_assignment_uses_operation_result_type(self) -> None:
        # int += float yields float, so the bound type is not the prior int.
        path = self._write("count = 1\ncount += 0.5\ncount.hex()\n")
        result = runtime_doc.lookup(path, "count.hex", 3)
        self.assertIn("Return a hexadecimal representation of a floating-point number.", result)

    def test_walrus_assignment_binds_value_type(self) -> None:
        path = self._write("numbers = [1, 2]\nif (alias := numbers):\n    alias.append(3)\n")
        result = runtime_doc.lookup(path, "alias.append", 3)
        self.assertIn("Append object to the end of the list.", result)

    def test_sequence_unpacking_binds_positional_element_types(self) -> None:
        path = self._write("first, second = 1, 2\nfirst.bit_length()\n")
        result = runtime_doc.lookup(path, "first.bit_length", 2)
        self.assertIn("Number of bits necessary to represent self in binary.", result)

    def test_starred_unpacking_binds_remaining_list(self) -> None:
        path = self._write("head, *tail = [1, 2, 3]\ntail.append(4)\n")
        result = runtime_doc.lookup(path, "tail.append", 2)
        self.assertIn("Append object to the end of the list.", result)

    def test_nested_unpacking_binds_inner_element_types(self) -> None:
        path = self._write("(left, (right, third)) = (1, (2, 3))\nright.bit_length()\n")
        result = runtime_doc.lookup(path, "right.bit_length", 2)
        self.assertIn("Number of bits necessary to represent self in binary.", result)

    def test_for_target_from_range_is_int(self) -> None:
        path = self._write("for index in range(3):\n    index.bit_length()\n")
        result = runtime_doc.lookup(path, "index.bit_length", 2)
        self.assertIn("Number of bits necessary to represent self in binary.", result)

    def test_for_target_from_annotated_iterator_element(self) -> None:
        path = self._write(
            "from typing import Iterator\n"
            "def consume(values: Iterator[int]) -> None:\n"
            "    for value in values:\n"
            "        value.bit_length()\n"
        )
        result = runtime_doc.lookup(path, "value.bit_length", 4)
        self.assertIn("Number of bits necessary to represent self in binary.", result)

    def test_async_for_target_from_annotated_iterator_element(self) -> None:
        path = self._write(
            "from typing import AsyncIterator\n"
            "async def consume(chunks: AsyncIterator[str]) -> None:\n"
            "    async for chunk in chunks:\n"
            "        chunk.upper()\n"
        )
        result = runtime_doc.lookup(path, "chunk.upper", 4)
        self.assertIn("Return a copy of the string converted to uppercase.", result)

    def test_for_target_is_not_visible_after_loop(self) -> None:
        # The loop may run zero times, so the target has no reliable type after it.
        self._write("for item in [1, 2]:\n    pass\nitem.bit_length()\n")
        self._assert_unresolved("item.bit_length", 3)

    def test_with_context_manager_annotation_binds_enter_type(self) -> None:
        path = self._write(
            "from typing import ContextManager\n"
            "def load(manager: ContextManager[list]) -> None:\n"
            "    with manager as items:\n"
            "        items.append(1)\n"
        )
        result = runtime_doc.lookup(path, "items.append", 4)
        self.assertIn("Append object to the end of the list.", result)

    def test_with_call_result_binds_annotated_enter_return_type(self) -> None:
        # Mirrors `with mujoco.viewer.launch_passive(...) as viewer`: the
        # callee's declared return type is the manager, and the bound name is
        # the manager's `__enter__` return type, not the manager itself.
        fixture = Path(self.temp_dir.name) / "cm_fixture.py"
        fixture.write_text(
            "class Session:\n"
            "    def __enter__(self) -> list:\n"
            "        return []\n"
            "    def close(self) -> None:\n"
            '        """Close the session."""\n'
            "\n"
            "\n"
            "def open_session() -> Session:\n"
            "    return Session()\n",
            encoding="utf-8",
        )
        sys.path.insert(0, self.temp_dir.name)
        self.addCleanup(sys.path.remove, self.temp_dir.name)
        importlib.invalidate_caches()
        self.addCleanup(sys.modules.pop, "cm_fixture", None)
        path = self._write(
            "from cm_fixture import open_session\n"
            "with open_session() as session:\n"
            "    session.append(1)\n"
        )
        result = runtime_doc.lookup(path, "session.append", 3)
        self.assertIn("Append object to the end of the list.", result)

    def test_async_with_binds_aenter_return_type(self) -> None:
        path = self._write(
            "from typing import AsyncContextManager\n"
            "async def drain(manager: AsyncContextManager[list]) -> None:\n"
            "    async with manager as items:\n"
            "        items.append(1)\n"
        )
        result = runtime_doc.lookup(path, "items.append", 4)
        self.assertIn("Append object to the end of the list.", result)

    def test_with_unresolvable_context_manager_refuses(self) -> None:
        # No annotation and no resolvable callee: `__enter__` type is unknown.
        self._write("with build_manager() as handle:\n    handle.read()\n")
        self._assert_unresolved("handle.read", 2)

    def test_except_target_binds_handled_exception_type(self) -> None:
        path = self._write(
            "try:\n"
            "    open('missing.txt')\n"
            "except FileNotFoundError as err:\n"
            "    err.with_traceback(None)\n"
        )
        result = runtime_doc.lookup(path, "err.with_traceback", 4)
        # Assert on the receiver type and a version-stable doc fragment:
        # CPython 3.12 says "set self.__traceback__ ..." and 3.14 "Set ...".
        self.assertIn("builtins.FileNotFoundError.with_traceback", result)
        self.assertIn("<method of FileNotFoundError>", result)
        self.assertIn("self.__traceback__ to tb", result)

    def test_except_star_target_binds_exception_group(self) -> None:
        path = self._write(
            "try:\n"
            "    raise ExceptionGroup('batch', [ValueError('bad')])\n"
            "except* ValueError as group:\n"
            "    group.subgroup(ValueError)\n"
        )
        result = runtime_doc.lookup(path, "group", 4)
        self.assertIn("ExceptionGroup", result)

    def test_except_target_is_not_visible_after_handler(self) -> None:
        # CPython deletes the handler name when the handler exits.
        self._write(
            "try:\n"
            "    pass\n"
            "except ValueError as err:\n"
            "    pass\n"
            "err.with_traceback(None)\n"
        )
        self._assert_unresolved("err.with_traceback", 5)

    def test_except_with_unresolvable_type_refuses(self) -> None:
        self._write(
            "def build_error():\n"
            "    return ValueError()\n"
            "try:\n"
            "    pass\n"
            "except build_error() as err:\n"
            "    err.with_traceback(None)\n"
        )
        self._assert_unresolved("err.with_traceback", 6)

    def test_match_bare_capture_uses_subject_type(self) -> None:
        path = self._write(
            "def handle(value: int) -> None:\n"
            "    match value:\n"
            "        case captured:\n"
            "            captured.bit_length()\n"
        )
        result = runtime_doc.lookup(path, "captured.bit_length", 4)
        self.assertIn("Number of bits necessary to represent self in binary.", result)

    def test_match_sequence_star_capture_binds_list(self) -> None:
        path = self._write(
            "def handle(values: list[int]) -> None:\n"
            "    match values:\n"
            "        case [first, *rest]:\n"
            "            rest.append(first)\n"
        )
        result = runtime_doc.lookup(path, "rest.append", 4)
        self.assertIn("Append object to the end of the list.", result)

    def test_match_mapping_rest_binds_dict(self) -> None:
        path = self._write(
            "def handle(mapping: dict[str, int]) -> None:\n"
            "    match mapping:\n"
            "        case {'k': 1, **rest}:\n"
            "            rest.keys()\n"
        )
        result = runtime_doc.lookup(path, "rest.keys", 4)
        # Receiver type plus a fragment both 3.12 ("D.keys() -> a set-like
        # object ...") and 3.14 ("Return a set-like object ...") contain.
        self.assertIn("builtins.dict.keys", result)
        self.assertIn("<method of dict>", result)
        self.assertIn("set-like object providing a view on", result)

    def test_match_value_pattern_does_not_narrow(self) -> None:
        # `case 1:` only compares; the subject keeps its (unknown) type.
        self._write("def handle(value):\n    match value:\n        case 1:\n            value.bit_length()\n")
        self._assert_unresolved("value.bit_length", 4)

    def test_del_invalidates_prior_binding(self) -> None:
        self._write("values = []\ndel values\nvalues.append(1)\n")
        self._assert_unresolved("values.append", 3)

    def test_comprehension_target_is_not_bound_outside(self) -> None:
        self._write("rows = [index for index in range(2)]\nindex.bit_length()\n")
        self._assert_unresolved("index.bit_length", 2)

    def test_unannotated_parameter_default_does_not_provide_type(self) -> None:
        self._write("def build(flag=1):\n    flag.bit_length()\n")
        self._assert_unresolved("flag.bit_length", 2)

    def test_unannotated_call_assignment_refuses(self) -> None:
        self._write("items = build_items()\nitems.append(1)\n")
        self._assert_unresolved("items.append", 2)

    def test_star_import_names_are_not_guessed(self) -> None:
        self._write("from json import *\ndumps({})\n")
        self._assert_unresolved("dumps", 2)

    def test_unresolvable_string_annotation_refuses(self) -> None:
        # Unresolvable annotations must be refused, never eval'd.
        self._write("def build(value: 'Nope') -> None:\n    value.bit_length()\n")
        self._assert_unresolved("value.bit_length", 2)

    def test_augmented_assignment_without_prior_binding_refuses(self) -> None:
        # Augmented assignment reads before it writes: there is no prior type.
        self._write("def bump() -> None:\n    total += 1\n    total.bit_length()\n")
        self._assert_unresolved("total.bit_length", 3)

    # ------------------------------------------------------------------
    # Control-flow and scope counterexamples (review.md F1-F5, F7).
    #
    # Contract: an inferred type may only describe what the binding construct
    # guarantees at the query line. Constructs that may not run at all (loop
    # bodies, try/except/else bodies, match cases) must not publish their
    # bindings to the following line, and a pre-existing binding must not be
    # reused where the construct may have rebound the name. The one positive
    # case below (`with` completed normally) is asserted as a type, because
    # reaching the following line proves the body ran to completion.
    # ------------------------------------------------------------------

    def test_for_target_does_not_reuse_prior_type_after_loop(self) -> None:
        # Zero iterations keep "old" (str); otherwise the target is rebound to
        # the element (int), so no single type is provable after the loop.
        self._write(
            'message = "old"\n'
            "for message in (1, 2):\n"
            "    pass\n"
            "message.upper()\n"
        )
        self._assert_unresolved("message.upper", 4)

    def test_async_for_target_does_not_reuse_prior_type_after_loop(self) -> None:
        self._write(
            "async def drain(chunks) -> None:\n"
            '    chunk = "old"\n'
            "    async for chunk in chunks:\n"
            "        pass\n"
            "    chunk.upper()\n"
        )
        self._assert_unresolved("chunk.upper", 5)

    def test_loop_body_binding_is_not_visible_after_the_loop(self) -> None:
        # `range(0)` may run zero times, so the body binding is not guaranteed.
        self._write('for _ in range(0):\n    leftover = "loop"\nleftover.upper()\n')
        self._assert_unresolved("leftover.upper", 3)

    def test_while_body_binding_is_not_visible_after_the_loop(self) -> None:
        self._write('while False:\n    leftover = "loop"\nleftover.upper()\n')
        self._assert_unresolved("leftover.upper", 3)

    def test_try_body_binding_is_not_visible_after_the_statement(self) -> None:
        # The handler path skips `rows = []`, so `rows` may be unbound.
        self._write(
            "try:\n"
            "    open('missing.txt')\n"
            "    rows = []\n"
            "except OSError:\n"
            "    pass\n"
            "rows.append(1)\n"
        )
        self._assert_unresolved("rows.append", 6)

    def test_try_else_binding_is_not_visible_after_the_statement(self) -> None:
        # `else` runs only when the try body raised nothing.
        self._write(
            "try:\n"
            "    pass\n"
            "except ValueError:\n"
            "    pass\n"
            "else:\n"
            "    ready = 1\n"
            "ready.bit_length()\n"
        )
        self._assert_unresolved("ready.bit_length", 7)

    def test_handler_body_binding_is_not_visible_after_the_statement(self) -> None:
        # `detail` exists only on the exception path.
        self._write(
            "try:\n"
            "    pass\n"
            "except ValueError:\n"
            '    detail = "bad"\n'
            "detail.upper()\n"
        )
        self._assert_unresolved("detail.upper", 5)

    def test_with_body_binding_is_visible_after_normal_exit(self) -> None:
        # Not a leak: reaching the next line proves the body ran to completion,
        # unlike a loop or try body that can be skipped.
        path = self._write(
            "with open('missing.txt') as handle:\n"
            '    inside = "text"\n'
            "inside.upper()\n"
        )
        result = runtime_doc.lookup(path, "inside.upper", 3)
        self.assertIn("Return a copy of the string converted to uppercase.", result)

    def test_with_target_keeps_enter_type_after_normal_exit(self) -> None:
        # Mirrors `with mujoco.viewer.launch_passive(...) as viewer`: after a
        # normally completed block the target holds the `__enter__` result,
        # not the value it held before the statement.
        self._install_cm_fixture()
        path = self._write(
            "from cm_fixture import open_session\n"
            'session = "old"\n'
            "with open_session() as session:\n"
            "    pass\n"
            "session.append(1)\n"
        )
        result = runtime_doc.lookup(path, "session.append", 5)
        self.assertIn("Append object to the end of the list.", result)

    def test_match_merge_does_not_take_the_last_case(self) -> None:
        # Cases are alternatives; the last one must not win by source order.
        self._write(
            "def handle(value: int) -> None:\n"
            "    match value:\n"
            "        case 1:\n"
            "            picked = 1\n"
            "        case _:\n"
            '            picked = "s"\n'
            "    picked.upper()\n"
        )
        self._assert_unresolved("picked.upper", 7)

    def test_match_merge_is_independent_of_case_order(self) -> None:
        self._write(
            "def handle(value: int) -> None:\n"
            "    match value:\n"
            "        case 1:\n"
            '            picked = "s"\n'
            "        case _:\n"
            "            picked = 1\n"
            "    picked.upper()\n"
        )
        self._assert_unresolved("picked.upper", 7)

    def test_lambda_walrus_is_not_bound_before_the_call(self) -> None:
        # PEP 572 binds the walrus target when the lambda body runs, not when
        # the lambda object is created.
        self._write("build = lambda: (leaked := 1)\nleaked.bit_length()\n")
        self._assert_unresolved("leaked.bit_length", 2)

    def test_local_binding_shadows_builtin_for_bare_names(self) -> None:
        # Receiver and bare-name lookups must agree: `id = 5` is an int.
        path = self._write("id = 5\nid.bit_length()\nid\n")
        receiver = runtime_doc.lookup(path, "id.bit_length", 2)
        self.assertIn("Number of bits necessary to represent self in binary.", receiver)
        bare = runtime_doc.lookup(path, "id", 3)
        self.assertIn("id    <builtins.int>", bare)
        self.assertNotIn("Return the identity of an object.", bare)

    def test_annotation_without_value_records_declared_container_type(self) -> None:
        # PEP 526 declaration only: the *declared* type documents the name,
        # without claiming a runtime value was bound.
        path = self._write("from pathlib import Path\nentry: Path\nentry.exists()\n")
        result = runtime_doc.lookup(path, "entry.exists", 3)
        self.assertIn("Whether this path exists.", result)

    # ------------------------------------------------------------------
    # `for ... else` with `break`, and suppressing context managers
    # (independent re-review N1/N2).
    #
    # Contract: README "结果类型无法唯一确定时一律报告无法推断" and the
    # `with` guarantee that an unprovable rebind never falls back to the
    # prior type. `break` skips `else`, so the target and the `else` body
    # may hold different types on the line after the loop; and a context
    # manager whose `__exit__` swallows the exception lets a later statement
    # in the block never run, so "reaching the next line proves the whole
    # block ran" only holds for statements before the first potentially
    # raising statement.
    # ------------------------------------------------------------------

    def test_for_else_break_skips_else_binding_after_loop(self) -> None:
        # `break` jumps over `else`: at runtime the target keeps the element
        # type (int here), so the trailing `x = "s"` never runs on that path.
        self._write(
            'x = "old"\n'
            "for x in (1, 2):\n"
            "    if x:\n"
            "        break\n"
            "else:\n"
            '    x = "s"\n'
            "x.upper()\n"
        )
        self._assert_unresolved("x.upper", 7)

    def test_for_else_break_is_independent_of_branch_assignment_order(self) -> None:
        # The two samples differ only in which branch assigns str and which
        # assigns int; `else` is not sequential code that runs after `break`,
        # so neither program has a unique type after the loop.
        self._write(
            'x = "old"\n'
            "for x in (1, 2):\n"
            '    x = "s"\n'
            "    break\n"
            "else:\n"
            "    x = 1\n"
            "x.upper()\n"
        )
        self._assert_unresolved("x.upper", 7)
        self._write(
            'x = "old"\n'
            "for x in (1, 2):\n"
            "    x = 1\n"
            "    break\n"
            "else:\n"
            '    x = "s"\n'
            "x.upper()\n"
        )
        self._assert_unresolved("x.upper", 7)

    def test_for_else_break_agreeing_paths_still_resolve(self) -> None:
        # Every path out of the statement (break, normal exit, zero
        # iterations) leaves a str: refusing here would be over-rejection.
        path = self._write(
            'x = "old"\n'
            "for x in (1, 2):\n"
            '    x = "body"\n'
            "    break\n"
            "else:\n"
            '    x = "else"\n'
            "x.upper()\n"
        )
        result = runtime_doc.lookup(path, "x.upper", 7)
        self.assertIn("Return a copy of the string converted to uppercase.", result)

    def test_for_else_without_break_resolves_when_paths_agree(self) -> None:
        # No `break`: `else` runs on both the zero-iteration and the
        # completed-loop path, and it agrees with the prior int binding.
        path = self._write(
            "count = 0\n"
            "for count in (1, 2):\n"
            "    pass\n"
            "else:\n"
            "    count = 3\n"
            "count.bit_length()\n"
        )
        result = runtime_doc.lookup(path, "count.bit_length", 6)
        self.assertIn("Number of bits necessary to represent self in binary.", result)

    def test_suppress_body_binding_after_potential_raise_refuses(self) -> None:
        # contextlib.suppress swallows the ValueError from `maybe()`, so the
        # next statement never runs; `y` may be unbound at the query line.
        self._write(
            "import contextlib\n"
            "\n"
            "with contextlib.suppress(ValueError):\n"
            "    maybe()\n"
            '    y = "never"\n'
            "y.upper()\n"
        )
        self._assert_unresolved("y.upper", 6)

    def test_suppress_body_rebinding_does_not_fall_back_to_prior_type(self) -> None:
        # The block may abort before rebinding `y`, so the prior int is not
        # provable either: neither branch may be picked as the answer.
        self._write(
            "import contextlib\n"
            "\n"
            "y = 1\n"
            "with contextlib.suppress(ValueError):\n"
            "    maybe()\n"
            '    y = "never"\n'
            "y.upper()\n"
        )
        self._assert_unresolved("y.upper", 7)
        self._assert_unresolved("y.bit_length", 7)

    def test_suppress_keeps_binding_that_precedes_potential_raise(self) -> None:
        # `first` is bound before anything can raise, so it survives a
        # swallowed exception; `second` is not, and must stay unknown.
        path = self._write(
            "import contextlib\n"
            "\n"
            "with contextlib.suppress(ValueError):\n"
            "    first = 1\n"
            "    maybe()\n"
            '    second = "later"\n'
            "second.upper()\n"
        )
        result = runtime_doc.lookup(path, "first.bit_length", 7)
        self.assertIn("Number of bits necessary to represent self in binary.", result)
        self._assert_unresolved("second.upper", 7)

    def test_suppress_block_with_literal_only_binding_resolves(self) -> None:
        # Nothing in the block can raise before the assignment, so the
        # binding always happens; a blanket refusal of suppress blocks would
        # discard a provable type.
        path = self._write(
            "import contextlib\n"
            "\n"
            "with contextlib.suppress(ValueError):\n"
            '    y = "always"\n'
            "y.upper()\n"
        )
        result = runtime_doc.lookup(path, "y.upper", 5)
        self.assertIn("Return a copy of the string converted to uppercase.", result)


if __name__ == "__main__":
    unittest.main()
