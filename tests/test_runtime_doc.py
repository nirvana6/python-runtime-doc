from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
