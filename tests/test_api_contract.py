"""Real provider contracts for partition-confirmed reads and acknowledged writes."""

from contextlib import redirect_stderr
from copy import deepcopy
from io import StringIO
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.model import Item, Snapshot, SyncError
from syncer.providers import Trakt
from test_providers import TRAKT_URL, trakt_item, trakt_meta


class ListApi:
    """Route-aware API: fixtures model server limits, generations and write ACKs."""

    def __init__(self, count=430):
        self.rows = [trakt_item(n, n) for n in range(1, count + 1)]
        self.meta = dict(trakt_meta(count), sort_by="rank", sort_how="asc")
        self.calls = []
        self.clamp = 250
        self.hybrid = False
        self.bad_header = None
        self.stale_summary = False

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, deepcopy(params), deepcopy(body)))
        if method == "POST":
            if path.endswith("/reorder"):
                by_id = {row["id"]: row for row in self.rows}
                self.rows = [by_id[entry] for entry in body["rank"]]
                for rank, row in enumerate(self.rows, 1):
                    row["rank"] = rank
                return {"updated": len(self.rows), "list": self.summary()}, {}
            if path.endswith("/remove"):
                ids = {item["ids"]["trakt"] for item in body["movies"]}
                self.rows = [row for row in self.rows if row["movie"]["ids"]["trakt"] not in ids]
                field = "deleted"
            else:
                for item in body["movies"]:
                    self.rows.append(trakt_item(item["ids"]["trakt"],
                        max((row["rank"] for row in self.rows), default=0) + 1))
                field = "added"
            if not self.stale_summary:
                self.meta["item_count"] = len(self.rows)
            return {field: {"movies": len(body["movies"])}, "list": self.summary()}, {}
        if "/items" not in path:
            return deepcopy(self.meta), {}
        limit = min(params["limit"], self.clamp)
        page = params["page"]
        rows = deepcopy(self.rows)
        if self.hybrid:
            # The primary traversal is a same-count hybrid; the other partition
            # returns generation B. All individual pages are valid and unique.
            if params["limit"] == 250:
                rows[300] = trakt_item(10301, 301)
            else:
                rows[0] = trakt_item(10001, 1)
                rows[300] = trakt_item(10301, 301)
        total = len(rows)
        headers = {"X-Pagination-Page": str(page), "X-Pagination-Limit": str(limit),
                   "X-Pagination-Page-Count": str((total + limit - 1) // limit),
                   "X-Pagination-Item-Count": str(total),
                   "X-Sort-By": "rank", "X-Sort-How": "asc"}
        if self.bad_header:
            headers.pop(self.bad_header, None)
        return rows[(page - 1) * limit:page * limit], headers

    def summary(self):
        return {"item_count": len(self.rows), "updated_at": "2026-11-06T15:01:47.000Z"}


class ApiContractTests(unittest.TestCase):
    def read(self, provider):
        with patch("time.sleep"), redirect_stderr(StringIO()):
            return provider.snapshot(order="rank")

    def test_explicit_sort_effective_limits_and_independent_partition(self):
        api = ListApi()
        api.clamp = 200
        snapshot = self.read(Trakt(TRAKT_URL, api, "owner"))
        requests = [call[2] for call in api.calls if "/items" in call[1]]
        self.assertTrue(all(call[1].endswith("/items/movie,show,season,episode/rank/asc")
                            for call in api.calls if "/items" in call[1]))
        self.assertEqual(len(snapshot.items), 430)
        self.assertTrue(all(set(p) == {"page", "limit"} for p in requests))
        self.assertEqual(requests[0]["limit"], 250)
        self.assertGreaterEqual(len({p["limit"] for p in requests}), 2)
        self.assertEqual([p["page"] for p in requests[:3]], [1, 2, 3])

    def test_same_count_mixed_generations_never_escape(self):
        api = ListApi()
        api.hybrid = True
        with self.assertRaisesRegex(SyncError, "confirm|partition|changed"):
            self.read(Trakt(TRAKT_URL, api, "owner"))
        self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_effective_limit_is_required_not_guessed(self):
        api = ListApi(2)
        api.bad_header = "X-Pagination-Limit"
        with self.assertRaises(SyncError):
            self.read(Trakt(TRAKT_URL, api, "owner"))
        self.assertEqual(len(api.calls), 2)

    def test_invalid_rank_or_server_order_are_not_locally_repaired(self):
        for ranks in ([0, 3], [2, 1]):
            api = ListApi(2)
            for row, rank in zip(api.rows, ranks):
                row["rank"] = rank
            with self.subTest(ranks=ranks), self.assertRaises(SyncError):
                self.read(Trakt(TRAKT_URL, api, "owner"))

    def test_acknowledged_add_can_verify_against_confirmed_items_with_stale_summary(self):
        api = ListApi(250)
        provider = Trakt(TRAKT_URL, api, "owner")
        self.read(provider)
        api.stale_summary = True
        provider.add([Item("movie", {"trakt": 251})])
        snapshot = self.read(provider)
        self.assertEqual(len(snapshot.items), 251)
        # Do not manufacture fresh summary metadata, and keep confirmation context
        # available for the runner's final checkpoint until the summary catches up.
        self.assertEqual(snapshot.metadata["item_count"], 250)
        self.assertEqual(len(self.read(provider).items), 251)
        self.assertEqual(sum(call[0] == "POST" for call in api.calls), 1)

    def test_acknowledgement_cannot_hide_unexpected_same_count_membership(self):
        api = ListApi(250)
        provider = Trakt(TRAKT_URL, api, "owner")
        self.read(provider)
        api.stale_summary = True
        provider.add([Item("movie", {"trakt": 251})])
        api.rows[0] = trakt_item(9001, 1)
        with self.assertRaises(SyncError):
            self.read(provider)

    def test_stale_summary_without_own_write_still_fails(self):
        api = ListApi(251)
        api.meta["item_count"] = 250
        with self.assertRaises(SyncError):
            self.read(Trakt(TRAKT_URL, api, "owner"))

    def test_reorder_uses_supplied_snapshot_entry_ids_without_hidden_get(self):
        api = ListApi(2)
        provider = Trakt(TRAKT_URL, api, "owner")
        snapshot = self.read(provider)
        api.calls.clear()
        provider.reorder(list(reversed(snapshot.items)), snapshot)
        self.assertEqual([call[0] for call in api.calls], ["POST"])
        self.assertEqual(api.calls[0][3], {"rank": [1002, 1001]})

    def test_wrong_snapshot_reorder_fails_before_request(self):
        api = ListApi(1)
        provider = Trakt(TRAKT_URL, api, "owner")
        wrong = Snapshot("https://trakt.tv/users/owner/lists/other", "", "",
                         [Item("movie", {"trakt": 1}, entry_id=1001)])
        with self.assertRaises(SyncError):
            provider.reorder(wrong.items, wrong)
        self.assertEqual(api.calls, [])

    def test_ack_summary_count_is_evidence_not_authority(self):
        api = ListApi(250)
        provider = Trakt(TRAKT_URL, api, "owner")
        self.read(provider)
        api.stale_summary = True
        with patch.object(api, "summary", return_value={"item_count": 999, "updated_at": "2026-11-06T15:01:47Z"}):
            provider.add([Item("movie", {"trakt": 251})])
        with self.assertRaises(SyncError):
            self.read(provider)

    def test_acknowledged_remove_with_stale_summary_requires_exact_membership(self):
        api = ListApi(251)
        provider = Trakt(TRAKT_URL, api, "owner")
        self.read(provider)
        api.stale_summary = True
        provider.remove([Item("movie", {"trakt": 251})])
        result = self.read(provider)
        self.assertEqual(len(result.items), 250)
        self.assertEqual(result.metadata["item_count"], 251)
        self.assertNotIn(251, [item.ids["trakt"] for item in result.items])

    def test_own_write_cannot_hide_concurrent_metadata_change(self):
        api = ListApi(250)
        provider = Trakt(TRAKT_URL, api, "owner")
        self.read(provider)
        api.stale_summary = True
        provider.add([Item("movie", {"trakt": 251})])
        api.meta["privacy"] = "public"
        with self.assertRaises(SyncError):
            self.read(provider)

    def test_confirmation_effective_partition_must_actually_differ(self):
        api = ListApi(430)
        api.clamp = 1
        with self.assertRaisesRegex(SyncError, "partition"):
            self.read(Trakt(TRAKT_URL, api, "owner"))

    def test_confirmation_selects_smaller_partition_when_server_caps_at_100(self):
        api = ListApi(430)
        api.clamp = 100
        result = self.read(Trakt(TRAKT_URL, api, "owner"))
        self.assertEqual(len(result.items), 430)
        limits = {call[2]["limit"] for call in api.calls if "/items" in call[1]}
        self.assertIn(99, limits)

    def test_acknowledged_summary_does_not_recover_without_validated_base(self):
        api = ListApi(251)
        api.meta["item_count"] = 250
        provider = Trakt(TRAKT_URL, api, "owner")
        api.stale_summary = True
        provider.add([Item("movie", {"trakt": 252})])
        with self.assertRaises(SyncError):
            self.read(provider)


if __name__ == "__main__":
    unittest.main()