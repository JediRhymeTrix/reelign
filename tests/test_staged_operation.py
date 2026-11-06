"""Runner integration: healthy pending is explicit; confirmed baselines stay put."""
from copy import deepcopy
import json
from unittest.mock import patch

from test_operation import OperationTests
from syncer.model import SyncError, StateError


class StagedOperationTests(OperationTests):
    def test_deferred_has_explicit_status_and_no_baseline_advance(self):
        from syncer.staged import Deferred
        with patch("syncer.runner.execute_targets", side_effect=Deferred("waiting for visibility")):
            self.assertEqual(self.call(["--rule", "one"]), 0)
        self.assertEqual(self.state["rules"], {})
        result = json.loads(self.output.getvalue().splitlines()[0])
        self.assertEqual(result["status"], "pending_verification")
        self.assertFalse(result["converged"])

    def test_state_error_is_global_not_unrelated_rule_continuation(self):
        with patch("syncer.runner.execute_targets", side_effect=StateError("persist failed")) as execute:
            self.assertEqual(self.call(), 1)
        self.assertEqual(execute.call_count, 1)

    def test_pending_writer_blocks_selected_reader_even_if_writer_unselected(self):
        from syncer.config import Rule
        from syncer.staged import encode, records
        from syncer.model import Snapshot, Item
        source, target = self.rules[0].source, self.rules[0].destination
        snapshots = [Snapshot(url, "", "", [Item("movie", {"tmdb": 1})]) for url in [source, target]]
        self.state.update(version=2, journals={"one": {
            "version": 1, "fingerprint": self.rules[0].fingerprint,
            "urls": [source, target], "desired": [records(s.items) for s in snapshots],
            "expected": [encode(s) for s in snapshots], "operations": [],
            "created_at": "2026-11-06T17:00:00Z"}})
        reader = Rule("reader", target, "https://mdblist.com/lists/owner/reader",
                      "source_to_destination", "mirror", attribution=False)
        self.assertEqual(self.call(["--rule", "reader"], rules=[self.rules[0], reader]), 1)
        self.assertNotIn("reader", self.state["rules"])

    def test_converged_status_is_explicit(self):
        self.assertEqual(self.call(["--rule", "one"]), 0)
        entry = json.loads(self.output.getvalue().splitlines()[0])
        self.assertEqual(entry["status"], "converged")
        self.assertTrue(entry["converged"])