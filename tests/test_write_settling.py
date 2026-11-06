"""Acknowledged Trakt writes settle via reads, not repeated mutations."""

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from syncer.http import ApiError
from syncer.model import Item, Snapshot, SyncError
from syncer.providers import Trakt
from syncer.runner import execute_targets
from test_providers import FakeHttp, TRAKT_URL, trakt_meta, trakt_item, trakt_page


def complete(numbers, ranks=None):
    rows = [trakt_item(n, ranks[i] if ranks else i + 1) for i, n in enumerate(numbers)]
    return [trakt_meta(len(rows)), trakt_page(rows), trakt_meta(len(rows))]


class WriteSettlingTests(unittest.TestCase):
    def snapshot(self, provider):
        with patch("time.sleep") as sleep, redirect_stderr(StringIO()):
            result = provider.snapshot(order="rank")
        return result, sleep

    def test_acknowledged_add_survives_three_stale_counts_without_readding(self):
        stale = [trakt_meta(1), trakt_page([trakt_item(1, 1)], total=2)]
        http = FakeHttp({"added": {"movies": 1}}, *(stale * 3), *complete([1, 2]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 2})])
        snapshot, sleep = self.snapshot(provider)
        self.assertEqual([item.ids["trakt"] for item in snapshot.items], [1, 2])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 5, 10])
        self.assertEqual(sum(call[0] == "POST" for call in http.calls), 1)

    def test_reorder_transient_duplicate_rank_discards_whole_read(self):
        items = [Item("movie", {"trakt": 2}, entry_id=1002), Item("movie", {"trakt": 1}, entry_id=1001)]
        http = FakeHttp({"updated": 2}, trakt_meta(2),
                        trakt_page([trakt_item(1, 1), trakt_item(2, 1)]), *complete([2, 1]))
        provider = Trakt(TRAKT_URL, http, "owner")
        old = Snapshot(TRAKT_URL, "Films", "", items)
        with patch.object(provider, "snapshot", return_value=old):
            provider.reorder(items, old)
        result, sleep = self.snapshot(provider)
        self.assertEqual([i.ids["trakt"] for i in result.items], [2, 1])
        sleep.assert_called_once_with(2)
        self.assertEqual(sum(call[0] == "POST" for call in http.calls), 1)

    def test_cross_page_overlap_after_write_is_retried_without_deduplication(self):
        bad = [trakt_meta(2), trakt_page([trakt_item(1, 1)], 1, 2, 2),
               trakt_page([trakt_item(1, 1)], 2, 2, 2)]
        http = FakeHttp({"added": {"movies": 1}}, *bad, *complete([1, 2]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 2})])
        result, sleep = self.snapshot(provider)
        self.assertEqual(len(result.items), 2)
        sleep.assert_called_once_with(2)

    def test_duplicate_rank_without_own_write_remains_terminal(self):
        http = FakeHttp(trakt_meta(2), trakt_page([trakt_item(1, 1), trakt_item(2, 1)]))
        with patch("time.sleep") as sleep, self.assertRaises(SyncError):
            Trakt(TRAKT_URL, http, "owner").snapshot()
        sleep.assert_not_called()
        self.assertEqual(len(http.calls), 2)

    def test_acknowledged_add_not_visible_in_otherwise_complete_stale_read_retries(self):
        http = FakeHttp({"added": {"movies": 1}}, *complete([1]), *complete([1, 2]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 2})])
        snapshot, sleep = self.snapshot(provider)
        self.assertEqual(len(snapshot.items), 2)
        sleep.assert_called_once_with(2)

    def test_acknowledged_remove_still_visible_retries_reads_only(self):
        http = FakeHttp({"deleted": {"movies": 1}}, *complete([1, 2]), *complete([1]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.remove([Item("movie", {"trakt": 2})])
        snapshot, sleep = self.snapshot(provider)
        self.assertEqual([i.ids["trakt"] for i in snapshot.items], [1])
        sleep.assert_called_once_with(2)
        self.assertEqual(sum(call[0] == "POST" for call in http.calls), 1)

    def test_reorder_complete_but_old_order_retries_until_acknowledged_order(self):
        desired = [Item("movie", {"trakt": 2}, entry_id=1002), Item("movie", {"trakt": 1}, entry_id=1001)]
        http = FakeHttp({"updated": 2}, *complete([1, 2]), *complete([2, 1]))
        provider = Trakt(TRAKT_URL, http, "owner")
        with patch.object(provider, "snapshot", return_value=Snapshot(TRAKT_URL, "", "", desired)):
            provider.reorder(desired, Snapshot(TRAKT_URL, "", "", desired))
        snapshot, sleep = self.snapshot(provider)
        self.assertEqual([i.ids["trakt"] for i in snapshot.items], [2, 1])
        sleep.assert_called_once_with(2)

    def test_settling_exhausts_five_reads_and_fails_no_write_replay(self):
        bad = [trakt_meta(2), trakt_page([trakt_item(1, 1), trakt_item(2, 1)])]
        http = FakeHttp({"added": {"movies": 1}}, *(bad * 5))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 2})])
        with patch("time.sleep") as sleep, redirect_stderr(StringIO()), self.assertRaises(SyncError):
            provider.snapshot(order="rank")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 5, 10, 20])
        self.assertEqual(len(http.calls), 11)
        self.assertEqual(sum(c[0] == "POST" for c in http.calls), 1)

    def test_valid_read_clears_pending_write_context(self):
        http = FakeHttp({"added": {"movies": 1}}, *complete([1, 2]),
                        trakt_meta(2), trakt_page([trakt_item(1, 1), trakt_item(2, 1)]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 2})])
        self.snapshot(provider)
        with patch("time.sleep") as sleep, self.assertRaises(SyncError):
            provider.snapshot()
        sleep.assert_not_called()

    def test_unacknowledged_or_partial_write_does_not_enable_settling(self):
        for failure in (ApiError("write denied", 403), {"added": {"movies": 0}}):
            http = FakeHttp(failure, trakt_meta(2), trakt_page([trakt_item(1, 1), trakt_item(2, 1)]))
            provider = Trakt(TRAKT_URL, http, "owner")
            with self.subTest(failure=failure), self.assertRaises(SyncError):
                provider.add([Item("movie", {"trakt": 2})])
            with patch("time.sleep") as sleep, self.assertRaises(SyncError):
                provider.snapshot()
            sleep.assert_not_called()

    def test_postwrite_auth_quota_unsupported_and_invalid_identity_remain_terminal(self):
        for response in (ApiError("auth", 401), ApiError("quota", 429),
                         trakt_page([trakt_item(1, 1, kind="episode")], total=2)):
            responses = [response] if isinstance(response, Exception) else [trakt_meta(2), response]
            http = FakeHttp({"added": {"movies": 1}}, *responses)
            provider = Trakt(TRAKT_URL, http, "owner")
            provider.add([Item("movie", {"trakt": 2})])
            with self.subTest(response=response), patch("time.sleep") as sleep, self.assertRaises(SyncError):
                provider.snapshot()
            sleep.assert_not_called()

    def test_runner_add_once_then_settles_before_removal(self):
        a, b, c = [Item("movie", {"trakt": n}, rank=n, entry_id=1000+n) for n in [1, 2, 3]]
        initial = Snapshot(TRAKT_URL, "Films", "Description", [a, c], metadata=trakt_meta(2))
        http = FakeHttp(*complete([1, 3]), {"added": {"movies": 1}},
            trakt_meta(3), trakt_page([trakt_item(1, 1), trakt_item(2, 1), trakt_item(3, 3)]),
            *complete([1, 2, 3]), {"deleted": {"movies": 1}},
            *complete([1, 2]), *complete([1, 2]))
        provider = Trakt(TRAKT_URL, http, "owner")
        with patch("time.sleep") as sleep, patch.object(provider, "ensure_rank_sort", return_value=False), redirect_stderr(StringIO()):
            execute_targets([(provider, initial, [a, b])])
        self.assertEqual([c[1] for c in http.calls if c[0]=='POST'],
                         ["/users/owner/lists/films/items", "/users/owner/lists/films/items/remove"])
        sleep.assert_called_once_with(2)

    def test_partial_batch_sequence_tracks_only_acknowledged_batch(self):
        http = FakeHttp({"added": {"movies": 100}}, ApiError("write denied", 403),
                        *complete(list(range(1, 101))))
        provider = Trakt(TRAKT_URL, http, "owner")
        with self.assertRaises(ApiError):
            provider.add([Item("movie", {"trakt": n}) for n in range(1, 102)])
        result, sleep = self.snapshot(provider)
        self.assertEqual(len(result.items), 100)
        sleep.assert_not_called()
        self.assertEqual(sum(c[0] == "POST" for c in http.calls), 2)

    def test_repeated_membership_actions_replace_old_visibility_expectation(self):
        http = FakeHttp({"added": {"movies": 1}}, {"deleted": {"movies": 1}}, *complete([]))
        provider = Trakt(TRAKT_URL, http, "owner")
        item = Item("movie", {"trakt": 1})
        provider.add([item])
        provider.remove([item])
        result, sleep = self.snapshot(provider)
        self.assertEqual(result.items, [])
        sleep.assert_not_called()

    def test_postwrite_malformed_id_later_in_duplicate_page_remains_terminal(self):
        broken = trakt_item(3, 3)
        broken['movie']['ids'] = {"trakt": "invalid"}
        http = FakeHttp({"added": {"movies": 1}}, trakt_meta(3),
                        trakt_page([trakt_item(1, 1), trakt_item(2, 1), broken]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 2})])
        with patch("time.sleep") as sleep, self.assertRaises(SyncError):
            provider.snapshot()
        sleep.assert_not_called()

    def test_runner_settling_exhaustion_does_not_repeat_add_or_remove_old_item(self):
        a, b = Item("movie", {"trakt": 1}), Item("movie", {"trakt": 2})
        initial = Snapshot(TRAKT_URL, "Films", "Description", [a], metadata=trakt_meta(1))
        bad = [trakt_meta(2), trakt_page([trakt_item(1, 1), trakt_item(2, 1)])]
        http = FakeHttp(*complete([1]), {"added": {"movies": 1}}, *(bad * 5))
        provider = Trakt(TRAKT_URL, http, "owner")
        with patch("time.sleep"), patch.object(provider,"ensure_rank_sort",return_value=False), \
                redirect_stderr(StringIO()), self.assertRaises(SyncError):
            execute_targets([(provider, initial, [b])])
        self.assertEqual([c[1] for c in http.calls if c[0]=='POST'],
                         ["/users/owner/lists/films/items"])

    def test_overlapping_ranks_never_hide_conflicting_supported_ids(self):
        one, two = trakt_item(1, 1), trakt_item(2, 1)
        two['movie']['ids']['tmdb'] = 1
        http = FakeHttp({"added": {"movies": 1}}, trakt_meta(2), trakt_page([one, two]),
                        *complete([1, 2]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 2})])
        with patch("time.sleep") as sleep, self.assertRaisesRegex(SyncError, "Conflicting"):
            provider.snapshot()
        sleep.assert_not_called()
        self.assertEqual(len(http.calls), 3)

    def test_overlapping_ranks_never_hide_invalid_addition_timestamp(self):
        one, two = trakt_item(1, 1), trakt_item(2, 1, "not-a-date")
        http = FakeHttp({"added": {"movies": 1}}, trakt_meta(2), trakt_page([one, two]),
                        *complete([1, 2]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 2})])
        with patch("time.sleep") as sleep, self.assertRaisesRegex(SyncError, "timestamp"):
            provider.snapshot(order="added")
        sleep.assert_not_called()

    def test_pending_identity_aliases_survive_repeated_add_then_remove_by_other_alias(self):
        http = FakeHttp({"added": {"movies": 1}}, {"added": {"movies": 0}, "existing": {"movies": 1}},
                        {"deleted": {"movies": 1}}, *complete([]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 1, "tmdb": 1})])
        provider.add([Item("movie", {"trakt": 1})])
        provider.remove([Item("movie", {"tmdb": 1})])
        result, sleep = self.snapshot(provider)
        self.assertEqual(result.items, [])
        sleep.assert_not_called()

    def test_large_acknowledgement_tracking_does_not_rescan_all_prior_items(self):
        from unittest.mock import Mock
        http = FakeHttp(*[{"added": {"movies": 100}} for _ in range(10)])
        provider = Trakt(TRAKT_URL, http, "owner")
        alias_method = Item.aliases
        calls = Mock()
        def counted(item):
            calls()
            return alias_method(item)
        with patch.object(Item, 'aliases', counted):
            provider.add([Item('movie', {'trakt': i, 'tmdb': i}) for i in range(1,1001)])
        self.assertLess(calls.call_count, 20000)

    def test_pending_alias_bridge_merges_components_with_latest_action(self):
        http = FakeHttp({"added": {"movies": 2}}, {"deleted": {"movies": 1}}, *complete([]))
        provider = Trakt(TRAKT_URL, http, "owner")
        provider.add([Item("movie", {"trakt": 1}), Item("movie", {"tmdb": 1})])
        provider.remove([Item("movie", {"trakt": 1, "tmdb": 1})])
        result, sleep = self.snapshot(provider)
        self.assertEqual(result.items, [])
        sleep.assert_not_called()

    def test_cross_page_conflicting_identity_never_hidden_by_overlap(self):
        one, conflicting = trakt_item(1, 1), trakt_item(2, 1)
        conflicting['movie']['ids']['tmdb'] = 1
        http = FakeHttp({"added": {"movies": 1}}, trakt_meta(2),
                        trakt_page([one],1,2,2),trakt_page([conflicting],2,2,2))
        provider = Trakt(TRAKT_URL,http,"owner")
        provider.add([Item("movie", {"trakt": 2})])
        with patch("time.sleep") as sleep, self.assertRaisesRegex(SyncError,"Conflicting"):
            provider.snapshot()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()