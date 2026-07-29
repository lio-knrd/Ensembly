import tempfile
import unittest
from pathlib import Path

from backend.main import _safe_file


class SafeFileTests(unittest.TestCase):
    """The SPA handler matches every unclaimed path, so containment matters."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "public"
        (self.root / "nested").mkdir(parents=True)
        (self.root / "proof.txt").write_text("ok", encoding="utf-8")
        (self.root / "nested" / "deep.txt").write_text("ok", encoding="utf-8")
        # A secret next to, but outside, the served directory.
        (Path(self._tmp.name) / ".env").write_text("SECRET=1", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_serves_a_file_in_the_directory(self):
        self.assertIsNotNone(_safe_file(self.root, "proof.txt"))

    def test_serves_a_nested_file(self):
        self.assertIsNotNone(_safe_file(self.root, "nested/deep.txt"))

    def test_refuses_traversal_out_of_the_directory(self):
        self.assertIsNone(_safe_file(self.root, "../.env"))
        self.assertIsNone(_safe_file(self.root, "nested/../../.env"))

    def test_missing_file_and_empty_path_return_none(self):
        self.assertIsNone(_safe_file(self.root, "nope.txt"))
        self.assertIsNone(_safe_file(self.root, ""))

    def test_a_directory_is_not_served_as_a_file(self):
        self.assertIsNone(_safe_file(self.root, "nested"))

    def test_missing_root_directory_is_not_an_error(self):
        self.assertIsNone(_safe_file(self.root.parent / "absent", "proof.txt"))


if __name__ == "__main__":
    unittest.main()
