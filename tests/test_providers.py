"""Offline HTTP and provider contracts. All credentials and responses are fixtures."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.http import ApiError, Http
from syncer.model import Item, Snapshot, SyncError
from syncer.providers import MDBList, Trakt, make_provider


MDB_URL = "https://mdblist.com/lists/owner/films"
TRAKT_URL = "https://trakt.tv/users/owner/lists/films"


class Response(BytesIO):
    def __init__(self, value=b'{}', headers=None):
        super().__init__(value)
        self.headers = headers or {}


def http_error(status, headers=None):
    return HTTPError("https://example.test/?apikey=fixture-secret", status,
                     "fixture-secret", headers or {}, BytesIO(b"fixture-secret"))


class HttpTests(unittest.TestCase):
    def test_json_defaults_headers_params_timeout(self):
        client = Http("https://example.test", {"Authorization": "Bearer fixture"},
                      {"apikey": "fixture", "page": 1})
        with patch("syncer.http.urlopen", return_value=Response(b'{"ok":true}', {"X-Test": "yes"})) as send:
            value, headers = client.request("PUT", "/lists/1", {"page": 2}, {"name": "film"})
        req = send.call_args.args[0]
        self.assertEqual(value, {"ok": True})
        self.assertEqual(headers["X-Test"], "yes")
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.get_header("User-agent"), "Mozilla/5.0")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(parse_qs(urlsplit(req.full_url).query), {"apikey": ["fixture"], "page": ["2"]})
        self.assertEqual(send.call_args.kwargs["timeout"], 30)
        self.assertIn(b'"name"', req.data)

    def test_safe_errors_never_show_credentials_or_server_body(self):
        for failure in (http_error(403), URLError("fixture-secret"), socket.timeout("fixture-secret")):
            with self.subTest(failure=type(failure).__name__), patch("syncer.http.urlopen", side_effect=failure), patch("syncer.http.time.sleep"):
                with self.assertRaises(ApiError) as caught:
                    Http("https://example.test", params={"apikey": "fixture-secret"}).request("GET", "/list")
                self.assertIsInstance(caught.exception, SyncError)
                self.assertNotIn("fixture-secret", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    def test_get_put_and_only_idempotent_list_posts_retry(self):
        cases = [("GET", "/x", True), ("PUT", "/x", True),
                 ("POST", "/users/owner/lists/1/items", True),
                 ("POST", "/users/owner/lists/1/items/remove", True),
                 ("POST", "/lists/1/items/add", True),
                 ("POST", "/lists/1/items/reorder", True),
                 ("POST", "/oauth/token", False),
                 ("POST", "/users/owner/lists", False), ("DELETE", "/lists/1", False)]
        for method, path, retry in cases:
            with self.subTest(method=method, path=path), patch("syncer.http.urlopen", side_effect=[http_error(503), Response()]) as send, patch("syncer.http.time.sleep"):
                if retry:
                    Http("https://example.test").request(method, path, body={})
                    self.assertEqual(send.call_count, 2)
                else:
                    with self.assertRaises(ApiError):
                        Http("https://example.test").request(method, path, body={})
                    self.assertEqual(send.call_count, 1)

    def test_retry_after_bounded_and_attempts_finite(self):
        with patch("syncer.http.urlopen", side_effect=lambda *a, **k: http_failure()) as send, patch("syncer.http.time.sleep") as sleep:
            with self.assertRaises(ApiError):
                Http("https://example.test").request("GET", "/x")
        self.assertEqual(send.call_count, 1)
        sleep.assert_not_called()  # Never retry before the server's long quota reset.
        with patch("syncer.http.urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(http_error(503))) as send, patch("syncer.http.time.sleep") as sleep:
            with self.assertRaises(ApiError):
                Http("https://example.test").request("GET", "/x")
        self.assertEqual(send.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(0 <= call.args[0] <= 120 for call in sleep.call_args_list))

    def test_retry_after_http_date_and_malformed_fallback(self):
        for value in (format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60)), "invalid", "NaN"):
            with self.subTest(value=value), patch("syncer.http.urlopen", side_effect=[http_error(429, {"Retry-After": value}), Response()]), patch("syncer.http.time.sleep") as sleep:
                Http("https://example.test").request("GET", "/x")
                self.assertTrue(0 < sleep.call_args.args[0] <= 60)

    def test_incomplete_response_is_safe_and_retry_bounded(self):
        with patch("syncer.http.urlopen", side_effect=IncompleteRead(b"fixture-secret")) as send, patch("syncer.http.time.sleep"):
            with self.assertRaises(ApiError) as caught:
                Http("https://example.test").request("GET", "/x")
            self.assertNotIn("fixture-secret", str(caught.exception))
            self.assertEqual(send.call_count, 3)

    def test_retry_after_used_and_bad_json_is_safe(self):
        with patch("syncer.http.urlopen", side_effect=[http_error(429, {"Retry-After": "7"}), Response()]), patch("syncer.http.time.sleep") as sleep:
            Http("https://example.test").request("GET", "/x")
            sleep.assert_called_once_with(7)
        with patch("syncer.http.urlopen", return_value=Response(b"fixture-secret")):
            with self.assertRaises(ApiError) as caught:
                Http("https://example.test").request("GET", "/x")
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_redirects_are_not_followed_with_credentials(self):
        from syncer.http import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(Mock(), Mock(), 302, "", {}, "https://evil.test"))

    def test_http_rejects_unsafe_base_or_path(self):
        for base in ("http://example.test", "https://user:pass@example.test", "https://example.test?secret=x"):
            with self.subTest(base=base), self.assertRaises(SyncError):
                Http(base)
        for path in ("https://evil.test", "//evil.test", "/../oauth", "/x?apikey=other"):
            with self.subTest(path=path), self.assertRaises(SyncError):
                Http("https://example.test").request("GET", path)


def http_failure():
    raise http_error(429, {"Retry-After": "999999"})


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path, deepcopy(params), deepcopy(body)))
        if not self.responses:
            raise AssertionError("Unexpected request: " + path)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response) if isinstance(response, tuple) else (deepcopy(response), {})


def mdb_meta(count=2, **extra):
    return dict(id=12, name="Films", description="Description", user_id=9,
                user_name="owner", dynamic=False, items=count, updated_at="v1", **extra)


def mdb_item(n, kind="movie", **extra):
    return dict(id=n, ids={"tmdb": n, "imdb": f"tt{n:07}"}, mediatype=kind,
                rank=n, added_at=None, **extra)


def mdb_page(items, total=None, cursor=None):
    return {"movies": items, "shows": [], "pagination": {
        "total": len(items) if total is None else total,
        "has_more": cursor is not None, "next_cursor": cursor}}


def trakt_meta(count=2):
    return {"ids": {"trakt": 42, "slug": "films"}, "name": "Films",
            "description": "Description", "item_count": count, "updated_at": "v1",
            "user": {"username": "owner", "ids": {"slug": "owner"}}}


def trakt_item(n, rank, date="2026-01-01T00:00:00Z", kind="movie"):
    return {"id": 1000 + n, "type": kind, "rank": rank, "listed_at": date,
            kind: {"ids": {"trakt": n, "tmdb": n, "imdb": f"tt{n:07}"}}}


def trakt_page(items, page=1, pages=1, total=None):
    return (items, {"X-Pagination-Page": str(page), "X-Pagination-Page-Count": str(pages),
                    "X-Pagination-Item-Count": str(len(items) if total is None else total)})


class URLTests(unittest.TestCase):
    def test_factory_and_strict_parsing(self):
        self.assertIsInstance(make_provider(MDB_URL, FakeHttp(), "owner"), MDBList)
        self.assertIsInstance(make_provider(TRAKT_URL, FakeHttp(), "owner"), Trakt)
        for url in ("http://mdblist.com/lists/a/b", "https://mdblist.com.evil/lists/a/b",
                    "https://user:secret@mdblist.com/lists/a/b", MDB_URL + "?apikey=x",
                    MDB_URL + "#x", "https://trakt.tv/lists/films", TRAKT_URL + "/items",
                    "https://mdblist.com:443/lists/a/b", "https://mdblist.com/lists/a/%2e%2e"):
            with self.subTest(url=url), self.assertRaises(SyncError):
                make_provider(url, FakeHttp(), "owner")


class MDBTests(unittest.TestCase):
    def test_complete_cursor_snapshot_array_metadata_null_dates_newest_first(self):
        meta = mdb_meta(3)
        http = FakeHttp([meta], mdb_page([mdb_item(3), mdb_item(1)], 3, "next"),
                        mdb_page([mdb_item(2)], 3), [meta])
        snap = MDBList(MDB_URL, http).snapshot()
        self.assertEqual([i.ids["tmdb"] for i in snap.items], [3, 1, 2])
        self.assertEqual([i.added_at for i in snap.items], [None, None, None])
        self.assertEqual((snap.name, snap.description, snap.provider), ("Films", "Description", "mdblist"))
        self.assertEqual(http.calls[1][2]["sort"], "added")
        self.assertEqual(http.calls[1][2]["order"], "asc")
        self.assertEqual(http.calls[2][2]["cursor"], "next")
        self.assertEqual(len(http.calls), 4)

    def test_rank_snapshot_requests_actual_custom_rank(self):
        http = FakeHttp(mdb_meta(), mdb_page([mdb_item(2), mdb_item(1)]), mdb_meta())
        snap = MDBList(MDB_URL, http).snapshot(order="rank")
        self.assertEqual(http.calls[1][2]["sort"], "usort")
        self.assertEqual(http.calls[1][2]["order"], "asc")
        self.assertEqual([i.ids["tmdb"] for i in snap.items], [2, 1])

    def test_live_updated_field_is_checked_and_exposed_as_version(self):
        before = mdb_meta(); before.pop("updated_at"); before["updated"] = "2026-01-01T00:00:00Z"
        after = dict(before, updated="2026-01-02T00:00:00Z")
        with self.assertRaises(SyncError):
            MDBList(MDB_URL, FakeHttp(before, mdb_page([mdb_item(1), mdb_item(2)]), after)).snapshot()
        snapshot = MDBList(MDB_URL, FakeHttp(before, mdb_page([mdb_item(1), mdb_item(2)]), before)).snapshot()
        self.assertEqual(snapshot.version, before["updated"])

    def test_unified_mixed_items_preserve_server_date_added_sequence(self):
        page = {"items": [mdb_item(2, "show"), mdb_item(1)],
                "pagination": {"total": 2, "has_more": False, "next_cursor": None}}
        snap = MDBList(MDB_URL, FakeHttp(mdb_meta(), page, mdb_meta())).snapshot()
        self.assertEqual([i.media_type for i in snap.items], ["show", "movie"])

    def test_live_unified_root_array_uses_pagination_headers(self):
        first = ([mdb_item(2, "show")], {"x-total-items": "2", "x-has-more": "true", "x-next-cursor": "abc"})
        last = ([mdb_item(1)], {"x-total-items": "2", "x-has-more": "false"})
        http = FakeHttp([mdb_meta()], first, last, [mdb_meta()])
        snap = MDBList(MDB_URL, http).snapshot()
        self.assertEqual([i.media_type for i in snap.items], ["show", "movie"])
        self.assertEqual(http.calls[1][2]["unified"], "true")
        self.assertEqual(http.calls[2][2]["cursor"], "abc")

    def test_incomplete_unstable_duplicate_and_unsupported_snapshots_fail(self):
        changed = mdb_meta(); changed["updated_at"] = "v2"
        bad_cursor = mdb_page([mdb_item(1)], 2, "same")
        scenarios = [
            [mdb_meta(), mdb_page([mdb_item(1)], 2), mdb_meta()],
            [mdb_meta(), mdb_page([mdb_item(1), mdb_item(2)]), changed],
            [mdb_meta(), mdb_page([mdb_item(1), mdb_item(1)]), mdb_meta()],
            [mdb_meta(), bad_cursor, bad_cursor],
            [mdb_meta(1), mdb_page([mdb_item(1, "episode")]), mdb_meta(1)],
            [mdb_meta(), {"movies": [], "pagination": {"total": 2, "has_more": True}}],
        ]
        for responses in scenarios:
            with self.subTest(responses=responses), self.assertRaises(SyncError):
                MDBList(MDB_URL, FakeHttp(*responses)).snapshot()

    def test_preflight_authenticates_account_and_requires_static_owned(self):
        # Factory's user can be a Trakt account; MDB ownership uses authenticated /user.
        http = FakeHttp({"user_id": 9, "username": "owner"})
        provider = MDBList(MDB_URL, http, user="different-trakt-name")
        snap = Snapshot(MDB_URL, "Films", "", [], provider="mdblist", metadata=mdb_meta(0))
        provider.preflight(snap)
        self.assertTrue(snap.writable)
        self.assertEqual(http.calls[0][:2], ("GET", "/user"))
        for metadata, account in [(dict(mdb_meta(0), dynamic=True), {"user_id": 9}),
                                  (mdb_meta(0), {"user_id": 999, "username": "someone"}),
                                  ({k: v for k, v in mdb_meta(0).items() if k != "dynamic"}, {"user_id": 9})]:
            with self.subTest(metadata=metadata), self.assertRaises(SyncError):
                MDBList(MDB_URL, FakeHttp(account)).preflight(
                    Snapshot(MDB_URL, "Films", "", [], metadata=metadata))

    def test_batches_add_remove_flat_ids_and_rejects_partial_not_found(self):
        items = [Item("movie", {"tmdb": n}) for n in range(1, 102)]
        for action, result in [("add", "added"), ("remove", "removed")]:
            http = FakeHttp({result: {"movies": 100}}, {result: {"movies": 1}})
            provider = MDBList(MDB_URL, http)
            provider.list_id = 12
            getattr(provider, action)(items)
            self.assertEqual([len(c[3]["movies"]) for c in http.calls], [100, 1])
            self.assertEqual(http.calls[0][3]["movies"][0], {"tmdb": 1})
            self.assertEqual(http.calls[0][1], f"/lists/12/items/{action}")
        for result in ({"added": {"movies": 0}}, {"added": {"movies": 1}, "not_found": {"movies": [{"tmdb": 1}]}}):
            with self.subTest(result=result), self.assertRaises(SyncError):
                provider = MDBList(MDB_URL, FakeHttp(result)); provider.list_id = 12
                provider.add([items[0]])

    def test_reorder_uses_media_ids_and_chunks_after_previous_anchor(self):
        items = [Item("movie", {"tmdb": n}) for n in range(1, 1002)]
        http = FakeHttp({"updated": 1000, "not_found": []}, {"updated": 1, "not_found": []})
        provider = MDBList(MDB_URL, http); provider.list_id = 12
        provider.reorder(items, Snapshot(MDB_URL, "", "", items, metadata=mdb_meta(1001)))
        self.assertEqual([len(c[3]["items"]) for c in http.calls], [1000, 1])
        self.assertEqual(http.calls[0][3]["position"], "top")
        self.assertEqual(http.calls[1][3]["after"], {"mediatype": "movie", "ids": {"tmdb": 1000}})


class TraktTests(unittest.TestCase):
    def test_newest_first_equal_timestamp_ties_keep_ascending_rank(self):
        rows = [trakt_item(1, 3, "2026-01-01T00:00:00Z"),
                trakt_item(2, 2, "2026-02-01T00:00:00Z"),
                trakt_item(3, 1, "2026-02-01T00:00:00Z")]
        http = FakeHttp(trakt_meta(3), trakt_page(rows), trakt_meta(3))
        result = Trakt(TRAKT_URL, http, "owner").snapshot()
        self.assertEqual([i.ids["trakt"] for i in result.items], [3, 2, 1])

    def test_default_sort_update_only_sort_fields_and_verify(self):
        initial = dict(trakt_meta(), sort_by="added", sort_how="desc")
        expected = dict(trakt_meta(), sort_by="rank", sort_how="asc")
        http = FakeHttp(expected, expected)
        provider = Trakt(TRAKT_URL, http, "owner")
        snapshot = Snapshot(TRAKT_URL, "Films", "Description", [], metadata=initial)
        self.assertTrue(provider.ensure_rank_sort(snapshot))
        self.assertEqual(http.calls[0], ("PUT", "/users/owner/lists/films", None,
                                        {"sort_by": "rank", "sort_how": "asc"}))
        self.assertEqual(http.calls[1][0], "GET")
        self.assertEqual(snapshot.metadata["sort_by"], "rank")
        self.assertEqual(snapshot.metadata["sort_how"], "asc")

    def test_default_sort_already_correct_makes_no_request(self):
        http = FakeHttp()
        snapshot = Snapshot(TRAKT_URL, "", "", [], metadata=dict(
            trakt_meta(), sort_by="rank", sort_how="asc"))
        self.assertFalse(Trakt(TRAKT_URL, http, "owner").ensure_rank_sort(snapshot))
        self.assertEqual(http.calls, [])

    def test_default_sort_rejects_failed_verification_or_nonowner_before_write(self):
        initial = dict(trakt_meta(), sort_by="added", sort_how="desc")
        snapshot = Snapshot(TRAKT_URL, "", "", [], metadata=initial)
        with self.assertRaises(SyncError):
            Trakt(TRAKT_URL, FakeHttp(initial, initial), "owner").ensure_rank_sort(snapshot)
        http = FakeHttp()
        with self.assertRaises(SyncError):
            Trakt(TRAKT_URL, http, "stranger").ensure_rank_sort(snapshot)
        self.assertEqual(http.calls, [])

    def test_add_batches_preserve_newest_first_input_sequence(self):
        items = [Item("movie", {"trakt": n}) for n in range(205, 0, -1)]
        http = FakeHttp({"added": {"movies": 100}}, {"added": {"movies": 100}},
                        {"added": {"movies": 5}})
        Trakt(TRAKT_URL, http, "owner").add(items)
        sent = [row["ids"]["trakt"] for call in http.calls for row in call[3]["movies"]]
        self.assertEqual(sent, list(range(205, 0, -1)))

    def test_paginated_snapshot_added_order_and_rank_order_are_distinct(self):
        first = trakt_item(1, 1, "2026-01-01T00:00:00Z")
        second = trakt_item(2, 2, "2026-02-01T00:00:00Z")
        for order, expected in [("added", [2, 1]), ("rank", [1, 2])]:
            http = FakeHttp(trakt_meta(), trakt_page([second], 1, 2, 2),
                            trakt_page([first], 2, 2, 2), trakt_meta())
            snap = Trakt(TRAKT_URL, http, "owner").snapshot(order=order)
            self.assertEqual([i.ids["trakt"] for i in snap.items], expected)
            self.assertEqual({i.entry_id for i in snap.items}, {1001, 1002})
            self.assertEqual(http.calls[2][2]["page"], 2)
            self.assertEqual(snap.provider, "trakt")

    def test_incomplete_unstable_duplicate_bad_rank_or_unsupported_fail(self):
        changed = trakt_meta(); changed["description"] = "Changed"
        scenarios = [
            [trakt_meta(), trakt_page([trakt_item(1, 1)], total=2), trakt_meta()],
            [trakt_meta(), trakt_page([trakt_item(1, 1), trakt_item(2, 2)]), changed],
            [trakt_meta(), trakt_page([trakt_item(1, 1), trakt_item(1, 2)]), trakt_meta()],
            [trakt_meta(), trakt_page([trakt_item(1, 1), trakt_item(2, 1)]), trakt_meta()],
            [trakt_meta(1), trakt_page([trakt_item(1, 1, kind="season")]), trakt_meta(1)],
            [trakt_meta(), ([trakt_item(1, 1)], {})],
            [trakt_meta(), trakt_page([trakt_item(1, 1)], 1, 2, 2), trakt_page([trakt_item(2, 2)], 2, 2, 3)],
        ]
        for responses in scenarios:
            with self.subTest(responses=responses), self.assertRaises(SyncError):
                Trakt(TRAKT_URL, FakeHttp(*responses), "owner").snapshot()

    def test_preflight_rejects_nonowner(self):
        snap = Snapshot(TRAKT_URL, "", "", [], metadata=trakt_meta(0))
        Trakt(TRAKT_URL, FakeHttp(), "OWNER").preflight(snap)
        self.assertTrue(snap.writable)
        with self.assertRaises(SyncError):
            Trakt(TRAKT_URL, FakeHttp(), "stranger").preflight(snap)

    def test_add_remove_batches_nested_ids_and_reject_partial(self):
        items = [Item("show", {"trakt": n}) for n in range(1, 102)]
        for action, result in [("add", "added"), ("remove", "deleted")]:
            http = FakeHttp({result: {"shows": 100}}, {result: {"shows": 1}})
            getattr(Trakt(TRAKT_URL, http, "owner"), action)(items)
            self.assertEqual([len(c[3]["shows"]) for c in http.calls], [100, 1])
            self.assertEqual(http.calls[0][3]["shows"][0], {"ids": {"trakt": 1}})
        for result in ({"added": {"shows": 0}}, {"added": {"shows": 1}, "not_found": {"shows": [{}]}}):
            with self.subTest(result=result), self.assertRaises(SyncError):
                Trakt(TRAKT_URL, FakeHttp(result), "owner").add(items[:1])

    def test_reorder_rereads_entry_ids_and_maps_canonical_representatives(self):
        http = FakeHttp({"updated": 2})
        provider = Trakt(TRAKT_URL, http, "owner")
        old = Snapshot(TRAKT_URL, "", "", [Item("movie", {"tmdb": 1}, entry_id=9)])
        latest = Snapshot(TRAKT_URL, "", "", [Item("movie", {"tmdb": 1, "trakt": 11}, entry_id=101),
                                              Item("movie", {"tmdb": 2, "trakt": 22}, entry_id=202)])
        desired = [Item("movie", {"tmdb": "2"}), Item("movie", {"trakt": "11"})]
        with patch.object(provider, "snapshot", return_value=latest) as read:
            provider.reorder(desired, old)
        read.assert_called_once_with(order="rank")
        self.assertEqual(http.calls[0][1], "/users/owner/lists/films/items/reorder")
        self.assertEqual(http.calls[0][3], {"rank": [202, 101]})

    def test_reorder_membership_mismatch_or_missing_entry_id_fails_before_post(self):
        for latest in ([Item("movie", {"tmdb": 2}, entry_id=200)], [Item("movie", {"tmdb": 1})]):
            http = FakeHttp(); provider = Trakt(TRAKT_URL, http, "owner")
            with patch.object(provider, "snapshot", return_value=Snapshot(TRAKT_URL, "", "", latest)):
                with self.assertRaises(SyncError):
                    provider.reorder([Item("movie", {"tmdb": 1})], Snapshot(TRAKT_URL, "", "", []))
            self.assertEqual(http.calls, [])

    def test_reorder_rejects_skipped_entry_ids_even_when_updated_count_matches(self):
        item = Item("movie", {"trakt": 1}, entry_id=101)
        snap = Snapshot(TRAKT_URL, "", "", [item])
        provider = Trakt(TRAKT_URL, FakeHttp({"updated": 1, "skipped_ids": [101]}), "owner")
        with patch.object(provider, "snapshot", return_value=snap), self.assertRaises(SyncError):
            provider.reorder([item], snap)

    def test_empty_writes_are_noops_and_invalid_items_rejected_before_mutation(self):
        for cls, url in ((Trakt, TRAKT_URL), (MDBList, MDB_URL)):
            http = FakeHttp(); provider = cls(url, http, "owner")
            provider.add([]); provider.remove([]); provider.reorder([], Snapshot(url, "", "", []))
            self.assertEqual(http.calls, [])
            for items in ([Item("episode", {"trakt": 1})],
                          [Item("movie", {"tmdb": 1}), Item("movie", {"tmdb": 1})]):
                with self.assertRaises(SyncError):
                    provider.add(items)
            self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()