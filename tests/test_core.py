"""Offline contracts for strict configuration and pure reconciliation (T003/T011)."""

from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.config import Rule, load_config, parse_url
from syncer.model import Item, Snapshot, SyncError, identify
from syncer.planner import Plan, plan, validate_deletions


SOURCE = "https://mdblist.com/lists/alice/releases"
DESTINATION = "https://trakt.tv/users/bob/lists/releases"


def movie(number, **kwargs):
    return Item("movie", {"tmdb": number}, **kwargs)


def snapshot(url, items):
    return Snapshot(url, "Title", "Description", items)


def rule(**changes):
    values = dict(
        id="releases", source=SOURCE, destination=DESTINATION,
        direction="source_to_destination", deletion_policy="mirror",
    )
    values.update(changes)
    return Rule(**values)


def baseline_for(config, source, destination=None):
    def records(items):
        return [{"media_type": item.media_type, "ids": dict(item.ids)} for item in items]

    return {
        "fingerprint": config.fingerprint,
        "source": records(source),
        "destination": records(source if destination is None else destination),
    }


def numbers(items):
    return [str(item.ids["tmdb"]) for item in items]


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def document(self, **changes):
        values = asdict(rule())
        values.update(changes)
        return "version = 1\n[[rules]]\n" + "\n".join(
            f"{key} = {json.dumps(value)}" for key, value in values.items()
        )

    def load_rules(self, *changes):
        return self.load("version = 1\n" + "\n".join(
            self.document(**entry).removeprefix("version = 1\n") for entry in changes
        ))

    def test_rule_dataclass_defaults_and_valid_toml(self):
        text = f'''version = 1
[[rules]]
id = "releases"
source = "{SOURCE}"
destination = "{DESTINATION}"
direction = "source_to_destination"
deletion_policy = "mirror"
'''
        loaded = self.load(text)
        self.assertEqual(loaded, [rule()])
        self.assertTrue(is_dataclass(Rule))
        self.assertEqual(loaded[0].order_authority, "source")
        self.assertIs(loaded[0].allow_empty, False)
        self.assertEqual(loaded[0].max_delete_fraction, 0.5)
        self.assertEqual(loaded[0].max_delete_count, 100)
        self.assertIs(loaded[0].enabled, True)

    def test_per_rule_policies_directions_and_overrides(self):
        first = self.document(allow_empty=True, max_delete_fraction=1, max_delete_count=0)
        second = self.document(
            id="reverse", source="https://mdblist.com/lists/carol/movies",
            destination="https://trakt.tv/users/dan/lists/movies",
            direction="destination_to_source", deletion_policy="additions_only",
            order_authority="destination", enabled=False,
        ).removeprefix("version = 1\n")
        loaded = self.load(first + "\n" + second)
        self.assertEqual([item.deletion_policy for item in loaded], ["mirror", "additions_only"])
        self.assertEqual(loaded[1].direction, "destination_to_source")
        self.assertFalse(loaded[1].enabled)
        self.assertTrue(loaded[0].allow_empty)

    def test_supported_urls(self):
        self.assertEqual(parse_url(SOURCE), ("mdblist", "alice", "releases"))
        self.assertEqual(parse_url(DESTINATION), ("trakt", "bob", "releases"))
        self.assertEqual(parse_url(SOURCE + "/"), ("mdblist", "alice", "releases"))
        self.assertEqual(
            parse_url("https://trakt.tv/users/Bob/lists/Releases/"),
            ("trakt", "bob", "releases"),
        )
        self.assertEqual(
            parse_url("https://trakt.tv/users/user-name_1/lists/list-2"),
            ("trakt", "user-name_1", "list-2"),
        )

    def test_unsupported_or_unsafe_urls(self):
        invalid = [
            "http://mdblist.com/lists/alice/releases",
            "https://www.mdblist.com/lists/alice/releases",
            "https://mdblist.com.evil.test/lists/alice/releases",
            "https://mdblist.com:443/lists/alice/releases",
            "https://mdblist.com:/lists/alice/releases",
            "https://user:secret@mdblist.com/lists/alice/releases",
            SOURCE + "?token=secret", SOURCE + "?", SOURCE + "#rank", SOURCE + "#",
            SOURCE + "/extra", SOURCE + "//", SOURCE + "\n", " " + SOURCE,
            "https://trakt.tv/users/bob/watchlist",
            "https://trakt.tv/users/bob/lists/",
            "https://trakt.tv/users/bob/lists/a%2Fb",
            "https://trakt.tv/users/../lists/releases",
            "https://mdblist.com/lists//releases", "https://example.org/lists/alice/releases",
            "https://mdblist.com\\@evil.test/lists/alice/releases", "", 5, None,
        ]
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(SyncError):
                parse_url(url)

    def test_strict_field_types_and_ranges(self):
        invalid = {
            "id": ["", "  ", 1, True],
            "source": [False, "https://example.org/list"],
            "destination": [5],
            "direction": ["forward", "both", "", False, 1],
            "deletion_policy": ["delete", "union", False],
            "order_authority": ["newest", True],
            "allow_empty": [0, 1, "false"],
            "enabled": [0, 1, "true"],
            "max_delete_fraction": [True, False, "0.5", -0.01, 1.01, float("inf"), float("nan")],
            "max_delete_count": [True, False, "100", 1.5, -1],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(SyncError):
                    rule(**{field: value})

    def test_toml_numeric_boolean_validation(self):
        for field, value in [
            ("enabled", 1), ("allow_empty", 0), ("max_delete_fraction", True),
            ("max_delete_count", False), ("max_delete_count", 1.0),
            ("direction", "reverse"), ("deletion_policy", "keep"),
        ]:
            with self.subTest(field=field), self.assertRaises(SyncError):
                self.load(self.document(**{field: value}))

    def test_unknown_missing_and_invalid_top_level_config(self):
        valid = self.document()
        invalid = [
            valid.replace("version = 1", "version = true"),
            valid.replace("version = 1", "version = 1.0"),
            valid.replace("version = 1", "version = 2"),
            valid.replace("version = 1\n", ""),
            valid.replace("version = 1", 'version = 1\ntypo = "unexpected"'),
            valid + '\nunknown = "unexpected"',
            valid.replace('id = "releases"\n', ""),
            valid.replace('deletion_policy = "mirror"\n', ""),
            "version = 1", "version = 1\nrules = []",
            "version = 1\nrules = false", "version = 1\n[rules]\nid = 'invalid'",
            "version = [",
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(SyncError):
                self.load(text)

    def test_missing_config_is_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(SyncError):
            load_config(Path(directory) / "missing.toml")

    def test_duplicate_rule_ids_and_same_endpoints_rejected(self):
        with self.assertRaises(SyncError):
            self.load(self.document() + "\n" + self.document().removeprefix("version = 1\n"))
        for destination in [SOURCE, SOURCE + "/", SOURCE.replace("alice/releases", "Alice/Releases")]:
            with self.subTest(destination=destination), self.assertRaises(SyncError):
                rule(destination=destination)

    def test_multiple_readers_allowed_but_active_writer_overlap_rejected(self):
        second = self.document(
            id="other", destination="https://trakt.tv/users/bob/lists/other",
        ).removeprefix("version = 1\n")
        self.assertEqual(len(self.load(self.document() + "\n" + second)), 2)
        overlapping = second.replace("/lists/other", "/lists/releases/")
        with self.assertRaises(SyncError):
            self.load(self.document() + "\n" + overlapping)
        case_variant = overlapping.replace("/bob/lists/releases/", "/Bob/lists/Releases/")
        with self.assertRaises(SyncError):
            self.load(self.document() + "\n" + case_variant)
        disabled = overlapping.replace("enabled = true", "enabled = false")
        self.assertEqual(len(self.load(self.document() + "\n" + disabled)), 2)

    def test_reciprocal_enabled_rules_rejected_as_cycle(self):
        for policy in ("mirror", "additions_only"):
            with self.subTest(policy=policy), self.assertRaisesRegex(SyncError, "cycle"):
                self.load_rules(
                    {"deletion_policy": policy},
                    {"id": "return", "source": DESTINATION,
                     "destination": SOURCE.replace("alice/releases", "Alice/Releases/")},
                )

    def test_reverse_direction_contributes_reversed_edge_to_cycle(self):
        with self.assertRaisesRegex(SyncError, "cycle"):
            self.load_rules(
                {}, {"id": "return", "direction": "destination_to_source"},
            )

    def test_three_rule_cycle_rejected_regardless_of_config_order(self):
        third = "https://mdblist.com/lists/carol/third"
        entries = [
            {},
            {"id": "second", "source": third, "destination": DESTINATION,
             "direction": "destination_to_source"},
            {"id": "third", "source": third, "destination": SOURCE},
        ]
        for ordered in (entries, entries[::-1]):
            with self.subTest(ordered=ordered), self.assertRaisesRegex(SyncError, "cycle"):
                self.load_rules(*ordered)

    def test_acyclic_rule_chain_valid_without_topological_input_order(self):
        third = "https://mdblist.com/lists/carol/third"
        fourth = "https://trakt.tv/users/dan/lists/fourth"
        loaded = self.load_rules(
            {"id": "third", "source": third, "destination": fourth},
            {},
            {"id": "second", "source": third, "destination": DESTINATION,
             "direction": "destination_to_source"},
        )
        self.assertEqual([entry.id for entry in loaded], ["third", "releases", "second"])

    def test_disabled_cycle_edge_ignored(self):
        for disabled_id in ("outbound", "return"):
            with self.subTest(disabled_id=disabled_id):
                loaded = self.load_rules(
                    {"id": "outbound", "enabled": disabled_id != "outbound"},
                    {"id": "return", "source": DESTINATION, "destination": SOURCE,
                     "enabled": disabled_id != "return"},
                )
                self.assertEqual(len(loaded), 2)

    def test_bidirectional_internal_pair_and_shared_readers_remain_valid(self):
        loaded = self.load_rules(
            {"direction": "bidirectional"},
            {"id": "source-reader", "destination": "https://trakt.tv/users/dan/lists/first"},
            {"id": "dest-reader", "source": DESTINATION,
             "destination": "https://mdblist.com/lists/carol/second"},
        )
        self.assertEqual(len(loaded), 3)

    def test_bidirectional_cross_rule_writer_overlap_still_rejected(self):
        with self.assertRaisesRegex(SyncError, "write the same endpoint"):
            self.load_rules(
                {"direction": "bidirectional"},
                {"id": "overlap", "source": "https://mdblist.com/lists/carol/third",
                 "destination": SOURCE},
            )

    def test_fingerprint_covers_every_rule_semantic(self):
        config = rule()
        self.assertIsInstance(config.fingerprint, str)
        self.assertIsNotNone(re.fullmatch("[0-9a-f]{64}", config.fingerprint))
        self.assertEqual(config.fingerprint, rule().fingerprint)
        changes = dict(
            id="other", source="https://mdblist.com/lists/alice/other",
            destination="https://trakt.tv/users/bob/lists/other", direction="bidirectional",
            deletion_policy="additions_only", order_authority="destination",
            allow_empty=True, max_delete_fraction=0.75, max_delete_count=50,
        )
        for field, value in changes.items():
            with self.subTest(field=field):
                self.assertNotEqual(config.fingerprint, rule(**{field: value}).fingerprint)
        self.assertEqual(config.fingerprint, rule(enabled=False).fingerprint)
        self.assertEqual(config.fingerprint, rule(profile="nightly").fingerprint)


class IdentityTests(unittest.TestCase):
    def test_shared_ids_join_without_title_or_media_type_guessing(self):
        sequences, representatives = identify(
            [Item("movie", {"tmdb": 1})],
            [Item("movie", {"tmdb": "01", "imdb": "tt0001"})],
            [Item("show", {"tmdb": 1})],
        )
        self.assertEqual(sequences[0], sequences[1])
        self.assertNotEqual(sequences[0], sequences[2])
        self.assertEqual(representatives[sequences[0][0]].ids, {"imdb": "tt0001", "tmdb": "1"})

    def test_conflicting_or_duplicate_ids_fail_closed(self):
        cases = [
            [[movie(1), movie("01")]],
            [[Item("movie", {"tmdb": 1, "imdb": "tt1"})],
             [Item("movie", {"tmdb": 2, "imdb": "tt1"})]],
            [[Item("movie", {})]], [[Item("episode", {"tmdb": 1})]],
        ]
        for collections in cases:
            with self.subTest(collections=collections), self.assertRaises(SyncError):
                identify(*collections)


class PlannerTests(unittest.TestCase):
    def reconcile(self, config, source, destination, baseline=None, bootstrap=False):
        return plan(config, snapshot(config.source, source), snapshot(config.destination, destination),
                    baseline, bootstrap=bootstrap)

    def test_one_way_mirror_preserves_input_sequence_not_rank_or_dates(self):
        source = [movie(3, rank=100, added_at="2025-03-01"),
                  movie(1, rank=1, added_at="2025-01-01"), movie(2)]
        result = self.reconcile(rule(), source, [movie(1), movie(4)])
        self.assertIsInstance(result, Plan)
        self.assertTrue(is_dataclass(Plan))
        self.assertEqual(numbers(result.source), ["3", "1", "2"])
        self.assertEqual(numbers(result.destination), ["3", "1", "2"])
        self.assertFalse(result.writes_source)
        self.assertTrue(result.writes_destination)

    def test_idempotent_with_different_provider_id_sets(self):
        source = [movie(1), movie(2)]
        destination = [Item("movie", {"imdb": "tt1", "tmdb": 1}), movie(2)]
        result = self.reconcile(rule(), source, destination)
        self.assertFalse(result.writes_source)
        self.assertTrue(result.writes_destination)
        self.assertEqual(numbers(result.source), numbers(result.destination))

    def test_order_only_change_requires_write(self):
        result = self.reconcile(rule(), [movie(2), movie(1)], [movie(1), movie(2)])
        self.assertTrue(result.writes_destination)
        self.assertFalse(result.writes_source)

    def test_additions_only_authority_first_then_destination_extras(self):
        config = rule(deletion_policy="additions_only")
        result = self.reconcile(config, [movie(2), movie(1)], [movie(9), movie(1), movie(8)])
        self.assertEqual(numbers(result.source), ["2", "1"])
        self.assertEqual(numbers(result.destination), ["2", "1", "9", "8"])
        self.assertEqual(len(result.baseline["source"]), 2)
        self.assertEqual(len(result.baseline["destination"]), 4)
        self.assertFalse(result.writes_source)
        repeated = self.reconcile(config, result.source, result.destination, result.baseline)
        self.assertEqual(numbers(repeated.destination), numbers(result.destination))

    def test_reverse_direction_uses_destination_as_authority(self):
        config = rule(direction="destination_to_source")
        result = self.reconcile(config, [movie(1), movie(4)], [movie(3), movie(1)])
        self.assertEqual(numbers(result.source), ["3", "1"])
        self.assertEqual(numbers(result.destination), ["3", "1"])
        self.assertTrue(result.writes_source)
        self.assertFalse(result.writes_destination)

    def test_reverse_additions_only_keeps_source_extras_after_authority(self):
        config = rule(direction="destination_to_source", deletion_policy="additions_only")
        result = self.reconcile(config, [movie(9), movie(1)], [movie(2), movie(1)])
        self.assertEqual(numbers(result.source), ["2", "1", "9"])
        self.assertEqual(numbers(result.destination), ["2", "1"])
        self.assertFalse(result.writes_destination)

    def test_mirror_requires_explicit_bootstrap_without_baseline(self):
        config = rule(direction="bidirectional")
        with self.assertRaises(SyncError):
            self.reconcile(config, [movie(1)], [movie(2)])
        result = self.reconcile(config, [movie(1)], [movie(2)], bootstrap=True)
        self.assertEqual(numbers(result.source), ["1", "2"])
        self.assertEqual(numbers(result.destination), ["1", "2"])
        self.assertTrue(result.writes_source)
        self.assertTrue(result.writes_destination)

    def test_bidirectional_deletes_without_resurrection_and_keeps_new_items(self):
        config = rule(direction="bidirectional")
        prior = baseline_for(config, [movie(1), movie(2), movie(3), movie(4)])
        result = self.reconcile(
            config, [movie(1), movie(3), movie(4), movie(5)],
            [movie(1), movie(2), movie(4), movie(6)], prior,
        )
        self.assertEqual(numbers(result.source), ["1", "4", "5", "6"])
        self.assertEqual(numbers(result.destination), ["1", "4", "5", "6"])
        repeated = self.reconcile(config, result.source, result.destination, result.baseline)
        self.assertEqual(numbers(repeated.source), numbers(result.source))
        self.assertEqual(numbers(repeated.destination), numbers(result.destination))

    def test_asymmetric_mirror_baseline_fails_closed_even_with_bootstrap(self):
        config = rule(direction="bidirectional")
        prior = baseline_for(config, [movie(1), movie(2)], [movie(2)])
        for bootstrap in [False, True]:
            with self.subTest(bootstrap=bootstrap), self.assertRaises(SyncError):
                self.reconcile(config, [movie(2)], [movie(2), movie(1)], prior, bootstrap)

    def test_mirror_baseline_compares_identity_sets_not_raw_aliases(self):
        config = rule(direction="bidirectional")
        prior = baseline_for(config, [movie(1)], [Item("movie", {"tmdb": 1, "imdb": "tt1"})])
        result = self.reconcile(config, [movie(1)], [Item("movie", {"imdb": "tt1"})], prior)
        self.assertEqual(numbers(result.source), ["1"])
        self.assertEqual(numbers(result.destination), ["1"])

    def test_additions_only_bidirectional_needs_no_baseline_and_ignores_deletions(self):
        config = rule(direction="bidirectional", deletion_policy="additions_only")
        for prior in [None, baseline_for(config, [movie(1), movie(2), movie(3)])]:
            with self.subTest(prior=prior):
                result = self.reconcile(config, [movie(1)], [movie(2)], prior)
                self.assertEqual(numbers(result.source), ["1", "2"])
                self.assertEqual(numbers(result.destination), ["1", "2"])

    def test_destination_order_authority_and_minority_timestamp_key_ties(self):
        config = rule(direction="bidirectional", order_authority="destination")
        source = [movie(8, added_at="2025-02-01"), movie(5, added_at="2025-01-01"),
                  movie(4, added_at="2025-01-01"), movie(1)]
        result = self.reconcile(config, source, [movie(3), movie(1)], bootstrap=True)
        self.assertEqual(numbers(result.source), ["3", "1", "4", "5", "8"])
        self.assertEqual(numbers(result.destination), numbers(result.source))

    def test_authority_sequence_preserved_and_undated_minority_order_is_deterministic(self):
        config = rule(direction="bidirectional")
        first = self.reconcile(config, [movie(3), movie(1)], [movie(9), movie(7)], bootstrap=True)
        second = self.reconcile(config, [movie(3), movie(1)], [movie(7), movie(9)], bootstrap=True)
        self.assertEqual(numbers(first.source), ["3", "1", "7", "9"])
        self.assertEqual(numbers(first.source), numbers(second.source))

    def test_baseline_ids_bridge_current_snapshots_before_identity_resolution(self):
        config = rule(direction="bidirectional")
        old = [Item("movie", {"imdb": "tt1", "tmdb": 1}), movie(2)]
        result = self.reconcile(
            config, [movie(1), movie(2)],
            [Item("movie", {"imdb": "tt1"}), movie(2)], baseline_for(config, old),
        )
        self.assertTrue(result.writes_source)
        self.assertTrue(result.writes_destination)
        self.assertEqual(len(result.source), 2)
        self.assertEqual(result.source[0].ids, {"imdb": "tt1", "tmdb": "1"})

    def test_new_alias_cannot_shift_baseline_key_and_resurrect_deleted_item(self):
        config = rule(direction="bidirectional")
        prior = baseline_for(config, [movie(1), movie(2)])
        result = self.reconcile(
            config, [movie(2)], [Item("movie", {"imdb": "tt1", "tmdb": 1}), movie(2)], prior,
        )
        self.assertEqual(numbers(result.source), ["2"])
        self.assertEqual(numbers(result.destination), ["2"])

    def test_changed_fingerprint_fails_closed_even_with_bootstrap(self):
        config = rule(direction="bidirectional")
        for field, value in [("source", "https://mdblist.com/lists/alice/other"),
                             ("max_delete_count", 5)]:
            prior = baseline_for(replace(config, **{field: value}), [movie(1)])
            for bootstrap in [False, True]:
                with self.subTest(field=field, bootstrap=bootstrap), self.assertRaises(SyncError):
                    self.reconcile(config, [movie(1)], [movie(1)], prior, bootstrap)

    def test_malformed_baselines_fail_closed(self):
        config = rule(direction="bidirectional")
        good = baseline_for(config, [movie(1)])
        invalid = [
            {}, [], {**good, "source": None}, {**good, "destination": {}},
            {**good, "source": [{"media_type": "movie"}]},
            {**good, "source": [{"media_type": "movie", "ids": {}}]},
            {**good, "source": [{"media_type": "episode", "ids": {"tmdb": 1}}]},
            {**good, "source": [{"media_type": "movie", "ids": "invalid"}]},
            {**good, "source": good["source"] * 2},
            {**good, "source": [{"media_type": "movie", "ids": {"tmdb": 2, "imdb": "tt1"}}],
             "destination": [{"media_type": "movie", "ids": {"tmdb": 1, "imdb": "tt1"}}]},
        ]
        for prior in invalid:
            with self.subTest(prior=prior), self.assertRaises(SyncError):
                self.reconcile(config, [movie(1)], [movie(1)], prior)

    def test_baseline_contains_only_fingerprint_and_intended_identity_sets(self):
        config = rule()
        first = movie(1, added_at="2025-01-01", rank=4, entry_id=10)
        first.ids["secret"] = "must-not-persist"
        result = self.reconcile(config, [first], [])
        self.assertEqual(result.baseline, {
            "fingerprint": config.fingerprint,
            "source": [{"media_type": "movie", "ids": {"tmdb": "1"}}],
            "destination": [{"media_type": "movie", "ids": {"tmdb": "1"}}],
        })
        self.assertNotIn("must-not-persist", json.dumps(result.baseline))
        self.assertEqual(json.loads(json.dumps(result.baseline)), result.baseline)

    def test_inputs_unchanged_and_target_entry_metadata_retained(self):
        config = rule()
        source = snapshot(SOURCE, [movie(2), movie(1, added_at="2025-01-01")])
        destination = snapshot(DESTINATION, [movie(1, entry_id=42), movie(2, entry_id=43)])
        prior = baseline_for(config, source.items)
        before = deepcopy((source, destination, prior))
        result = plan(config, source, destination, prior)
        self.assertEqual((source, destination, prior), before)
        self.assertEqual([item.entry_id for item in result.destination], [43, 42])
        result.destination[0].ids["tmdb"] = "999"
        self.assertEqual((source, destination, prior), before)

    def test_ambiguous_current_or_baseline_ids_refuse_plan(self):
        config = rule(direction="bidirectional")
        source = [Item("movie", {"tmdb": 2, "imdb": "tt1"})]
        destination = [Item("movie", {"tmdb": 1, "imdb": "tt1"})]
        with self.assertRaises(SyncError):
            self.reconcile(config, source, destination, bootstrap=True)
        with self.assertRaises(SyncError):
            self.reconcile(rule(), [movie(1), movie(1)], [])

    def test_movie_and_show_ids_are_distinct(self):
        result = self.reconcile(
            rule(), [movie(1), Item("show", {"tmdb": 1})], [],
        )
        self.assertEqual([item.media_type for item in result.destination], ["movie", "show"])

    def test_flags_mark_writable_sides_so_runner_can_check_remote_rank(self):
        for direction, flags in [
            ("source_to_destination", (False, True)),
            ("destination_to_source", (True, False)),
            ("bidirectional", (True, True)),
        ]:
            config = rule(direction=direction)
            result = self.reconcile(config, [movie(1)], [movie(1)], baseline_for(config, [movie(1)]))
            self.assertEqual((result.writes_source, result.writes_destination), flags)

    def test_plan_guards_only_written_targets_and_handles_reverse(self):
        with self.assertRaises(SyncError):
            self.reconcile(rule(), [], [movie(1)])
        with self.assertRaises(SyncError):
            self.reconcile(rule(direction="destination_to_source"), [movie(1)], [])
        result = self.reconcile(rule(), [movie(1)], [])
        self.assertEqual(numbers(result.destination), ["1"])
        with self.assertRaises(SyncError):
            self.reconcile(rule(), [movie(4)], [movie(1), movie(2), movie(3)])

    def test_bidirectional_guards_both_endpoints(self):
        config = rule(direction="bidirectional")
        prior = baseline_for(config, [movie(1), movie(2), movie(3)])
        for source, destination in [([movie(1)], [movie(1), movie(2), movie(3)]),
                                    ([movie(1), movie(2), movie(3)], [movie(1)])]:
            with self.subTest(source=source), self.assertRaises(SyncError):
                self.reconcile(config, source, destination, prior)


class DeletionGuardTests(unittest.TestCase):
    def test_no_deletions_and_exact_limits_are_allowed(self):
        validate_deletions(rule(max_delete_count=0, max_delete_fraction=0), [], [])
        validate_deletions(rule(max_delete_count=0, max_delete_fraction=0), [movie(1)], [movie(2), movie(1)])
        validate_deletions(rule(max_delete_count=1), [movie(1), movie(2)], [movie(1)])

    def test_count_and_fraction_use_removed_ids_not_net_size(self):
        current = [movie(1), movie(2), movie(3), movie(4)]
        desired = [movie(1), movie(5), movie(6), movie(7)]
        for config in [rule(max_delete_fraction=1, max_delete_count=2),
                       rule(max_delete_count=100, max_delete_fraction=0.5)]:
            with self.subTest(config=config), self.assertRaises(SyncError):
                validate_deletions(config, current, desired)

    def test_empty_requires_opt_in_and_other_limits_still_apply(self):
        with self.assertRaises(SyncError):
            validate_deletions(rule(max_delete_fraction=1), [movie(1)], [])
        with self.assertRaises(SyncError):
            validate_deletions(rule(allow_empty=True), [movie(1)], [])
        validate_deletions(rule(allow_empty=True, max_delete_fraction=1), [movie(1)], [])

    def test_guard_matches_aliases_before_counting_deletions(self):
        validate_deletions(
            rule(max_delete_count=0, max_delete_fraction=0), [movie(1)],
            [Item("movie", {"tmdb": 1, "imdb": "tt1"})],
        )


if __name__ == "__main__":
    unittest.main()
