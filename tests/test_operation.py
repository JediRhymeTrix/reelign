"""Standalone selection, failure isolation and backward-compatible state."""

import base64
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.config import Rule
from syncer.http import ApiError
from syncer.model import Item, Snapshot, SyncError
from syncer.runner import main
from syncer.state import StateStore


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "state.enc"
        self.state_path.touch()
        self.state = {"version": 1, "rules": {}, "oauth": {
            "created_at": int(time.time()), "expires_in": 604800, "access_token": "fixture"}}
        self.store = Mock()
        self.output = StringIO()
        self.token = self.state["oauth"]
        self.store.load.return_value = self.state
        self.rules = [Rule("one", "https://mdblist.com/lists/curator/one",
                           "https://trakt.tv/users/owner/lists/one", "source_to_destination", "mirror"),
                      Rule("two", "https://mdblist.com/lists/curator/two",
                           "https://trakt.tv/users/owner/lists/two", "source_to_destination", "mirror")]

    def call(self, extra=(), rules=None, source_error=None):
        def adapter(url, *args):
            p = Mock()
            p.snapshot.return_value = Snapshot(url, "Film", "", [Item("movie", {"tmdb": 1})])
            p.ensure_rank_sort.return_value = False
            if source_error and url.endswith("/one") and "mdblist" in url:
                p.snapshot.side_effect = source_error
            return p

        with redirect_stdout(self.output), redirect_stderr(self.output), \
                patch("syncer.config.load_config", return_value=self.rules if rules is None else rules), \
                patch("syncer.runner.StateStore", return_value=self.store), \
                patch("syncer.http.Http") as http, \
                patch("syncer.providers.MDBList", side_effect=adapter), \
                patch("syncer.providers.Trakt", side_effect=adapter), \
                patch.dict(os.environ, {"SYNC_STATE_KEY": "fixture", "TRAKT_CLIENT_ID": "fixture",
                                       "TRAKT_CLIENT_SECRET": "fixture", "MDBLIST_API_KEY": "fixture"}):
            http.return_value.request.return_value = ({"user": {"ids": {"slug": "owner"}}}, {})
            result = main(["--config", "ignored.toml", "--state", str(self.state_path), *extra])
        return result

    def test_unrelated_rule_continues_after_failed_source_read(self):
        self.assertEqual(self.call(source_error=SyncError("fixture read failed")), 1)
        self.assertNotIn("one", self.state["rules"])
        self.assertIn("two", self.state["rules"])
        self.store.save.assert_called_once()

    def test_auth_failure_stops_remaining_rules(self):
        self.assertEqual(self.call(source_error=ApiError("Unauthorized", status=401)), 1)
        self.assertEqual(self.state["rules"], {})
        self.store.save.assert_not_called()

    def test_state_persistence_failure_stops_remaining_rules(self):
        self.store.save.side_effect = SyncError("state push failed")
        self.assertEqual(self.call(), 1)
        self.assertNotIn("two", self.state["rules"])

    def test_select_one_rule(self):
        self.assertEqual(self.call(["--rule", "two"]), 0)
        self.assertEqual(set(self.state["rules"]), {"two"})

    def test_unknown_rule_is_error_before_state(self):
        self.assertEqual(self.call(["--rule", "missing"]), 1)
        self.store.load.assert_not_called()

    def test_no_enabled_rules_needs_no_credentials_or_state(self):
        self.assertEqual(self.call(rules=[]), 0)
        self.store.load.assert_not_called()

    def test_validate_only_needs_no_credentials_or_state(self):
        self.assertEqual(self.call(["--validate-only"]), 0)
        self.store.load.assert_not_called()

    def test_maintenance_never_fetches_lists_or_checkpoints_rules(self):
        self.assertEqual(self.call(["--maintenance-only"]), 0)
        self.assertEqual(self.state["rules"], {})
        self.store.save.assert_not_called()

    def test_dry_run_never_advances_baselines(self):
        self.assertEqual(self.call(["--dry-run"]), 0)
        self.assertEqual(self.state["rules"], {})
        self.store.save.assert_not_called()

    def test_dependency_reader_skips_after_failed_upstream_but_independent_runs(self):
        downstream = Rule("downstream", self.rules[0].destination,
                          "https://mdblist.com/lists/owner/downstream", "source_to_destination", "mirror")
        self.assertEqual(self.call(rules=[self.rules[0], downstream, self.rules[1]],
                                   source_error=SyncError("source failed")), 1)
        self.assertNotIn("downstream", self.state["rules"])
        self.assertIn("two", self.state["rules"])

    def test_reversed_chain_waits_for_upstream_failure_before_running_downstream(self):
        downstream = Rule("downstream", self.rules[0].destination,
                          "https://mdblist.com/lists/owner/downstream", "source_to_destination", "mirror")
        self.assertEqual(self.call(rules=[downstream, self.rules[0], self.rules[1]],
                                   source_error=SyncError("source failed")), 1)
        self.assertNotIn("downstream", self.state["rules"])
        self.assertIn("two", self.state["rules"])

    def test_select_profile_only(self):
        from dataclasses import replace
        rules = [self.rules[0], replace(self.rules[1], profile="nightly")]
        self.assertEqual(self.call(["--profile", "nightly"], rules=rules), 0)
        self.assertEqual(set(self.state["rules"]), {"two"})

    def test_unknown_profile_fails_before_state(self):
        self.assertEqual(self.call(["--profile", "missing"]), 1)
        self.store.load.assert_not_called()

    def test_malformed_account_response_is_safe_error_not_traceback(self):
        with patch("syncer.config.load_config", return_value=self.rules), \
                patch("syncer.runner.StateStore", return_value=self.store), \
                patch("syncer.http.Http") as http, \
                patch.dict(os.environ, {"SYNC_STATE_KEY": "fixture", "TRAKT_CLIENT_ID": "fixture",
                                       "MDBLIST_API_KEY": "fixture"}), \
                redirect_stdout(self.output), redirect_stderr(self.output):
            http.return_value.request.return_value = (None, {})
            self.assertEqual(main(["--config", "ignored.toml", "--state", str(self.state_path)]), 1)


class LegacyStateTests(unittest.TestCase):
    def test_legacy_ciphertext_loads_without_rewrite(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = b"k" * 32
        magic = b"PERSONAL-CRON-STATE-v1\n"
        nonce = b"n" * 12
        data = {"version": 1, "oauth": {}, "rules": {}}
        raw = magic + nonce + AESGCM(key).encrypt(nonce, json.dumps(data).encode(), magic)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.enc"
            path.write_bytes(raw)
            store = StateStore(path, base64.urlsafe_b64encode(key).decode())
            self.assertEqual(store.load(), data)
            self.assertFalse(store.save(data))
            self.assertEqual(path.read_bytes(), raw)

    def test_new_state_has_product_neutral_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.enc"
            store = StateStore(path, base64.urlsafe_b64encode(b"k" * 32).decode())
            store.save({"version": 1, "oauth": {}, "rules": {}})
            self.assertTrue(path.read_bytes().startswith(b"REELIGN-STATE-v1\n"))


if __name__ == "__main__":
    unittest.main()