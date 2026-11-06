"""Tests for the README compatibility table check (python3 -m unittest)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import compat

REPO = Path(__file__).resolve().parents[2]

TABLE = """# agentware

<!-- compat:begin -->
| agentware SDK | kei-proxy (minimum) | kei-cli (minimum) |
| --- | --- | --- |
| 1.1.0 | v0.3.0 | 0.2.0 |
| 1.0.0 | v0.2.11 | 0.1.32 |
<!-- compat:end -->
"""


class ParseTest(unittest.TestCase):
    def test_reads_rows_between_markers(self) -> None:
        rows = compat.parse_table(TABLE)
        self.assertEqual(
            rows,
            [
                compat.Row(sdk="1.1.0", kei_proxy="v0.3.0", kei_cli="0.2.0"),
                compat.Row(sdk="1.0.0", kei_proxy="v0.2.11", kei_cli="0.1.32"),
            ],
        )

    def test_missing_markers_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            compat.parse_table("# agentware\n")

    def test_malformed_versions_are_errors(self) -> None:
        bad = TABLE.replace("| v0.3.0 |", "| latest |")
        with self.assertRaises(ValueError):
            compat.parse_table(bad)


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "README.md").write_text(TABLE)
        (self.root / "VERSION").write_text("1.1.0\n")
        (self.root / "KEI_PROXY_VERSION").write_text("v0.3.0\n")

    def test_repository_table_matches_the_pin(self) -> None:
        self.assertEqual(compat.check(REPO), [])

    def test_current_row_matches_pin(self) -> None:
        self.assertEqual(compat.check(self.root), [])

    def test_pin_without_leading_v_still_matches(self) -> None:
        (self.root / "KEI_PROXY_VERSION").write_text("0.3.0\n")
        self.assertEqual(compat.check(self.root), [])

    def test_current_row_disagreeing_with_pin_fails(self) -> None:
        (self.root / "KEI_PROXY_VERSION").write_text("v0.3.1\n")
        errors = compat.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("v0.3.0", errors[0])
        self.assertIn("v0.3.1", errors[0])

    def test_no_row_for_the_current_version_fails(self) -> None:
        (self.root / "VERSION").write_text("1.2.0\n")
        errors = compat.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("1.2.0", errors[0])

    def test_invalid_pin_fails(self) -> None:
        (self.root / "KEI_PROXY_VERSION").write_text("main\n")
        errors = compat.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("KEI_PROXY_VERSION", errors[0])



class DocsCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "VERSION").write_text("1.1.0\n")
        (self.root / "docs").mkdir()

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_repository_docs_pass(self) -> None:
        self.assertEqual(compat.check_docs(REPO), [])

    def test_older_version_without_floor_fails(self) -> None:
        self.write("docs/guide.md", "# Guide\n\nAgentware 1.0.0 can register tools.\n")
        errors = compat.check_docs(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("docs/guide.md:3", errors[0])
        self.assertIn("1.0.0", errors[0])

    def test_from_and_or_later_are_floors(self) -> None:
        self.write(
            "README.md",
            "From Agentware 1.0.0, tools register.\n"
            "Needs agentware SDK v0.9.0 or later.\n"
            "Works from\nAgentware 0.8.0 onward.\n",
        )
        self.assertEqual(compat.check_docs(self.root), [])

    def test_current_and_newer_versions_pass(self) -> None:
        self.write("docs/guide.md", "Agentware 1.1.0 and Agentware 2.0.0.\n")
        self.assertEqual(compat.check_docs(self.root), [])

    def test_specs_and_out_of_scope_files_are_skipped(self) -> None:
        self.write("docs/specs/contract.md", "Agentware 0.1.0 shipped this.\n")
        self.write("CHANGELOG.md", "Agentware 0.1.0 shipped this.\n")
        self.assertEqual(compat.check_docs(self.root), [])


if __name__ == "__main__":
    unittest.main()
