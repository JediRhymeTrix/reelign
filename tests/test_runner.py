import base64
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.model import SyncError
from syncer.state import StateStore
from syncer.model import Item, Snapshot


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.enc"
        self.key = base64.urlsafe_b64encode(b"x" * 32).decode()

    def test_roundtrip_is_encrypted_and_randomized(self):
        store = StateStore(self.path, self.key)
        value = {"version": 1, "oauth": {"access_token": "super-secret"}, "rules": {}}
        store.save(value)
        first = self.path.read_bytes()
        self.assertNotIn(b"super-secret", first)
        self.assertEqual(store.load(), value)
        StateStore(self.path, self.key).save(value)
        self.assertNotEqual(first, self.path.read_bytes())

    def test_missing_requires_explicit_bootstrap(self):
        with self.assertRaises(SyncError):
            StateStore(self.path, self.key).load()

    def test_corruption_and_wrong_key_fail_closed(self):
        store = StateStore(self.path, self.key)
        store.save({"version": 1, "oauth": {}, "rules": {}})
        other = base64.urlsafe_b64encode(b"y" * 32).decode()
        with self.assertRaises(SyncError):
            StateStore(self.path, other).load()
        raw = self.path.read_bytes()
        self.path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
        with self.assertRaises(SyncError):
            store.load()

    def test_invalid_schema_and_key_rejected(self):
        with self.assertRaises(SyncError):
            StateStore(self.path, "bad-key")
        store = StateStore(self.path, self.key)
        store.save({"version": 9, "rules": {}})
        with self.assertRaises(SyncError):
            store.load()

    def test_unchanged_state_does_not_write(self):
        store = StateStore(self.path, self.key)
        value = {"version": 1, "oauth": {}, "rules": {}}
        store.save(value)
        old = self.path.read_bytes()
        store.load()
        self.assertFalse(store.save(value))
        self.assertEqual(self.path.read_bytes(), old)


class ExecutionTests(unittest.TestCase):
    def provider(self, items, writable=True):
        class Fake:
            def __init__(self):
                self.items = list(items)
                self.calls = []

            def snapshot(self, order="added"):
                return Snapshot("test", "Test", "", list(self.items), writable=writable)

            def preflight(self, snapshot):
                self.calls.append("preflight")
                if not snapshot.writable:
                    raise SyncError("Not writable")

            def ensure_rank_sort(self, snapshot):
                return False

            def add(self, additions):
                self.calls.append("add")
                self.items.extend(additions)

            def remove(self, removals):
                self.calls.append("remove")
                aliases = set().union(*(i.aliases() for i in removals))
                self.items = [i for i in self.items if not i.aliases() & aliases]

            def reorder(self, desired, snapshot):
                self.calls.append("reorder")
                self.items = list(desired)

        return Fake()

    def test_noop_makes_no_writes(self):
        from syncer.runner import execute_targets
        a = Item("movie", {"tmdb": 1})
        p = self.provider([a])
        execute_targets([(p, p.snapshot(), [a])], dry_run=False)
        self.assertEqual(p.calls, ["preflight"])

    def test_default_rank_sort_is_applied_even_with_unchanged_membership(self):
        from syncer.runner import execute_targets
        from unittest.mock import Mock
        a = Item("movie", {"tmdb": 1})
        p = self.provider([a])
        p.ensure_rank_sort = Mock(return_value=True)
        summary = execute_targets([(p, p.snapshot(), [a])])
        p.ensure_rank_sort.assert_called_once()
        self.assertTrue(summary[0]["default_sort_changed"])
        self.assertFalse(summary[0]["reorder"])

    def test_dry_run_never_updates_default_sort(self):
        from syncer.runner import execute_targets
        from unittest.mock import Mock
        p = self.provider([])
        p.ensure_rank_sort = Mock()
        execute_targets([(p, p.snapshot(), [])], dry_run=True)
        p.ensure_rank_sort.assert_not_called()

    def test_additions_follow_source_sequence_and_become_destination_ranks(self):
        from syncer.runner import execute_targets
        from unittest.mock import Mock
        old, middle, newest = [Item("movie", {"tmdb": n}) for n in [1, 2, 3]]
        p = self.provider([old])
        original_add = p.add
        p.add = Mock(wraps=original_add)
        desired = [newest, middle, old]
        execute_targets([(p, p.snapshot(), desired)])
        p.add.assert_called_once_with([newest, middle])
        self.assertEqual(p.items, desired)

    def test_dry_run_never_writes(self):
        from syncer.runner import execute_targets
        p = self.provider([])
        execute_targets([(p, p.snapshot(), [Item("movie", {"tmdb": 1})])], dry_run=True)
        self.assertEqual(p.items, [])
        self.assertFalse(any(c in p.calls for c in ["add", "remove", "reorder"]))

    def test_preflight_all_before_any_mutations(self):
        from syncer.runner import execute_targets
        a = Item("movie", {"tmdb": 1})
        left = self.provider([])
        right = self.provider([], writable=False)
        with self.assertRaises(SyncError):
            execute_targets([(left, left.snapshot(), [a]), (right, right.snapshot(), [a])])
        self.assertEqual(left.items, [])

    def test_failed_addition_verification_prevents_removals(self):
        from syncer.runner import execute_targets
        a, b = Item("movie", {"tmdb": 1}), Item("movie", {"tmdb": 2})
        p = self.provider([a])
        p.add = lambda additions: p.calls.append("add")
        with self.assertRaises(SyncError):
            execute_targets([(p, p.snapshot(), [b])])
        self.assertNotIn("remove", p.calls)

    def test_rank_only_change_is_applied_and_verified(self):
        from syncer.runner import execute_targets
        a, b = Item("movie", {"tmdb": 1}), Item("movie", {"tmdb": 2})
        p = self.provider([b, a])
        execute_targets([(p, p.snapshot(), [a, b])])
        self.assertEqual(p.calls, ["preflight", "reorder"])
        self.assertEqual(p.items, [a, b])

    def test_concurrent_noop_endpoint_change_prevents_checkpoint(self):
        from syncer.runner import execute_targets
        a = Item("movie", {"tmdb": 1})
        left, right = self.provider([a]), self.provider([])
        original_add = right.add

        def concurrent_add(items):
            original_add(items)
            left.items.clear()

        right.add = concurrent_add
        with self.assertRaises(SyncError):
            execute_targets([(left, left.snapshot(), [a]), (right, right.snapshot(), [a])])

    def test_readonly_authority_guard_called_before_deletion_and_checkpoint(self):
        from syncer.runner import execute_targets
        from unittest.mock import MagicMock
        a, b = Item("movie", {"tmdb": 1}), Item("movie", {"tmdb": 2})
        p = self.provider([a, b])
        guard = MagicMock(side_effect=SyncError("Authority changed"))
        with self.assertRaises(SyncError):
            execute_targets([(p, p.snapshot(), [a])], authority_guard=guard)
        self.assertNotIn("remove", p.calls)

    def test_final_snapshot_detects_edit_during_other_endpoint_removal(self):
        from syncer.runner import execute_targets
        a, b = Item("movie", {"tmdb": 1}), Item("movie", {"tmdb": 2})
        left, right = self.provider([a]), self.provider([a, b])
        original_remove = right.remove

        def concurrent_remove(items):
            original_remove(items)
            left.items.clear()

        right.remove = concurrent_remove
        with self.assertRaises(SyncError):
            execute_targets([(left, left.snapshot(), [a]), (right, right.snapshot(), [a])])


