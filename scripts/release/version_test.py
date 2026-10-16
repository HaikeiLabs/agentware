"""Tests for the shared release version tool (python3 -m unittest)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import version

REPO = Path(__file__).resolve().parents[2]


def copy_targets(dst: Path) -> None:
    """Copy VERSION and every synced file into dst, keeping relative paths."""
    for rel in ["VERSION", *(t.path for t in version.TARGETS)]:
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / rel, dst / rel)


class Pep440Test(unittest.TestCase):
    def test_release_versions_are_unchanged(self) -> None:
        self.assertEqual(version.to_pep440("1.2.3"), "1.2.3")

    def test_prereleases_use_pep440_spelling(self) -> None:
        self.assertEqual(version.to_pep440("1.2.3-rc.1"), "1.2.3rc1")
        self.assertEqual(version.to_pep440("1.2.3-beta.2"), "1.2.3b2")
        self.assertEqual(version.to_pep440("1.2.3-alpha.0"), "1.2.3a0")

    def test_rejects_versions_every_registry_cannot_take(self) -> None:
        for bad in ["1.2", "v1.2.3", "1.2.3-preview", "1.2.3-rc1", "01.2.3", "1.2.3+build"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                version.parse(bad)


class ReleaseTagsTest(unittest.TestCase):
    def test_one_version_derives_every_registry_tag(self) -> None:
        self.assertEqual(
            version.release_tags("1.2.3-rc.1"),
            {
                "release": "agentware-v1.2.3-rc.1",
                "go": "go/v1.2.3-rc.1",
                "python": "agentware-py-v1.2.3rc1",
            },
        )


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        copy_targets(self.root)

    def test_repository_versions_agree(self) -> None:
        self.assertEqual(version.check(REPO), [])

    def test_reports_a_file_that_drifted(self) -> None:
        version.set_version(self.root, "2.0.0")
        path = self.root / "python/pyproject.toml"
        path.write_text(path.read_text().replace('version = "2.0.0"', 'version = "1.9.9"'))
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("python/pyproject.toml", errors[0])
        self.assertIn("1.9.9", errors[0])

    def test_reports_a_missing_version_line(self) -> None:
        path = self.root / "go/kei/runtimelink/link.go"
        path.write_text("package runtimelink\n")
        errors = version.check(self.root)
        self.assertEqual(len(errors), 1)
        self.assertIn("go/kei/runtimelink/link.go", errors[0])

    def test_release_tag_must_match_version(self) -> None:
        version.set_version(self.root, "2.0.0")
        self.assertEqual(version.check(self.root, tag="agentware-v2.0.0"), [])
        errors = version.check(self.root, tag="agentware-v2.0.1")
        self.assertEqual(len(errors), 1)
        self.assertIn("agentware-v2.0.1", errors[0])

    def test_set_version_updates_every_target(self) -> None:
        version.set_version(self.root, "3.1.0-rc.2")
        self.assertEqual((self.root / "VERSION").read_text(), "3.1.0-rc.2\n")
        self.assertEqual(version.check(self.root), [])
        pyproject = (self.root / "python/pyproject.toml").read_text()
        self.assertIn('\nversion = "3.1.0rc2"\n', pyproject)
        lock = (self.root / "typescript/package-lock.json").read_text()
        self.assertEqual(lock.count('"version": "3.1.0-rc.2"'), 2)

    def test_set_version_rejects_an_invalid_version(self) -> None:
        with self.assertRaises(ValueError):
            version.set_version(self.root, "3.1")
        self.assertEqual(version.check(self.root), [])


if __name__ == "__main__":
    unittest.main()
