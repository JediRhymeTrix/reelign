"""Sparse Trakt ranks are valid input; sync direction decides member repairs."""

from contextlib import redirect_stderr
import base64
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from syncer.config import Rule
from syncer.model import Item, Snapshot, SyncError
from syncer.planner import plan
from syncer.providers import Trakt
from syncer.runner import execute_targets
from syncer.staged import Session
from syncer.state import StateStore
from test_api_contract import ListApi
from test_providers import TRAKT_URL


SOURCE = "https://mdblist.com/lists/curator/source"


def media(*numbers):
    return [Item("movie", {"trakt": number}) for number in numbers]


def member_ids(items):
    return [int(item.ids["trakt"]) for item in items]


class RankReconciliationTests(unittest.TestCase):
    def read(self, provider, order="rank"):
        with patch("time.sleep"), redirect_stderr(StringIO()):
            return provider.snapshot(order=order)

    def sparse(self, count=4, removed=(2,)):
        api = ListApi(count)
        api.rows = [row for row in api.rows if row["movie"]["ids"]["trakt"] not in removed]
        api.meta["item_count"] = len(api.rows)
        return api, Trakt(TRAKT_URL, api, "owner")

    def test_sparse_snapshot_preserves_real_ranks_and_entries(self):
        api, provider = self.sparse(6, (1, 4))
        snapshot = self.read(provider)
        self.assertEqual([item.rank for item in snapshot.items], [2, 3, 5, 6])
        self.assertEqual([item.entry_id for item in snapshot.items], [1002, 1003, 1005, 1006])
        self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_gap_across_page_boundary_keeps_complete_partition_confirmation(self):
        api, provider = self.sparse(432, (4, 5, 251))
        snapshot = self.read(provider)
        self.assertEqual(len(snapshot.items), 429)
        self.assertEqual(snapshot.items[-1].rank, 432)
        self.assertEqual({c[2]["limit"] for c in api.calls if "/items" in c[1]}, {250, 100})
        self.assertEqual(len({item.entry_id for item in snapshot.items}), 429)

    def test_one_way_mirror_restores_destination_deletion_and_normalizes_order(self):
        api, provider = self.sparse()
        initial = self.read(provider)
        source = Snapshot(SOURCE, "", "", media(1, 2, 3, 4))
        rule = Rule("restore", SOURCE, TRAKT_URL, "source_to_destination", "mirror")
        result = plan(rule, source, initial, None)
        self.assertEqual(member_ids(result.destination), [1, 2, 3, 4])
        with patch("time.sleep"):
            summary = execute_targets([(provider, initial, result.destination)])
        final = self.read(provider)
        self.assertEqual(member_ids(final.items), [1, 2, 3, 4])
        self.assertEqual([item.rank for item in final.items], [1, 2, 3, 4])
        self.assertEqual((summary[0]["add"], summary[0]["remove"]), (1, 0))
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in api.calls if c[0] == "POST"], ["items", "reorder"])

    def test_rank_gaps_only_with_matching_membership_and_order_are_noop(self):
        api, provider = self.sparse()
        initial = self.read(provider)
        desired = list(initial.items)
        preview = execute_targets([(provider, initial, desired)], dry_run=True)
        self.assertFalse(preview[0]["reorder"])
        self.assertFalse(any(c[0] == "POST" for c in api.calls))
        execute_targets([(provider, initial, desired)])
        current = self.read(provider)
        self.assertEqual([i.rank for i in current.items], [1, 3, 4])
        self.assertEqual([i.entry_id for i in current.items], [1001, 1003, 1004])
        summary = execute_targets([(provider, current, current.items)])
        self.assertFalse(summary[0]["reorder"])
        self.assertFalse(any(c[0] == "POST" for c in api.calls))

    def test_reverse_authority_deletion_propagates_instead_of_resurrection(self):
        api, provider = self.sparse()
        right = self.read(provider, order="added")
        left = Snapshot(SOURCE, "", "", media(1, 2, 3, 4))
        rule = Rule("reverse", SOURCE, TRAKT_URL, "destination_to_source", "mirror")
        result = plan(rule, left, right, None)
        self.assertEqual(member_ids(result.source), [1, 3, 4])
        self.assertTrue(result.writes_source)
        self.assertFalse(result.writes_destination)
        self.assertTrue(all(c[0] == "GET" for c in api.calls))

    def test_additions_only_and_bidirectional_baseline_semantics_unchanged(self):
        _, provider = self.sparse()
        right = self.read(provider)
        left = Snapshot(SOURCE, "", "", media(1, 2, 3, 4))
        for direction in ("source_to_destination", "destination_to_source", "bidirectional"):
            rule = Rule("policies", SOURCE, TRAKT_URL, direction, "additions_only")
            result = plan(rule, left, right, None)
            target = result.source if direction == "destination_to_source" else result.destination
            self.assertEqual(set(member_ids(target)), {1, 2, 3, 4})
        rule = Rule("bidir", SOURCE, TRAKT_URL, "bidirectional", "mirror")
        baseline = {"fingerprint": rule.fingerprint,
                    "source": [{"media_type": item.media_type, "ids": item.ids} for item in left.items],
                    "destination": [{"media_type": item.media_type, "ids": item.ids} for item in left.items]}
        result = plan(rule, left, right, baseline)
        self.assertEqual(member_ids(result.source), [1, 3, 4])
        self.assertEqual(member_ids(result.destination), [1, 3, 4])

    def test_invalid_or_descending_ranks_are_not_repaired_by_reader(self):
        for ranks in ([0, 2], [2, 1], [1, 1], [-1, 2]):
            api = ListApi(2)
            for row, rank in zip(api.rows, ranks):
                row["rank"] = rank
            with self.subTest(ranks=ranks), self.assertRaises(SyncError):
                self.read(Trakt(TRAKT_URL, api, "owner"))
            self.assertTrue(all(c[0] == "GET" for c in api.calls))

    def test_sparse_ranks_never_mask_duplicate_entry_or_conflicting_identity(self):
        for conflict in ("entry", "identity"):
            api, provider = self.sparse()
            if conflict == "entry":
                api.rows[-1]["id"] = api.rows[0]["id"]
            else:
                api.rows[-1]["movie"]["ids"]["tmdb"] = 1
            with self.subTest(conflict=conflict), self.assertRaises(SyncError):
                self.read(provider)

    def test_directional_restore_completes_through_durable_session(self):
        api, provider = self.sparse()
        source_api = ListApi(4)
        source_url = "https://trakt.tv/users/owner/lists/source"
        source_provider = Trakt(source_url, source_api, "owner")
        source = self.read(source_provider)
        initial = self.read(provider)
        rule = Rule("staged-restore", source_url, TRAKT_URL, "source_to_destination", "mirror", attribution=False)
        result = plan(rule, source, initial, None)
        state = {"version": 1, "rules": {}, "oauth": {}}
        with tempfile.TemporaryDirectory() as directory, patch("time.sleep"):
            store = StateStore(Path(directory) / "state.enc", base64.urlsafe_b64encode(b"k" * 32).decode())
            session = Session(rule, [source_provider, provider], [source, initial], result, state, store)
            with session:
                execute_targets([(provider, initial, result.destination)], observation_guard=session.observe)
            session.commit(result.baseline)
            saved = store.load()
        self.assertEqual(member_ids(self.read(provider).items), [1, 2, 3, 4])
        self.assertEqual(saved["journals"], {})
        self.assertIn(rule.id, saved["rules"])
        self.assertEqual(sum(c[0] == "POST" for c in api.calls), 2)
        self.assertTrue(all(c[0] == "GET" for c in source_api.calls))

    def test_cross_page_descending_rank_does_not_escape_as_gap(self):
        api, provider = self.sparse(260, (4, 5))
        # Unique positive labels can still violate the promised rank/ascending
        # traversal. Do not locally sort a mixed or incorrectly sorted response.
        api.rows[249]["rank"], api.rows[250]["rank"] = api.rows[250]["rank"], api.rows[249]["rank"]
        with self.assertRaises(SyncError):
            self.read(provider)

if __name__ == "__main__":
    unittest.main()