"""Independent-review crash/recovery regressions at durable and provider boundaries."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from test_staged import StagedTests, SOURCE, TARGET
from syncer.model import Item, Snapshot, SyncError
from syncer.staged import _operation, _verified, resume, Deferred, Blocked


class StagedRegressions(StagedTests):
    def test_remove_ack_keeps_alias_bridge_after_expected_membership_empties(self):
        from syncer.staged import encode
        before = Snapshot(TARGET, "", "", [Item("movie", {"imdb": "tt1", "tmdb": 1}, entry_id=11)])
        op = _operation(1, "POST", "/users/owner/lists/films/items/remove",
                        {"movies": [{"ids": {"imdb": "tt1"}}]}, encode(before))
        op['outcome'] = 'acknowledged'
        stale = Snapshot(TARGET, "", "", [Item("movie", {"tmdb": 1}, entry_id=11)])
        self.assertFalse(_verified(op, stale))

    def test_unknown_alias_shrink_cannot_authorize_replay(self):
        from syncer.staged import encode
        before = Snapshot(TARGET, "", "", [Item("movie", {"imdb": "tt1", "tmdb": 1}, entry_id=11)])
        op = _operation(1, "POST", "/users/owner/lists/films/items/remove",
                        {"movies": [{"ids": {"imdb": "tt1"}}]}, encode(before))
        stale = Snapshot(TARGET, "", "", [Item("movie", {"tmdb": 1}, entry_id=11)])
        self.assertFalse(_verified(op, stale, [before.items]))

    def test_resume_uses_one_verified_observation_per_endpoint(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(Deferred):
            with session:
                self.providers[1].remove([snapshots[1].items[-1]])
                raise Deferred("wait")
        journal = self.store.load()['journals']['test']
        original = self.providers[1].snapshot
        with patch.object(self.providers[1], 'snapshot', wraps=original) as read:
            self.resume(self.rule, self.providers, journal)
        read.assert_called_once_with()

    def test_malformed_pending_snapshot_remains_terminal_not_healthy_pending(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(Deferred):
            with session:
                self.providers[1].remove([snapshots[1].items[-1]])
                raise Deferred('wait')
        journal = self.store.load()['journals']['test']
        with patch.object(self.providers[1], 'snapshot', side_effect=SyncError('invalid media')):
            with self.assertRaises(SyncError) as caught:
                self.resume(self.rule, self.providers, journal)
        self.assertNotIsInstance(caught.exception, Deferred)

    def test_expired_pending_is_action_required(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        journal = session.journal
        journal['created_at'] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with self.assertRaises(Blocked):
            self.resume(self.rule, self.providers, journal)

    def test_large_mdblist_reorder_rejects_before_first_chunk(self):
        from syncer.staged import encode
        entries = [Item('movie', {'tmdb': n}) for n in range(1, 1002)]
        before = Snapshot('https://mdblist.com/lists/owner/films', '', '', entries)
        body = {'position': 'top', 'items': [{'mediatype': i.media_type, 'ids': i.ids} for i in entries[:1000]]}
        with self.assertRaises(SyncError):
            _operation(0, 'POST', '/lists/12/items/reorder', body, encode(before))

    def test_unknown_inconsistent_observation_remains_action_required(self):
        from syncer.providers import _SnapshotChanged
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(SystemExit):
            with session, patch.object(self.apis[1], 'request', side_effect=SystemExit()):
                self.providers[1].remove([snapshots[1].items[-1]])
        journal = self.store.load()['journals']['test']
        with patch.object(self.providers[1], 'snapshot', side_effect=_SnapshotChanged('moving')):
            with self.assertRaises(Blocked):
                self.resume(self.rule, self.providers, journal)

    def test_partial_acknowledgement_is_durably_blocked_not_deferred(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        with self.assertRaises(SyncError):
            with session, patch.object(self.apis[1], 'request', return_value=({'deleted': {'movies': 0}}, {})):
                self.providers[1].remove([snapshots[1].items[-1]])
        journal = self.store.load()['journals']['test']
        self.assertEqual(journal['operations'][-1]['outcome'], 'invalid_response')
        with self.assertRaises(Blocked):
            self.resume(self.rule, self.providers, journal)

    def test_resume_guard_keeps_prior_unknown_reorder_predicate(self):
        snapshots, result = self.prepare()
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        op = _operation(1, 'POST', '/users/owner/lists/films/items/reorder',
                        {'rank': [1001, 1002, 1003]}, session.journal['expected'][1])
        session.journal['operations'].append(op)
        self.state.update(version=2, journals={'test': session.journal})
        next_session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        regressed = deepcopy(snapshots[1])
        for rank, item in enumerate(reversed(regressed.items), 1):
            item.rank = rank
        with self.assertRaises(Blocked):
            next_session.observe(self.providers[1], regressed)

    def test_confirmed_baseline_aliases_enrich_unknown_remove_payload(self):
        from syncer.staged import encode, records
        snapshots, result = self.prepare()
        bridge = Item('movie', {'imdb': 'tt1', 'tmdb': 1})
        snapshots[0].items = []
        result.source = []
        self.state['rules']['test'] = {'fingerprint': self.rule.fingerprint,
            'source': records([bridge]), 'destination': records([bridge])}
        snapshots[1].items = [Item('movie', {'imdb': 'tt1'}, entry_id=1001)]
        result.destination = []
        session = self.Session(self.rule, self.providers, snapshots, result, self.state, self.store)
        op = _operation(1, 'POST', '/users/owner/lists/films/items/remove',
                        {'movies': [{'ids': {'imdb': 'tt1'}}]}, session.journal['expected'][1])
        self.assertEqual(op['items'][0]['ids'], {'imdb': 'tt1', 'tmdb': 1})
        self.assertFalse(_verified(op, Snapshot(TARGET, '', '', [Item('movie', {'tmdb': 1})])))