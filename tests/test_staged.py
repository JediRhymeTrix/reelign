"""Durable intent, acknowledged deferral and restart without mutation replay."""
import base64
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from syncer.config import Rule
from syncer.model import Item, Snapshot, SyncError
from syncer.planner import plan
from syncer.runner import execute_targets
from syncer.state import StateStore
from test_api_contract import ListApi
from syncer.providers import Trakt

SOURCE = "https://trakt.tv/users/owner/lists/source"
TARGET = "https://trakt.tv/users/owner/lists/films"


class StagedTests(unittest.TestCase):
    def setUp(self):
        from syncer.staged import Session, resume, Deferred, Blocked
        self.Session, self.resume, self.Deferred, self.Blocked = Session, resume, Deferred, Blocked
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.enc"
        self.store = StateStore(self.path, base64.urlsafe_b64encode(b"k" * 32).decode())
        self.state = {"version": 1, "rules": {}, "oauth": {"access_token": "fixture"}}
        self.rule = Rule("test", SOURCE, TARGET, "source_to_destination", "mirror", attribution=False)
        self.apis = [ListApi(3), ListApi(3)]
        self.providers = [Trakt(SOURCE, self.apis[0], "owner"), Trakt(TARGET, self.apis[1], "owner")]
        self.sleeps = patch("time.sleep").start()
        self.addCleanup(patch.stopall)

    def snapshots(self):
        return [p.snapshot() for p in self.providers]

    def prepare(self):
        snapshots = self.snapshots()
        result = plan(self.rule, *snapshots, self.state["rules"].get(self.rule.id))
        return snapshots, result

    def execute(self, snapshots, result):
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with session:
            execute_targets([(self.providers[1], snapshots[1], result.destination)])
        session.commit(result.baseline)

    def test_ack_is_durable_before_verification_and_restart_never_replays(self):
        snapshots, result = self.prepare()
        result.destination = list(reversed(snapshots[1].items))
        result.baseline["destination"] = [{"media_type": i.media_type, "ids": i.ids} for i in result.destination]
        original = self.apis[1].request
        def request(method, path, params=None, body=None):
            if method == "POST":
                disk = self.store.load()
                self.assertEqual(disk["journals"]["test"]["operations"][-1]["outcome"], "intent")
            return original(method, path, params, body)
        self.apis[1].request = request
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(self.Deferred):
            with session:
                self.providers[1].reorder(result.destination, snapshots[1])
                raise self.Deferred("Rank verification not yet visible")
        disk = self.store.load()
        self.assertNotIn("test", disk["rules"])
        self.assertEqual(disk["journals"]["test"]["operations"][-1]["outcome"], "acknowledged")
        self.assertNotIn(b'"operations"', self.path.read_bytes())
        fresh = [Trakt(SOURCE, self.apis[0], "owner"), Trakt(TARGET, self.apis[1], "owner")]
        left, right, resumed = self.resume(self.rule, fresh, disk["journals"]["test"])
        self.assertEqual([i.entry_id for i in right.items], [i.entry_id for i in result.destination])
        self.assertEqual(sum(c[0] == "POST" for c in self.apis[1].calls), 1)

    def test_interrupted_intent_with_absent_effect_is_blocked_not_replayed(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(SystemExit):
            with session:
                with patch.object(self.apis[1], "request", side_effect=SystemExit("interrupted")):
                    self.providers[1].remove([snapshots[1].items[-1]])
        disk = self.store.load()
        with self.assertRaises(self.Blocked):
            self.resume(self.rule, self.providers, disk["journals"]["test"])
        self.assertFalse(any(c[0] == "POST" for c in self.apis[1].calls))

    def test_crash_after_committed_write_before_ack_can_verify_without_resend(self):
        snapshots, result = self.prepare()
        original = self.apis[1].request
        def request(*args, **kwargs):
            answer = original(*args, **kwargs)
            if args[0] == "POST":
                raise SystemExit("response lost")
            return answer
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(SystemExit):
            with session:
                with patch.object(self.apis[1], "request", side_effect=request):
                    self.providers[1].remove([snapshots[1].items[-1]])
        journal = self.store.load()["journals"]["test"]
        self.resume(self.rule, self.providers, journal)
        self.assertEqual(sum(c[0] == "POST" for c in self.apis[1].calls), 1)

    def test_failed_intent_persistence_prevents_remote_send(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(SyncError):
            with session, patch.object(self.store, "save", side_effect=SyncError("cannot persist")):
                self.providers[1].remove([snapshots[1].items[-1]])
        self.assertFalse(any(c[0] == "POST" for c in self.apis[1].calls))

    def test_noop_does_not_create_journal_or_migrate_state(self):
        snapshots, result = self.prepare()
        self.execute(snapshots, result)
        disk = self.store.load()
        self.assertEqual(disk["version"], 1)
        self.assertNotIn("journals", disk)
        self.assertIn("test", disk["rules"])

    def test_semantic_rule_change_cannot_resume_pending(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(self.Deferred):
            with session:
                self.providers[1].remove([snapshots[1].items[-1]])
                raise self.Deferred("Membership not yet visible")
        from dataclasses import replace
        with self.assertRaises(self.Blocked):
            self.resume(replace(self.rule, deletion_policy="additions_only"), self.providers,
                        self.store.load()["journals"]["test"])

    def test_own_add_then_remove_before_deferral_does_not_require_superseded_add(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        item = Item("movie", {"trakt": 4})
        with self.assertRaises(self.Deferred):
            with session:
                self.providers[1].add([item])
                self.providers[1].remove([item])
                raise self.Deferred("Membership not yet visible")
        self.resume(self.rule, self.providers, self.store.load()["journals"]["test"])
        self.assertEqual(sum(c[0] == "POST" for c in self.apis[1].calls), 2)

    def test_add_then_reorder_hydrates_entry_ids_without_deferral(self):
        snapshots, result = self.prepare()
        result.destination.insert(0, Item("movie", {"trakt": 4}))
        result.baseline["destination"].insert(0, {"media_type": "movie", "ids": {"trakt": 4}})
        self.execute(snapshots, result)
        self.assertEqual([row['movie']['ids']['trakt'] for row in self.apis[1].rows], [4, 1, 2, 3])
        self.assertNotIn('test', self.state.get('journals', {}))


if __name__ == "__main__":
    unittest.main()