class OAuthTests(unittest.TestCase):
    def test_rotation_persisted_before_return(self):
        from syncer.runner import refresh_oauth
        from unittest.mock import MagicMock
        state = {"oauth": {"access_token": "old", "refresh_token": "r", "created_at": 1, "expires_in": 1}}
        store = MagicMock()
        new = {"access_token": "new", "refresh_token": "r2", "expires_in": 604800}
        with patch("syncer.http.Http") as http:
            http.return_value.request.return_value = (new, {})
            token = refresh_oauth(state, store, "client", "secret", False)
        self.assertEqual(token["access_token"], "new")
        store.save.assert_called_once_with(state)
        store.preflight.assert_called_once()
        self.assertEqual(state["oauth"]["refresh_token"], "r2")

    def test_dry_run_does_not_refresh_or_persist(self):
        from syncer.runner import refresh_oauth
        from unittest.mock import MagicMock
        state = {"oauth": {"created_at": 1, "expires_in": 1}}
        store = MagicMock()
        with patch("syncer.http.Http") as http:
            with self.assertRaises(SyncError):
                refresh_oauth(state, store, "client", "secret", True)
            http.assert_not_called()
        store.save.assert_not_called()

    def test_valid_token_needs_no_writes(self):
        from syncer.runner import refresh_oauth
        from unittest.mock import MagicMock
        import time
        state = {"oauth": {"created_at": int(time.time()), "expires_in": 604800}}
        store = MagicMock()
        self.assertEqual(refresh_oauth(state, store, "client", "secret", False), state["oauth"])
        store.save.assert_not_called()

    def test_persistence_failure_stops_after_rotation(self):
        from syncer.runner import refresh_oauth
        from unittest.mock import MagicMock
        state = {"oauth": {"refresh_token": "r", "created_at": 1, "expires_in": 1}}
        store = MagicMock()
        store.save.side_effect = SyncError("Persistence unavailable")
        new = {"access_token": "new", "refresh_token": "r2", "expires_in": 604800}
        with patch("syncer.http.Http") as http:
            http.return_value.request.return_value = (new, {})
            with self.assertRaises(SyncError):
                refresh_oauth(state, store, "client", "secret", False)
            http.return_value.request.assert_called_once()

    def test_storage_preflight_failure_does_not_consume_refresh_token(self):
        from syncer.runner import refresh_oauth
        from unittest.mock import MagicMock
        state = {"oauth": {"refresh_token": "r", "created_at": 1, "expires_in": 1}}
        store = MagicMock()
        store.preflight.side_effect = SyncError("Storage unavailable")
        with patch("syncer.http.Http") as http:
            with self.assertRaises(SyncError):
                refresh_oauth(state, store, "client", "secret", False)
            http.assert_not_called()
        store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()