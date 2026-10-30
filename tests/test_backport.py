"""Allowlisted private integration updates never copy configuration or state."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.backport import backport


class BackportTests(unittest.TestCase):
    def test_copy_preserves_config_state_and_checks_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "source", Path(directory) / "target"
            (source / "syncer").mkdir(parents=True)
            (source / "tests").mkdir()
            (source / "scripts").mkdir()
            (source / "syncer/core.py").write_text("fixture")
            (source / "tests/test_core.py").write_text("test")
            (source / "run.py").write_text("run")
            (source / "manage.py").write_text("manage")
            (source / "requirements.txt").write_text("requirements")
            (source / "config.toml").write_text("PUBLIC EXAMPLE MUST NOT COPY")
            target.mkdir()
            (target / "config.toml").write_text("PRIVATE CONFIG")
            (target / "state.enc").write_bytes(b"PRIVATE STATE")
            self.assertTrue(backport(source, target))
            self.assertEqual((target / "config.toml").read_text(), "PRIVATE CONFIG")
            self.assertEqual((target / "state.enc").read_bytes(), b"PRIVATE STATE")
            self.assertTrue(backport(source, target, check=True))
            (target / "syncer/core.py").write_text("drift")
            self.assertFalse(backport(source, target, check=True))

    def test_requires_explicit_target_and_refuses_source_tree(self):
        from syncer.model import SyncError
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            for target in [source, source / "nested"]:
                with self.subTest(target=target), self.assertRaises(SyncError):
                    backport(source, target)

    def test_extra_engine_file_detected_by_parity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / "a", Path(directory) / "b"
            for root in [source, target]:
                (root / "syncer").mkdir(parents=True)
                (root / "tests").mkdir()
                (root / "scripts").mkdir()
                for name in ["run.py", "manage.py", "requirements.txt"]:
                    (root / name).write_text("fixture")
            backport(source, target)
            (target / "syncer/obsolete.py").write_text("unsafe drift")
            self.assertFalse(backport(source, target, check=True))


if __name__ == "__main__":
    unittest.main()