"""Adversarial, offline contracts for frozen-intent recovery and journal parsing."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from syncer.config import Rule
from syncer.model import Item, Snapshot, SyncError
from syncer.planner import plan
from syncer.staged import decode, encode, rebase, records, validate_journals

URLS = ["https://mdblist.com/lists/owner/source", "https://trakt.tv/users/owner/lists/target"]
DATE = "2026-11-06T17:00:00Z"


def media(*numbers):
    return [Item("movie", {"tmdb": number}) for number in numbers]


def pair(left, right):
    return [Snapshot(url, "Name", "Description", deepcopy(rows))
            for url, rows in zip(URLS, (left, right))]


def ids(rows):
    return [(item.media_type, str(item.ids["tmdb"])) for item in rows]


class StagedRebaseTests(unittest.TestCase):
    def setUp(self):
        self.rule = Rule("rebase", *URLS, "bidirectional", "mirror", max_delete_fraction=1)

    def journal(self, desired, expected):
        return {"version": 1, "fingerprint": self.rule.fingerprint, "urls": list(URLS),
                "desired": [records(rows) for rows in desired],
                "expected": [encode(snapshot) for snapshot in expected],
                "operations": [], "created_at": DATE}

    def check_targets(self, result, *numbers):
        wanted = ids(media(*numbers))
        self.assertEqual(ids(result.source), wanted)
        self.assertEqual(ids(result.destination), wanted)
        self.assertEqual(result.baseline["source"], records(result.source))
        self.assertEqual(result.baseline["destination"], records(result.destination))

    def test_partial_own_add_or_remove_does_not_reinterpret_frozen_desired(self):
        baseline = {"fingerprint": self.rule.fingerprint,
                    "source": records(media(1, 2)), "destination": records(media(1, 2))}
        initial = pair(media(1, 3), media(1, 2))
        frozen = plan(self.rule, *initial, baseline)
        for right in (media(1, 2, 3), media(1)):
            expected = pair(media(1, 3), right)
            journal = self.journal((frozen.source, frozen.destination), initial)
            before = deepcopy((baseline, journal, expected))
            self.check_targets(rebase(self.rule, journal, expected, deepcopy(expected)), 1, 3)
            self.assertEqual((baseline, journal, expected), before)

    def test_external_delete_of_own_added_item_propagates_without_resurrecting_planned_delete(self):
        initial = pair(media(1, 3), media(1, 2))
        journal = self.journal((media(1, 3), media(1, 3)), initial)
        expected = pair(media(1, 3), media(1, 2, 3))  # Own add landed; remove 2 not sent.
        current = pair(media(1, 3, 4), media(1, 2))  # External add 4 and delete 3.
        before = deepcopy((journal, expected, current))
        self.check_targets(rebase(self.rule, journal, expected, current), 1, 4)
        self.assertEqual((journal, expected, current), before)

    def test_addition_wins_simultaneous_external_removal_existing_policy(self):
        expected = pair(media(1, 2), media(1))
        journal = self.journal((media(1), media(1)), expected)
        self.check_targets(rebase(self.rule, journal, expected, pair(media(1), media(1, 2))), 1, 2)

    def test_external_delta_after_own_remove_keeps_unsent_add_and_does_not_restore_removed_item(self):
        initial = pair(media(1, 3), media(1, 2))
        journal = self.journal((media(1, 3), media(1, 3)), initial)
        expected = pair(media(1, 3), media(1))  # Own removal 2 landed; add 3 still unsent.
        current = pair(media(1, 3, 4), [])  # External delete 1 and add 4.
        self.check_targets(rebase(self.rule, journal, expected, current), 3, 4)

    def test_additions_only_keeps_frozen_intent_and_current_extras(self):
        expected = pair(media(1, 3), media(1, 2))
        journal = self.journal((media(1, 3), media(1, 3)), expected)
        result = rebase(replace(self.rule, deletion_policy="additions_only"), journal,
                        expected, pair(media(1), media(1, 2, 4)))
        self.check_targets(result, 1, 3, 2, 4)

    def test_partial_batch_uses_acknowledged_prefix_not_whole_desired_as_expected(self):
        all_items = media(*range(1, 103))
        initial = pair(all_items, media(1))
        journal = self.journal((all_items, all_items), initial)
        expected = pair(all_items, media(*range(1, 102)))
        current = pair(all_items, media(*range(1, 101)))  # Delete own-added 101, not unsent 102.
        self.check_targets(rebase(self.rule, journal, expected, current), *range(1, 101), 102)

    def test_alias_bridge_is_typed_and_target_local_entry_metadata_survives(self):
        bridge = Item("movie", {"tmdb": 1, "imdb": "tt0000001"})
        expected = pair(media(1), [bridge])
        current = pair([Item("movie", {"imdb": "tt0000001"}, DATE, 7, 41),
                        Item("show", {"tmdb": 1})], [Item("movie", {"tmdb": 1}, DATE, 9, 81)])
        journal = self.journal((media(1), media(1)), expected)
        result = rebase(self.rule, journal, expected, current)
        self.assertEqual(ids(result.source), [("movie", "1"), ("show", "1")])
        self.assertEqual(ids(result.destination), ids(result.source))
        self.assertEqual(result.source[0].ids, {"imdb": "tt0000001", "tmdb": "1"})
        self.assertEqual((result.source[0].entry_id, result.source[0].rank), (41, 7))
        self.assertEqual((result.destination[0].entry_id, result.destination[0].rank), (81, 9))
        self.assertIsNone(result.destination[1].entry_id)

    def test_destination_order_authority_preserves_external_reorder(self):
        expected = pair(media(1, 2, 3), media(1, 2, 3))
        journal = self.journal((media(1, 2, 3), media(1, 2, 3)), expected)
        result = rebase(replace(self.rule, order_authority="destination"), journal,
                        expected, pair(media(1, 2, 3), media(3, 1, 2)))
        self.check_targets(result, 3, 1, 2)
        self.assertEqual((result.writes_source, result.writes_destination), (True, True))

    def test_directional_and_disabled_write_flags_match_planner(self):
        current = pair(media(1), media(2))
        journal = self.journal((media(1), media(1)), current)
        for direction, enabled, flags in [("source_to_destination", True, (False, True)),
                                          ("destination_to_source", True, (True, False)),
                                          ("bidirectional", False, (False, False))]:
            with self.subTest(direction=direction, enabled=enabled):
                rule = replace(self.rule, direction=direction, enabled=enabled)
                result = rebase(rule, journal, current, current)
                self.assertEqual((result.writes_source, result.writes_destination), flags)
                if not enabled:
                    self.assertEqual((ids(result.source), ids(result.destination)),
                                     (ids(current[0].items), ids(current[1].items)))

    def test_guards_count_actual_removals_even_when_replacements_hide_net_size_change(self):
        current = pair(media(1, 2, 3), media(1, 4, 5))
        journal = self.journal((media(1, 4, 5), media(1, 4, 5)), current)
        for limits in ({"max_delete_count": 1}, {"max_delete_fraction": 0.5}):
            with self.subTest(limits=limits), self.assertRaises(SyncError):
                rebase(replace(self.rule, **limits), journal, current, current)

    def test_empty_guard_and_allow_empty_does_not_bypass_delete_limits(self):
        expected = pair(media(1), media(1))
        journal = self.journal((media(1), media(1)), expected)
        current = pair([], media(1))
        for limits in ({}, {"allow_empty": True, "max_delete_count": 0}):
            with self.subTest(limits=limits), self.assertRaises(SyncError):
                rebase(replace(self.rule, **limits), journal, expected, current)

    def valid_state(self):
        snapshots = pair(media(1), media(1))
        for snapshot in snapshots:
            snapshot.items[0].rank = 1
        journal = self.journal((media(1, 2), media(1, 2)), snapshots)
        after = encode(pair(media(1), media(1, 2))[1])
        for rank, row in enumerate(after["items"], 1):
            row["rank"] = rank
        journal["operations"] = [{"side": 1, "action": "add", "outcome": "acknowledged",
                                  "items": records(media(2)), "entries": [], "fields": {}, "after": after}]
        return {"version": 2, "journals": {self.rule.id: journal}}

    def test_valid_snapshot_and_journal_roundtrip_are_independent_copies(self):
        snapshot = pair([Item("movie", {"tmdb": 1}, DATE, 1, 7)], [])[0]
        snapshot.metadata = {"id": 12, "description": "Description", "dynamic": False}
        value = encode(snapshot)
        self.assertEqual(encode(decode(value)), value)
        identities = records(snapshot.items)
        identities[0]["ids"]["tmdb"] = 99
        value["items"][0]["ids"]["tmdb"] = 99
        value["metadata"]["id"] = 99
        self.assertEqual((snapshot.items[0].ids["tmdb"], snapshot.metadata["id"]), (1, 12))
        validate_journals(self.valid_state())

    def assert_bad_fields(self, base, cases, parser):
        for path, value in cases:
            with self.subTest(path=path, value=value):
                changed = deepcopy(base)
                target = changed
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                with self.assertRaises(SyncError):
                    parser(changed)

    def test_malformed_snapshot_known_fields_metadata_ids_and_dates_fail_closed(self):
        base = encode(pair(media(1), [])[0])
        cases = [((key,), value) for key, value in
                 [("url", URLS[0] + "?token=secret"), ("name", []), ("description", None),
                  ("version", False), ("metadata", []), ("items", {})]]
        cases += [(("items", 0, key), value) for key, value in
                  [("media_type", []), ("ids", []), ("ids", {"tmdb": True}),
                   ("ids", {"tmdb": 1, "extra": []}), ("rank", True), ("rank", -1),
                   ("entry_id", True), ("entry_id", 0), ("added_at", "not-a-date"),
                   ("added_at", "2026-11-06T17:00:00"), ("added_at", 12)]]
        cases += [(("metadata",), value) for value in
                  [{"id": True}, {"id": 0}, {"ids": []}, {"ids": {"trakt": True}},
                   {"item_count": False}, {"items": -1}, {"updated_at": "not-a-date"}]]
        self.assert_bad_fields(base, cases, decode)

    def test_malformed_journal_types_fingerprint_dates_and_endpoint_mismatch(self):
        state = self.valid_state()
        prefix = ("journals", self.rule.id)
        cases = [(("version",), True), (("version",), 2.0), (("journals",), [])]
        cases += [(prefix + (key,), value) for key, value in
                  [("version", True), ("fingerprint", "g" * 64), ("created_at", "yesterday"),
                   ("created_at", "2026-11-06T17:00:00"), ("urls", [URLS[0], URLS[0]]),
                   ("desired", {}), ("expected", []), ("operations", {})]]
        cases += [(prefix + ("expected", 1, "url"), URLS[0])]
        cases += [(prefix + ("desired", 0, 0, "ids"), {"tmdb": []})]
        self.assert_bad_fields(state, cases, validate_journals)

    def test_malformed_operation_action_payload_types_and_endpoint_mismatch(self):
        prefix = ("journals", self.rule.id, "operations", 0)
        cases = [(prefix + (key,), value) for key, value in
                 [("side", True), ("side", 2), ("action", []), ("outcome", {}),
                  ("entries", [True]), ("entries", [1, 1]), ("fields", {"description": 1}),
                  ("fields", {"other": "value"}), ("items", []), ("entries", [11]),
                  ("fields", {"description": "not a membership field"}), ("action", "metadata")]]
        cases += [(prefix + ("after", "url"), URLS[0])]
        self.assert_bad_fields(self.valid_state(), cases, validate_journals)

    def test_operation_after_must_match_its_action_projection(self):
        prefix = ("journals", self.rule.id, "operations", 0)
        state = self.valid_state()
        before = state["journals"][self.rule.id]["expected"][1]["items"]
        cases = [(prefix + ("after", "items", 1, "ids"), {"tmdb": 3}),
                 (prefix + ("after", "items"), before), (prefix + ("action",), "remove")]
        self.assert_bad_fields(state, cases, validate_journals)

    def test_reorder_entry_mapping_rank_projection_and_duplicate_entries(self):
        state = self.valid_state()
        journal = state["journals"][self.rule.id]
        op = journal["operations"][0]
        journal["expected"][1] = deepcopy(op["after"])
        for row in journal["expected"][1]["items"]:
            row["entry_id"] = row["rank"] + 100
        op.update(action="reorder", items=[], entries=[102, 101], after=deepcopy(journal["expected"][1]))
        op["after"]["items"].reverse()
        for rank, row in enumerate(op["after"]["items"], 1):
            row["rank"] = rank
        validate_journals(state)
        prefix = ("journals", self.rule.id, "operations", 0)
        cases = [(prefix + ("entries",), [101, 101]), (prefix + ("entries",), [101, 102]),
                 (prefix + ("after", "items", 1, "entry_id"), 102),
                 (prefix + ("after", "items", 1, "rank"), 1)]
        self.assert_bad_fields(state, cases, validate_journals)


if __name__ == "__main__":
    unittest.main()