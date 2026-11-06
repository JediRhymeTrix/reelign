"""Encrypted journal state schemas and fail-closed persistence boundaries."""

import base64
import copy
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer import model
from syncer.model import SyncError
from syncer.state import LEGACY_MAGIC, MAGIC, StateStore


SECRET = "private-journal-and-refresh-token"


class JournalStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.enc"
        self.key = base64.urlsafe_b64encode(b"j" * 32).decode()
        self.store = StateStore(self.path, self.key)
        self.legacy = {"version": 1, "rules": {}, "oauth": {"refresh_token": SECRET}}

    def assert_state_error(self, operation):
        with self.assertRaises(Exception) as caught:
            operation()
        error = caught.exception
        self.assertIsInstance(error, SyncError)
        self.assertEqual(type(error).__name__, "StateError")
        self.assertNotIn(SECRET, str(error))
        return error

    def write_payload(self, data, magic=MAGIC):
        """Build independently authenticated malformed fixtures, bypassing save."""
        nonce = b"n" * 12
        raw = json.dumps(data).encode()
        self.path.write_bytes(magic + nonce + self.store.cipher.encrypt(nonce, raw, magic))

    def schema_two(self):
        return {**copy.deepcopy(self.legacy), "version": 2, "journals": {}}

    def pending_state(self):
        data = self.schema_two()
        urls = ["https://mdblist.com/lists/fixture/source",
                "https://trakt.tv/users/fixture/lists/destination"]
        identity = {"media_type": "movie", "ids": {"imdb": "tt0000001", "tmdb": 1}}
        expected = [{"url": url, "name": SECRET, "description": SECRET,
                     "version": "fixture-version", "metadata": {"description": SECRET},
                     "items": [{**copy.deepcopy(identity), "added_at": None,
                                "rank": 1, "entry_id": None}]}
                    for url in urls]
        data["journals"]["fixture-rule"] = {
            "version": 1, "fingerprint": "a" * 64, "urls": urls,
            "desired": [[copy.deepcopy(identity)], [copy.deepcopy(identity)]],
            "expected": expected, "created_at": "2026-01-01T00:00:00+00:00",
            "operations": [{"side": 1, "action": "add", "outcome": "acknowledged",
                            "items": [copy.deepcopy(identity)], "entries": [], "fields": {},
                            "after": copy.deepcopy(expected[1])}],
        }
        return data

    def test_state_error_is_an_actionable_sync_error(self):
        error_type = getattr(model, "StateError", None)
        self.assertIsNotNone(error_type)
        self.assertTrue(issubclass(error_type, SyncError))

    def test_schema_one_roundtrip_and_unchanged_save_remain_compatible(self):
        self.assertTrue(self.store.save(self.legacy))
        raw = self.path.read_bytes()
        self.assertTrue(raw.startswith(MAGIC))
        self.assertNotIn(SECRET.encode(), raw)
        self.assertEqual(self.store.load(), self.legacy)
        self.assertFalse(self.store.save(self.legacy))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_legacy_envelope_loads_without_rewriting(self):
        self.write_payload(self.legacy, LEGACY_MAGIC)
        raw = self.path.read_bytes()
        self.assertEqual(self.store.load(), self.legacy)
        self.assertFalse(self.store.save(self.legacy))
        self.assertEqual(self.path.read_bytes(), raw)

    def test_schema_two_is_encrypted_and_randomized_with_permanent_empty_journals(self):
        data = self.schema_two()
        self.assertTrue(self.store.save(data))
        first = self.path.read_bytes()
        self.assertTrue(first.startswith(MAGIC))
        self.assertNotIn(SECRET.encode(), first)
        self.assertNotIn(b'"journals"', first)
        self.assertEqual(self.store.load(), data)
        self.assertFalse(self.store.save(data))
        self.assertTrue(StateStore(self.path, self.key).save(data))
        self.assertNotEqual(first, self.path.read_bytes())
        self.assertEqual(StateStore(self.path, self.key).load()["journals"], {})

    def test_schema_two_version_makes_legacy_binary_reject_payload(self):
        data = self.schema_two()
        self.store.save(data)
        raw = self.path.read_bytes()[len(MAGIC):]
        payload = json.loads(self.store.cipher.decrypt(raw[:12], raw[12:], MAGIC))
        self.assertEqual(payload["version"], 2)
        self.assertNotEqual(payload.get("version"), 1)
        self.assertIn("journals", payload)

    def test_nonempty_pending_journal_roundtrip_keeps_all_details_encrypted(self):
        data = self.pending_state()
        self.store.save(data)
        raw = self.path.read_bytes()
        for private in (SECRET, "fixture-rule", "fixture-version", "tt0000001", "acknowledged"):
            self.assertNotIn(private.encode(), raw)
        self.assertEqual(StateStore(self.path, self.key).load(), data)

    def test_nested_malformed_journal_load_and_save_fail_closed(self):
        self.store.save(self.legacy)
        before = self.path.read_bytes()
        for field in ("desired", "expected", "operations"):
            data = self.pending_state()
            data["journals"]["fixture-rule"][field][0] = {"private": SECRET}
            with self.subTest(field=field):
                self.assert_state_error(lambda: self.store.save(data))
                self.assertEqual(self.path.read_bytes(), before)
                self.write_payload(data)
                self.assert_state_error(self.store.load)
                self.assertEqual(self.store.previous, self.legacy)
                self.path.write_bytes(before)

    def test_both_schema_two_boundaries_call_full_journal_validator(self):
        staged = importlib.import_module("syncer.staged")
        data = self.schema_two()
        with patch("syncer.staged.validate_journals", wraps=staged.validate_journals) as validate:
            self.store.save(data)
            validate.assert_called_once_with(data)
            validate.reset_mock()
            self.assertEqual(self.store.load(), data)
            validate.assert_called_once_with(data)
            validate.reset_mock()
            self.assertFalse(self.store.save(data))
            validate.assert_called_once_with(data)

    def test_schema_one_does_not_need_staged_validator(self):
        original_import = __import__

        def reject_staged(name, *args, **kwargs):
            if name in {"staged", "syncer.staged"}:
                raise AssertionError("schema one must not import staged")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=reject_staged):
            self.store.save(self.legacy)
            self.assertEqual(self.store.load(), self.legacy)

    def invalid_payloads(self):
        return [
            None, [], SECRET,
            {"version": 9, "rules": {}, "oauth": {}},
            {"version": True, "rules": {}, "oauth": {}},
            {"version": 1, "rules": [], "oauth": {}},
            {"version": 1, "rules": {}, "oauth": []},
            {"version": 1, "rules": {}, "oauth": {}, "journals": {}},
            {"version": 1, "rules": {}, "oauth": {}, "journals": {"rule": SECRET}},
            {"version": 2, "rules": {}, "oauth": {}},
            {"version": 2, "rules": {}, "oauth": {}, "journals": []},
            {"version": 2, "rules": {}, "oauth": {}, "journals": {"rule": SECRET}},
            {"version": 2, "rules": {}, "oauth": {}, "journals": {"rule": {"private": SECRET}}},
        ]

    def test_invalid_schema_save_fails_before_encryption_and_preserves_previous(self):
        self.store.save(self.legacy)
        raw = self.path.read_bytes()
        for data in self.invalid_payloads():
            with self.subTest(data=data), patch("syncer.state.os.urandom") as random:
                self.assert_state_error(lambda: self.store.save(data))
                random.assert_not_called()
                self.assertEqual(self.path.read_bytes(), raw)
                self.assertEqual(self.store.previous, self.legacy)

    def test_invalid_authenticated_payload_load_fails_without_exposing_journal(self):
        self.store.save(self.legacy)
        for data in self.invalid_payloads():
            with self.subTest(data=data):
                self.write_payload(data)
                self.assert_state_error(self.store.load)
                self.assertEqual(self.store.previous, self.legacy)

    def test_validator_exception_is_sanitized_on_load_and_save(self):
        data = self.schema_two()
        self.write_payload(data)
        with patch("syncer.staged.validate_journals", side_effect=RuntimeError(SECRET)):
            self.assert_state_error(self.store.load)
            self.assert_state_error(lambda: self.store.save(data))
        self.assertIsNone(self.store.previous)

    def test_missing_corrupt_and_wrong_key_are_state_errors(self):
        self.assert_state_error(self.store.load)
        self.path.write_bytes(SECRET.encode())
        self.assert_state_error(self.store.load)
        self.store.save(self.legacy)
        other_key = base64.urlsafe_b64encode(b"z" * 32).decode()
        self.assert_state_error(StateStore(self.path, other_key).load)

    def test_load_filesystem_exception_is_sanitized(self):
        self.store.save(self.legacy)
        for kind in (OSError, ValueError, RuntimeError):
            with self.subTest(kind=kind), patch.object(Path, "read_bytes", side_effect=kind(SECRET)):
                self.assert_state_error(self.store.load)

    def test_invalid_key_is_a_safe_state_error(self):
        self.assert_state_error(lambda: StateStore(self.path, SECRET))

    def test_save_serialization_failure_is_sanitized_and_does_not_replace_file(self):
        self.store.save(self.legacy)
        before = self.path.read_bytes()
        data = copy.deepcopy(self.legacy)
        data["rules"]["private"] = {SECRET}
        self.assert_state_error(lambda: self.store.save(data))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.previous, self.legacy)

    def test_save_crypto_and_filesystem_failures_leave_previous_unadvanced(self):
        targets = (
            "syncer.state.json.dumps", "syncer.state.os.urandom",
            "syncer.state.Path.mkdir", "syncer.state.tempfile.mkstemp",
            "syncer.state.os.fsync", "syncer.state.os.replace",
        )
        for target in targets:
            for kind in (OSError, ValueError, RuntimeError):
                with self.subTest(target=target, kind=kind):
                    self.store.save(self.legacy)
                    before = self.path.read_bytes()
                    data = copy.deepcopy(self.legacy)
                    data["rules"]["new"] = {}
                    with patch(target, side_effect=kind(SECRET)):
                        self.assert_state_error(lambda: self.store.save(data))
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertEqual(self.store.previous, self.legacy)
                    self.assertEqual(list(self.path.parent.glob(".state-*")), [])

    def test_encryption_exception_is_sanitized_before_file_creation(self):
        self.store.cipher = Mock()
        self.store.cipher.encrypt.side_effect = RuntimeError(SECRET)
        self.assert_state_error(lambda: self.store.save(self.legacy))
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.store.previous)

    def test_saved_previous_is_a_detached_snapshot(self):
        data = copy.deepcopy(self.legacy)
        self.store.save(data)
        data["oauth"]["refresh_token"] = "replacement"
        self.assertEqual(self.store.previous, self.legacy)
        self.assertTrue(self.store.save(data))

    def test_preflight_local_failures_are_sanitized(self):
        for kind in (OSError, ValueError, RuntimeError):
            with self.subTest(kind=kind), patch.object(Path, "mkdir", side_effect=kind(SECRET)):
                self.assert_state_error(self.store.preflight)
        with patch("syncer.state.os.access", return_value=False):
            self.assert_state_error(self.store.preflight)

    def test_save_does_not_repeat_preflight(self):
        with patch.object(self.store, "preflight") as preflight:
            self.store.save(self.legacy)
        preflight.assert_not_called()


