"""Offline send-level contracts: unknown writes stop; known 429s may retry."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.client import IncompleteRead
from io import BytesIO
from pathlib import Path
import socket
import sys
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.http import ApiError, Http


SECRET = "fixture-private-value"
ITEMS = "/users/owner/lists/1/items"


class Response(BytesIO):
    def __init__(self, body=b'{}', headers=None, read_failure=None):
        super().__init__(body)
        self.headers = headers or {}
        self.read_failure = read_failure

    def read(self, *args):
        if self.read_failure is not None:
            raise self.read_failure
        return super().read(*args)


def rejected(status, headers=None):
    return HTTPError("https://example.test/?apikey=" + SECRET, status,
                     SECRET, headers or {}, BytesIO(SECRET.encode()))


class Clock:
    def __init__(self):
        self.now = 10.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TransportSafetyTests(unittest.TestCase):
    def test_long_retry_after_blocks_later_independent_request(self):
        clock = Clock()
        client = Http("https://api.trakt.tv", headers={"Authorization": "Bearer fixture"})
        with patch("syncer.http.time.monotonic", side_effect=clock.monotonic), \
                patch("syncer.http.time.sleep", side_effect=clock.sleep), \
                patch("syncer.http.urlopen", side_effect=[rejected(429, {"Retry-After": "600"}), Response()]) as send:
            with self.assertRaises(ApiError):
                client.request("POST", ITEMS, body={})
            with self.assertRaises(ApiError):
                client.request("POST", "/users/owner/lists/2/items", body={})
            self.assertEqual(send.call_count, 1)
            clock.now += 600
            client.request("GET", "/users/settings")
            self.assertEqual(send.call_count, 2)

    def test_final_429_preserves_quiet_period_for_later_requests(self):
        clock = Clock()
        client = Http("https://example.test")
        with patch("syncer.http.time.monotonic", side_effect=clock.monotonic), \
                patch("syncer.http.time.sleep", side_effect=clock.sleep), \
                patch("syncer.http.urlopen", side_effect=[rejected(429, {"Retry-After": "2"}) for _ in range(3)]) as send:
            with self.assertRaises(ApiError):
                client.request("GET", "/lists")
            with self.assertRaises(ApiError):
                client.request("GET", "/other")
            self.assertEqual(send.call_count, 3)

    def setUp(self):
        # Block both common socket paths even if a transport patch is missed.
        for target in ("socket.create_connection", "socket.socket.connect"):
            blocker = patch(target, side_effect=AssertionError("Network forbidden in transport tests"))
            self.addCleanup(blocker.stop)
            self.addCleanup(blocker.start().assert_not_called)

    def assert_unknown(self, error):
        self.assertEqual(getattr(error, "outcome", None), "unknown")
        self.assertIn("unknown", str(error).lower())
        self.assertIn("reconcile", str(error).lower())
        self.assertIn("retry", str(error).lower())
        self.assertNotIn(SECRET, str(error))
        self.assertNotIn(SECRET, "".join(traceback.format_exception(error)))

    def test_committed_post_response_loss_sends_exactly_once(self):
        for phase in ("open", "read"):
            committed = []

            def send(request, **kwargs):
                committed.append(request.get_method())
                if phase == "open":
                    raise socket.timeout(SECRET)
                return Response(read_failure=IncompleteRead(SECRET.encode()))

            with self.subTest(phase=phase), patch("syncer.http.urlopen", side_effect=send), patch("syncer.http.time.sleep") as sleep:
                with self.assertRaises(ApiError) as caught:
                    Http("https://example.test").request("POST", ITEMS, body={"movies": []})
                self.assertEqual(committed, ["POST"])
                self.assert_unknown(caught.exception)
                sleep.assert_not_called()

    def test_mutations_never_replay_network_read_or_invalid_json(self):
        for method, path in (("POST", ITEMS), ("PUT", "/users/owner/lists/1"),
                             ("DELETE", "/users/owner/lists/1"), ("POST", "/oauth/token")):
            for failure in ("network", "timeout", "read", "json", "unicode"):
                def send(*args, **kwargs):
                    if failure == "network":
                        raise URLError(SECRET)
                    if failure == "timeout":
                        raise socket.timeout(SECRET)
                    if failure == "read":
                        return Response(read_failure=IncompleteRead(SECRET.encode()))
                    return Response(SECRET.encode() if failure == "json" else b'\xff')

                with self.subTest(method=method, failure=failure), patch("syncer.http.urlopen", side_effect=send) as transport, patch("syncer.http.time.sleep") as sleep:
                    with self.assertRaises(ApiError) as caught:
                        Http("https://example.test", params={"apikey": SECRET}).request(method, path, body={})
                    self.assertEqual(transport.call_count, 1)
                    self.assert_unknown(caught.exception)
                    sleep.assert_not_called()

    def test_mutation_5xx_is_unknown_not_automatically_replayed(self):
        for method, path in (("POST", ITEMS), ("PUT", "/lists/1"), ("DELETE", "/lists/1")):
            for status in (500, 503, 599):
                with self.subTest(method=method, status=status), patch("syncer.http.urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(rejected(status))) as send, patch("syncer.http.time.sleep") as sleep:
                    with self.assertRaises(ApiError) as caught:
                        Http("https://example.test").request(method, path, body={})
                    self.assertEqual(send.call_count, 1)
                    self.assertEqual(caught.exception.status, status)
                    self.assert_unknown(caught.exception)
                    sleep.assert_not_called()

    def test_get_network_read_and_5xx_retry_three_attempts_only(self):
        for kind in ("network", "read", "503", "429"):
            def send(*args, **kwargs):
                if kind == "network":
                    raise URLError(SECRET)
                if kind == "read":
                    return Response(read_failure=IncompleteRead(SECRET.encode()))
                raise rejected(int(kind), {"Retry-After": "0"})

            with self.subTest(kind=kind), patch("syncer.http.urlopen", side_effect=send) as transport, patch("syncer.http.time.sleep") as sleep:
                with self.assertRaises(ApiError) as caught:
                    Http("https://example.test").request("GET", "/list")
                self.assertEqual(transport.call_count, 3)
                self.assertEqual(sleep.call_count, 2)
                self.assertNotEqual(getattr(caught.exception, "outcome", None), "unknown")
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertTrue(all(0 <= call.args[0] <= 120 for call in sleep.call_args_list))

    def test_explicit_429_retries_only_known_list_operations(self):
        cases = (("POST", ITEMS), ("POST", ITEMS + "/remove"),
                 ("POST", ITEMS + "/reorder"), ("POST", "/lists/1/items/add"),
                 ("POST", "/lists/1/items/remove"), ("POST", "/lists/1/items/reorder"),
                 ("PUT", "/lists/1"), ("PUT", "/users/owner/lists/1"))
        for method, path in cases:
            failures = [rejected(429, {"Retry-After": "2"}),
                        rejected(429, {"Retry-After": "3"}), Response(b'{"ok":true}')]
            with self.subTest(method=method, path=path), patch("syncer.http.urlopen", side_effect=failures) as send, patch("syncer.http.time.sleep") as sleep:
                result, _ = Http("https://example.test").request(method, path, body={})
                self.assertEqual(result, {"ok": True})
                self.assertEqual(send.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 3])

    def test_429_exhaustion_is_known_rejection_and_stops_at_three(self):
        with patch("syncer.http.urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(rejected(429, {"Retry-After": "0"}))) as send, patch("syncer.http.time.sleep") as sleep:
            with self.assertRaises(ApiError) as caught:
                Http("https://example.test").request("POST", ITEMS, body={})
            self.assertEqual(send.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(caught.exception.status, 429)
            self.assertEqual(getattr(caught.exception, "outcome", None), "rejected")

    def test_unknown_routes_oauth_and_create_never_replay_even_429(self):
        cases = (("POST", "/oauth/token"), ("POST", "/oauth/device/token"),
                 ("POST", "/users/owner/lists"), ("POST", "/lists"),
                 ("POST", "/unknown"), ("PUT", "/unknown"), ("DELETE", "/lists/1"))
        for method, path in cases:
            with self.subTest(method=method, path=path), patch("syncer.http.urlopen", side_effect=rejected(429)) as send, patch("syncer.http.time.sleep") as sleep:
                with self.assertRaises(ApiError) as caught:
                    Http("https://example.test").request(method, path, body={})
                self.assertEqual(send.call_count, 1)
                self.assertEqual(getattr(caught.exception, "outcome", None), "rejected")
                sleep.assert_not_called()

    def test_terminal_4xx_safe_known_rejection(self):
        for method in ("GET", "POST", "PUT", "DELETE"):
            for status in (400, 401, 403, 404, 409, 422):
                with self.subTest(method=method, status=status), patch("syncer.http.urlopen", side_effect=rejected(status)) as send, patch("syncer.http.time.sleep") as sleep:
                    with self.assertRaises(ApiError) as caught:
                        Http("https://example.test").request(method, ITEMS, body={})
                    self.assertEqual(send.call_count, 1)
                    self.assertEqual(caught.exception.status, status)
                    self.assertEqual(getattr(caught.exception, "outcome", None), "rejected")
                    self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))
                    sleep.assert_not_called()

    def test_429_after_retry_then_ambiguous_failure_stops(self):
        failures = [rejected(429, {"Retry-After": "1"}), URLError(SECRET), Response()]
        with patch("syncer.http.urlopen", side_effect=failures) as send, patch("syncer.http.time.sleep") as sleep:
            with self.assertRaises(ApiError) as caught:
                Http("https://example.test").request("POST", ITEMS, body={})
            self.assertEqual(send.call_count, 2)
            self.assert_unknown(caught.exception)
            sleep.assert_called_once_with(1)

    def test_long_retry_after_aborts_without_early_retry(self):
        for value in ("121", "999999", "Infinity", format_datetime(datetime.now(timezone.utc) + timedelta(minutes=10))):
            with self.subTest(value=value), patch("syncer.http.urlopen", side_effect=rejected(429, {"Retry-After": value})) as send, patch("syncer.http.time.sleep") as sleep:
                with self.assertRaises(ApiError) as caught:
                    Http("https://example.test").request("POST", ITEMS, body={})
                self.assertEqual(send.call_count, 1)
                self.assertNotIn(SECRET, str(caught.exception))
                sleep.assert_not_called()

    def test_missing_or_bad_retry_after_uses_bounded_jitter(self):
        for value in (None, "invalid", "NaN", "-Infinity"):
            headers = {} if value is None else {"Retry-After": value}
            failures = [rejected(429, headers), rejected(429, headers), Response()]
            with self.subTest(value=value), patch("syncer.http.urlopen", side_effect=failures), patch("syncer.http.time.sleep") as sleep, patch("syncer.http.random.uniform", side_effect=[1.25, 2.5]) as jitter:
                Http("https://example.test").request("POST", ITEMS, body={})
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.25, 2.5])
                self.assertEqual([call.args for call in jitter.call_args_list], [(1, 2), (2, 4)])

    def test_shared_authenticated_trakt_paces_all_mutations_not_get(self):
        clock = Clock()
        starts = []

        def send(request, **kwargs):
            starts.append((request.get_method(), clock.now))
            self.assertEqual(kwargs["timeout"], 30)
            self.assertEqual(request.get_header("User-agent"), "Mozilla/5.0")
            return Response()

        client = Http("https://api.trakt.tv", {"aUtHoRiZaTiOn": "Bearer fixture"})
        with patch("syncer.http.urlopen", side_effect=send), patch("syncer.http.time.monotonic", side_effect=clock.monotonic), patch("syncer.http.time.sleep", side_effect=clock.sleep), patch("syncer.http.time.time", side_effect=AssertionError("Use monotonic pacing")):
            client.request("POST", ITEMS, body={})
            client.request("GET", "/users/owner/lists/1")
            client.request("PUT", "/users/owner/lists/other", body={})
            client.request("DELETE", "/users/owner/lists/third")
            client.request("GET", ITEMS)
        self.assertEqual(starts, [("POST", 10), ("GET", 10), ("PUT", 11), ("DELETE", 12), ("GET", 12)])
        self.assertEqual(clock.sleeps, [1, 1])

    def test_all_429_send_attempts_are_paced_without_double_wait(self):
        for retry_after in ("0", "3"):
            clock = Clock()
            starts = []

            def send(request, **kwargs):
                starts.append(clock.now)
                if len(starts) < 3:
                    raise rejected(429, {"Retry-After": retry_after})
                return Response()

            with self.subTest(retry_after=retry_after), patch("syncer.http.urlopen", side_effect=send), patch("syncer.http.time.monotonic", side_effect=clock.monotonic), patch("syncer.http.time.sleep", side_effect=clock.sleep):
                Http("https://api.trakt.tv", {"Authorization": "Bearer fixture"}).request("POST", ITEMS, body={})
            self.assertEqual(starts, [10, 10 + max(1, int(retry_after)), 10 + 2 * max(1, int(retry_after))])

    def test_failed_attempt_reserves_pacing_but_other_clients_are_independent(self):
        clock = Clock()
        starts = []

        def send(request, **kwargs):
            starts.append(clock.now)
            if len(starts) == 1:
                raise URLError(SECRET)
            return Response()

        headers = {"Authorization": "Bearer fixture"}
        client = Http("https://api.trakt.tv", headers)
        with patch("syncer.http.urlopen", side_effect=send), patch("syncer.http.time.monotonic", side_effect=clock.monotonic), patch("syncer.http.time.sleep", side_effect=clock.sleep):
            with self.assertRaises(ApiError):
                client.request("POST", ITEMS, body={})
            Http("https://api.trakt.tv", headers).request("POST", ITEMS, body={})
            client.request("PUT", "/users/owner/lists/other", body={})
        self.assertEqual(starts, [10, 10, 11])

    def test_pacing_only_authenticated_exact_trakt_origin(self):
        for base, headers in (("https://example.test", {"Authorization": "Bearer fixture"}),
                              ("https://api.trakt.tv", {}),
                              ("https://api.trakt.tv.evil.test", {"Authorization": "Bearer fixture"}),
                              ("https://api.trakt.tv:444", {"Authorization": "Bearer fixture"})):
            with self.subTest(base=base), patch("syncer.http.urlopen", side_effect=lambda *a, **k: Response()), patch("syncer.http.time.sleep") as sleep:
                client = Http(base, headers)
                client.request("POST", ITEMS, body={})
                client.request("PUT", "/users/owner/lists/1", body={})
                client.request("DELETE", "/users/owner/lists/1")
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()