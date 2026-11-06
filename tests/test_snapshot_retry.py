"""Restart only unstable read snapshots; never replay writes or suppress errors."""

from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.http import ApiError
from syncer.model import Item, Snapshot, SyncError
from syncer.providers import MDBList, Trakt
from syncer.runner import execute_targets
from test_providers import (FakeHttp, MDB_URL, TRAKT_URL, mdb_meta, mdb_item,
                            mdb_page, trakt_meta, trakt_item, trakt_page)


class SnapshotRetryTests(unittest.TestCase):
    def read(self, provider, order="rank"):
        output = StringIO()
        with patch("time.sleep") as sleep, redirect_stderr(output):
            snapshot = provider.snapshot(order=order)
        self.assertTrue(all(call[0] == "GET" for call in provider.http.calls))
        return snapshot, sleep, output.getvalue()

    def test_trakt_total_change_restarts_metadata_and_page_one_discards_partial(self):
        old, new = trakt_meta(2), trakt_meta(1)
        http = FakeHttp(old, trakt_page([trakt_item(99, 1)], 1, 2, 2),
                        trakt_page([trakt_item(2, 2)], 2, 2, 3),
                        new, trakt_page([trakt_item(3, 1)], total=1), new)
        snapshot, sleep, output = self.read(Trakt(TRAKT_URL, http, "owner"))
        self.assertEqual([item.ids["trakt"] for item in snapshot.items], [3])
        self.assertEqual([call[2]["page"] for call in http.calls if "/items" in call[1]], [1, 2, 1])
        sleep.assert_called_once_with(1)
        self.assertIn("trakt", output.lower())
        self.assertNotIn(TRAKT_URL, output)

    def test_trakt_page_count_change_restarts_whole_read(self):
        meta = trakt_meta(3)
        http = FakeHttp(meta, trakt_page([trakt_item(99, 1)], 1, 3, 3),
                        trakt_page([trakt_item(2, 2)], 2, 2, 3),
                        meta, trakt_page([trakt_item(3, 1), trakt_item(4, 2), trakt_item(5, 3)]), meta)
        snapshot, sleep, _ = self.read(Trakt(TRAKT_URL, http, "owner"))
        self.assertEqual([i.ids["trakt"] for i in snapshot.items], [3, 4, 5])
        sleep.assert_called_once_with(1)

    def test_metadata_change_retries_then_returns_only_fresh_snapshot_both_providers(self):
        for cls, url, metadata, row, page in (
            (Trakt, TRAKT_URL, trakt_meta, lambda n: trakt_item(n, 1), trakt_page),
            (MDBList, MDB_URL, mdb_meta, mdb_item, mdb_page),
        ):
            before = metadata(1)
            after = dict(before, updated_at="v2")
            http = FakeHttp(before, page([row(99)]), after,
                            after, page([row(3)]), after)
            with self.subTest(provider=cls.__name__):
                snapshot, sleep, _ = self.read(cls(url, http, "owner"))
                self.assertEqual([i.ids["tmdb"] for i in snapshot.items], [3])
                sleep.assert_called_once_with(1)

    def test_mdblist_cursor_restart_does_not_reuse_old_cursor_or_items(self):
        http = FakeHttp(mdb_meta(2), mdb_page([mdb_item(99)], 2, "old-cursor"),
                        mdb_page([mdb_item(2)], 1), mdb_meta(2),
                        mdb_page([mdb_item(3)], 2, "new-cursor"),
                        mdb_page([mdb_item(4)], 2), mdb_meta(2))
        snapshot, sleep, _ = self.read(MDBList(MDB_URL, http))
        calls = [call for call in http.calls if call[1].endswith("/items")]
        self.assertEqual([call[2].get("cursor") for call in calls],
                         [None, "old-cursor", None, "new-cursor"])
        self.assertEqual([i.ids["tmdb"] for i in snapshot.items], [3, 4])
        sleep.assert_called_once_with(1)

    def test_exhaustion_is_three_attempts_two_sleeps_and_no_partial_return(self):
        for cls, url, metadata, row, page in (
            (Trakt, TRAKT_URL, trakt_meta, lambda n: trakt_item(n, 1), trakt_page),
            (MDBList, MDB_URL, mdb_meta, mdb_item, mdb_page),
        ):
            responses = []
            for version in range(3):
                before = dict(metadata(1), updated_at=f"v{version}")
                after = dict(before, updated_at=f"v{version+1}")
                responses.extend([before, page([row(version+1)]), after])
            http = FakeHttp(*responses)
            with self.subTest(provider=cls.__name__), patch("time.sleep") as sleep, \
                    redirect_stderr(StringIO()), self.assertRaises(SyncError):
                cls(url, http, "owner").snapshot()
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
            self.assertEqual(len(http.calls), 9)
            self.assertTrue(all(call[0] == "GET" for call in http.calls))

    def test_successful_read_uses_no_delay_or_extra_requests(self):
        for cls, url, metadata, row, page in (
            (Trakt, TRAKT_URL, trakt_meta, lambda n: trakt_item(n, 1), trakt_page),
            (MDBList, MDB_URL, mdb_meta, mdb_item, mdb_page),
        ):
            http = FakeHttp(metadata(1), page([row(1)]), metadata(1))
            with self.subTest(provider=cls.__name__):
                _, sleep, output = self.read(cls(url, http, "owner"))
                sleep.assert_not_called()
                self.assertEqual(output, "")
                self.assertEqual(len(http.calls), 3)

    def test_auth_rate_limit_transport_and_nonretryable_validation_errors_not_retried(self):
        failures = [ApiError("unauthorized", 401), ApiError("quota", 429),
                    ApiError("transport already exhausted"), SyncError("Invalid identity")]
        for failure in failures:
            for cls, url in ((Trakt, TRAKT_URL), (MDBList, MDB_URL)):
                http = FakeHttp(failure)
                with self.subTest(error=type(failure).__name__, provider=cls.__name__), \
                        patch("time.sleep") as sleep:
                    with self.assertRaises(type(failure)) as caught:
                        cls(url, http, "owner").snapshot()
                    self.assertIs(caught.exception, failure)
                sleep.assert_not_called()
                self.assertEqual(len(http.calls), 1)

    def test_wrong_page_missing_headers_duplicates_and_unsupported_types_are_terminal(self):
        invalid = [
            [trakt_meta(1), trakt_page([trakt_item(1, 1)], page=2)],
            [trakt_meta(1), ([trakt_item(1, 1)], {})],
            [trakt_meta(2), trakt_page([trakt_item(1, 1), trakt_item(1, 2)]), trakt_meta(2)],
            [trakt_meta(2), trakt_page([trakt_item(1, 1), trakt_item(2, 1)]), trakt_meta(2)],
            [trakt_meta(1), trakt_page([trakt_item(1, 1, kind="season")])],
        ]
        for responses in invalid:
            http = FakeHttp(*responses)
            with self.subTest(responses=responses), patch("time.sleep") as sleep, \
                    self.assertRaises(SyncError):
                Trakt(TRAKT_URL, http, "owner").snapshot()
            sleep.assert_not_called()
            self.assertLessEqual(len(http.calls), len(responses))

    def test_mutations_are_not_replayed_by_snapshot_retry(self):
        failure = SyncError("Trakt pagination changed during snapshot")
        provider = Trakt(TRAKT_URL, FakeHttp(failure), "owner")
        with patch("time.sleep") as sleep, self.assertRaises(SyncError):
            provider.add([Item("movie", {"trakt": 1})])
        self.assertEqual(len(provider.http.calls), 1)
        self.assertEqual(provider.http.calls[0][0], "POST")
        sleep.assert_not_called()

    def test_exhausted_snapshot_during_preflight_prevents_any_item_write(self):
        class Provider(Trakt):
            def preflight(self, snapshot):
                pass

        before = trakt_meta(1)
        after = dict(before, updated_at="v2")
        http = FakeHttp(*([before, trakt_page([trakt_item(1, 1)]), after] * 3))
        provider = Provider(TRAKT_URL, http, "owner")
        initial = Snapshot(TRAKT_URL, "Films", "Description", [Item("movie", {"trakt": 1})])
        with patch("time.sleep"), redirect_stderr(StringIO()), self.assertRaises(SyncError):
            execute_targets([(provider, initial, [])])
        self.assertEqual(len(http.calls), 9)
        self.assertTrue(all(call[0] == "GET" for call in http.calls))

    def test_failure_after_restart_is_not_hidden_or_followed_by_more_attempts(self):
        failure = ApiError("Authentication lost", 401)
        http = FakeHttp(trakt_meta(1), trakt_page([trakt_item(1, 1), trakt_item(2, 2)], total=2), failure)
        with patch("time.sleep") as sleep, redirect_stderr(StringIO()), self.assertRaises(ApiError) as caught:
            Trakt(TRAKT_URL, http, "owner").snapshot()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(http.calls), 3)
        sleep.assert_called_once_with(1)

    def test_third_attempt_can_succeed_with_requested_newest_first_order(self):
        meta = trakt_meta(2)
        first = trakt_item(1, 1, "2026-01-01T00:00:00Z")
        newest = trakt_item(2, 2, "2026-02-01T00:00:00Z")
        http = FakeHttp(meta, trakt_page([trakt_item(99, 1)], total=1),
                        meta, trakt_page([trakt_item(98, 1)], total=1),
                        meta, trakt_page([first, newest]), meta)
        snapshot, sleep, output = self.read(Trakt(TRAKT_URL, http, "owner"), order="added")
        self.assertEqual([i.ids["trakt"] for i in snapshot.items], [2, 1])
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])
        self.assertEqual(output.count("restarting full snapshot"), 2)
        self.assertEqual(len(http.calls), 7)

    def test_mdblist_malformed_cursor_unsupported_media_and_excess_rows_are_terminal(self):
        scenarios = [
            [mdb_meta(2), mdb_page([mdb_item(1)], 2, "repeat"),
             mdb_page([mdb_item(2)], 2, "repeat")],
            [mdb_meta(1), mdb_page([mdb_item(1, "episode")])],
            [mdb_meta(1), mdb_page([mdb_item(1), mdb_item(2)], 1)],
            [mdb_meta(1), ([mdb_item(1)], {"x-total-items": "1"})],
        ]
        for responses in scenarios:
            http = FakeHttp(*responses)
            with self.subTest(responses=responses), patch("time.sleep") as sleep, self.assertRaises(SyncError):
                MDBList(MDB_URL, http).snapshot()
            sleep.assert_not_called()
            self.assertLessEqual(len(http.calls), len(responses))

    def test_repeated_cursor_is_rejected_for_cursor_not_excess_count(self):
        http = FakeHttp(mdb_meta(2), mdb_page([mdb_item(1)], 2, "same"),
                        mdb_page([mdb_item(2)], 2, "same"))
        with patch("time.sleep") as sleep, self.assertRaisesRegex(SyncError, "cursor.*repeated"):
            MDBList(MDB_URL, http).snapshot()
        sleep.assert_not_called()

    def test_trakt_count_change_does_not_hide_terminal_page_content(self):
        invalid_pages = [
            trakt_page([trakt_item(1, 1, kind="episode")], total=2),
            trakt_page([None], total=2),
            trakt_page([trakt_item(1, 1)], pages=0, total=2),
            trakt_page([trakt_item(1, 1), trakt_item(1, 2)], total=2),
            trakt_page([trakt_item(1, 1), trakt_item(2, 1)], total=2),
        ]
        for bad_page in invalid_pages:
            http = FakeHttp(trakt_meta(1), bad_page, trakt_meta(1),
                            trakt_page([trakt_item(3, 1)]), trakt_meta(1))
            with self.subTest(page=bad_page), patch("time.sleep") as sleep, self.assertRaises(SyncError):
                Trakt(TRAKT_URL, http, "owner").snapshot()
            sleep.assert_not_called()
            self.assertEqual(len(http.calls), 2)

    def test_metadata_change_never_hides_duplicate_identity_or_rank(self):
        cases = [
            (Trakt, TRAKT_URL, trakt_meta, trakt_page,
             [trakt_item(1, 1), trakt_item(1, 2)], [trakt_item(3, 1), trakt_item(4, 2)]),
            (Trakt, TRAKT_URL, trakt_meta, trakt_page,
             [trakt_item(1, 1), trakt_item(2, 1)], [trakt_item(3, 1), trakt_item(4, 2)]),
            (MDBList, MDB_URL, mdb_meta, mdb_page,
             [mdb_item(1), mdb_item(1)], [mdb_item(3), mdb_item(4)]),
        ]
        for cls, url, metadata, page, bad, good in cases:
            before = metadata(2)
            after = dict(before, updated_at="v2")
            http = FakeHttp(before, page(bad), after, after, page(good), after)
            with self.subTest(provider=cls.__name__, bad=bad), patch("time.sleep") as sleep, \
                    self.assertRaises(SyncError):
                cls(url, http, "owner").snapshot()
            sleep.assert_not_called()
            self.assertLessEqual(len(http.calls), 3)

    def test_mdblist_count_change_never_hides_malformed_end_or_repeated_cursor(self):
        bad_end = mdb_page([mdb_item(1)], 2)
        bad_end["pagination"]["next_cursor"] = "unexpected-at-end"
        cases = [
            [mdb_meta(1), bad_end],
            [mdb_meta(2), mdb_page([mdb_item(1)], 2, "same"),
             mdb_page([mdb_item(2)], 3, "same")],
            [mdb_meta(1), mdb_page([mdb_item(1), mdb_item(1)], 2)],
        ]
        for bad in cases:
            http = FakeHttp(*bad, mdb_meta(1), mdb_page([mdb_item(3)]), mdb_meta(1))
            with self.subTest(responses=bad), patch("time.sleep") as sleep, self.assertRaises(SyncError):
                MDBList(MDB_URL, http).snapshot()
            sleep.assert_not_called()
            self.assertLessEqual(len(http.calls), len(bad))

    def test_incomplete_final_count_stays_terminal_even_when_metadata_changes(self):
        for cls, url, metadata, row, page in (
            (Trakt, TRAKT_URL, trakt_meta, lambda n: trakt_item(n, 1), trakt_page),
            (MDBList, MDB_URL, mdb_meta, mdb_item, mdb_page),
        ):
            before = metadata(2)
            after = dict(before, updated_at="v2")
            http = FakeHttp(before, page([row(1)], total=2), after,
                            after, page([row(2), row(3)], total=2), after)
            with self.subTest(provider=cls.__name__), patch("time.sleep") as sleep, self.assertRaises(SyncError):
                cls(url, http, "owner").snapshot()
            sleep.assert_not_called()

    def test_drift_never_hides_rows_exceeding_pages_own_total(self):
        for cls, url, metadata, row, page in (
            (Trakt, TRAKT_URL, trakt_meta, lambda n: trakt_item(n, n), trakt_page),
            (MDBList, MDB_URL, mdb_meta, mdb_item, mdb_page),
        ):
            http = FakeHttp(metadata(1), page([row(1), row(2), row(3)], total=2),
                            metadata(1), page([row(4)]), metadata(1))
            with self.subTest(provider=cls.__name__), patch("time.sleep") as sleep, self.assertRaises(SyncError):
                cls(url, http, "owner").snapshot()
            sleep.assert_not_called()
            self.assertEqual(len(http.calls), 2)

    def test_trakt_empty_nonfinal_page_is_terminal_even_when_total_changes(self):
        http = FakeHttp(trakt_meta(1), trakt_page([], page=1, pages=2, total=2),
                        trakt_meta(1), trakt_page([trakt_item(1, 1)]), trakt_meta(1))
        with patch("time.sleep") as sleep, self.assertRaises(SyncError):
            Trakt(TRAKT_URL, http, "owner").snapshot()
        sleep.assert_not_called()
        self.assertEqual(len(http.calls), 2)


if __name__ == "__main__":
    unittest.main()