class JournalGitStateTests(unittest.TestCase):
    assert_state_error = JournalStateTests.assert_state_error

    def setUp(self):
        JournalStateTests.setUp(self)
        self.store.repo = self.path.parent.resolve()

    def test_git_process_failures_are_sanitized(self):
        errors = [OSError(SECRET), ValueError(SECRET), RuntimeError(SECRET),
                  subprocess.TimeoutExpired("git", 45, output=SECRET, stderr=SECRET)]
        for error in errors:
            with self.subTest(error=type(error)), patch("syncer.state.subprocess.run", side_effect=error):
                self.assert_state_error(lambda: self.store._git("status"))
        result = subprocess.CompletedProcess([], 1, SECRET, SECRET)
        with patch("syncer.state.subprocess.run", return_value=result):
            self.assert_state_error(lambda: self.store._git("status"))

    def test_git_preflight_failure_never_reaches_mutations(self):
        for kind in (SyncError, RuntimeError, OSError):
            with self.subTest(kind=kind), patch.object(self.store, "_git", side_effect=kind(SECRET)) as git:
                self.assert_state_error(self.store.preflight)
                git.assert_called_once_with("fetch", "--quiet", "origin", "cron-state")

    def test_git_preflight_checks_fresh_clean_checkout_and_dry_run(self):
        with patch.object(self.store, "_git", side_effect=["", "same\n", "same\n", "", ""]) as git:
            self.store.preflight()
        self.assertEqual([call.args for call in git.call_args_list], [
            ("fetch", "--quiet", "origin", "cron-state"),
            ("rev-parse", "HEAD"), ("rev-parse", "origin/cron-state"),
            ("status", "--porcelain"), ("push", "--dry-run", "origin", "HEAD:cron-state"),
        ])
        for results in (["", "local", "remote"], ["", "same", "same", SECRET]):
            with self.subTest(results=results), patch.object(self.store, "_git", side_effect=results):
                self.assert_state_error(self.store.preflight)

    def test_git_add_commit_and_push_failures_retain_encrypted_recovery_file(self):
        for failed_step in ("add", "commit", "push"):
            for kind in (SyncError, RuntimeError, OSError):
                with self.subTest(step=failed_step, kind=kind):
                    self.store.repo = None
                    self.store.save(self.legacy)
                    previous = copy.deepcopy(self.store.previous)
                    data = copy.deepcopy(self.legacy)
                    data["rules"]["checkpoint"] = {"private": SECRET}
                    self.store.repo = self.path.parent.resolve()
                    calls = []

                    def git(*args):
                        calls.append(args)
                        if args[0] == failed_step or failed_step == "commit" and "commit" in args:
                            raise kind(SECRET)
                        return ""

                    with patch.object(self.store, "_git", side_effect=git), patch("syncer.state.time.sleep"):
                        self.assert_state_error(lambda: self.store.save(data))
                    raw = self.path.read_bytes()
                    self.assertNotIn(SECRET.encode(), raw)
                    self.assertEqual(StateStore(self.path, self.key).load(), data)
                    self.assertEqual(self.store.previous, previous)
                    self.assertNotIn("fetch", [call[0] for call in calls])

    def test_every_successful_checkpoint_commits_and_pushes_before_advancing_previous(self):
        def git(*args):
            self.assertIsNone(self.store.previous)
            return ""

        with patch.object(self.store, "_git", side_effect=git) as calls:
            self.assertTrue(self.store.save(self.legacy))
        self.assertEqual(self.store.previous, self.legacy)
        self.assertEqual([call.args[0] for call in calls.call_args_list], ["add", "-c", "push"])
        with patch.object(self.store, "_git") as calls:
            self.assertFalse(self.store.save(self.legacy))
        calls.assert_not_called()

    def test_retryable_push_failure_does_not_advance_previous_until_success(self):
        attempts = []

        def git(*args):
            self.assertIsNone(self.store.previous)
            if args[0] == "push":
                attempts.append(args)
                if len(attempts) < 3:
                    raise SyncError(SECRET)
            return ""

        with patch.object(self.store, "_git", side_effect=git), patch("syncer.state.time.sleep") as sleep:
            self.assertTrue(self.store.save(self.legacy))
        self.assertEqual(len(attempts), 3)
        self.assertEqual([call.args for call in sleep.call_args_list], [(1,), (2,)])
        self.assertEqual(self.store.previous, self.legacy)


if __name__ == "__main__":
    unittest.main()