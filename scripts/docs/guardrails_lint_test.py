"""Tests for the guardrails language lint (python3 -m unittest)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import guardrails_lint

REPO = Path(__file__).resolve().parents[2]


class ProseHitsTest(unittest.TestCase):
    def test_prose_word_is_a_hit(self) -> None:
        self.assertEqual(guardrails_lint.prose_hits("A governed call.\n"), [(1, "governed")])

    def test_case_insensitive(self) -> None:
        self.assertEqual(guardrails_lint.prose_hits("## Governance\n"), [(1, "Governance")])

    def test_inline_code_is_allowed(self) -> None:
        text = "Use `governed_call` and the `governed-call-audit` job.\n"
        self.assertEqual(guardrails_lint.prose_hits(text), [])

    def test_fenced_code_is_allowed(self) -> None:
        text = "Intro\n\n```go\n// governed call\n```\n\nAfter a governed call.\n"
        self.assertEqual(guardrails_lint.prose_hits(text), [(7, "governed")])

    def test_other_word_forms_are_not_hits(self) -> None:
        self.assertEqual(guardrails_lint.prose_hits("ungoverned governs govern\n"), [])


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_repository_docs_pass(self) -> None:
        self.assertEqual(guardrails_lint.check(REPO), [])

    def test_in_scope_hit_is_reported_with_location(self) -> None:
        self.write("docs/guide.md", "# Guide\n\nKei governance applies.\n")
        errors = guardrails_lint.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("docs/guide.md:3", errors[0])

    def test_out_of_scope_files_are_skipped(self) -> None:
        self.write("CHANGELOG.md", "Governed calls.\n")
        self.write("docs/specs/contract.md", "Governed calls.\n")
        self.write("examples/go/x/NOTES.md", "Governed calls.\n")
        self.assertEqual(guardrails_lint.check(self.root), [])


if __name__ == "__main__":
    unittest.main()
