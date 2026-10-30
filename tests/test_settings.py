"""Offline settings and legacy-baseline compatibility contracts (T003)."""

from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.config import Rule, Schedule, Settings, load_config, load_settings
from syncer.model import Item, Snapshot, SyncError
from syncer.planner import plan


SOURCE = "https://mdblist.com/lists/curator/releases"
DESTINATION = "https://trakt.tv/users/viewer/lists/releases"


def rule(**changes):
    values = dict(id="releases", source=SOURCE, destination=DESTINATION,
                  direction="source_to_destination", deletion_policy="mirror")
    values.update(changes)
    return Rule(**values)


def original_values(config):
    """Explicit pre-profile dataclass shape, independent of implementation."""
    return {
        "id": config.id, "source": config.source, "destination": config.destination,
        "direction": config.direction, "deletion_policy": config.deletion_policy,
        "order_authority": config.order_authority, "allow_empty": config.allow_empty,
        "max_delete_fraction": float(config.max_delete_fraction),
        "max_delete_count": config.max_delete_count, "enabled": config.enabled,
    }


def digest(values):
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def document(**changes):
    values = asdict(rule())
    values.update(changes)
    return "version = 1\n[[rules]]\n" + "\n".join(
        f"{key} = {json.dumps(value)}" for key, value in values.items()
    ) + "\n"


class SettingsTests(unittest.TestCase):
    def test_attribution_opt_out_does_not_reset_baseline_fingerprint(self):
        current = rule()
        disabled = rule(attribution=False)
        self.assertTrue(current.attribution)
        self.assertEqual(current.fingerprint, disabled.fingerprint)
        self.assertEqual(current.legacy_fingerprint, disabled.legacy_fingerprint)
        settings = self.load(document(attribution=False))
        self.assertFalse(settings.rules[0].attribution)
        with self.assertRaises(SyncError):
            rule(attribution="false")

    def load(self, text, loader=load_settings):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(text, encoding="utf-8")
            return loader(path)

    def test_dataclass_defaults_are_independent_and_legacy_config_loads(self):
        first, second = Settings(), Settings()
        self.assertEqual(first.rules, [])
        self.assertEqual(first.schedules, {"default": Schedule()})
        self.assertEqual(Schedule().cron, "17 * * * *")
        self.assertEqual(Schedule().timezone, "UTC")
        first.rules.append(rule())
        first.schedules["other"] = Schedule(cron="manual")
        self.assertEqual(second.rules, [])
        self.assertEqual(second.schedules, {"default": Schedule()})
        self.assertEqual(rule().profile, "default")
        legacy = document().replace('profile = "default"\n', "")
        settings = self.load(legacy)
        self.assertEqual(settings.rules, [rule()])
        self.assertEqual(settings.schedules, {"default": Schedule()})
        self.assertEqual(self.load(legacy, load_config), settings.rules)

    def test_named_schedules_default_override_and_manual(self):
        text = document(profile="nightly") + '''
[schedules.default]
cron = "manual"
[schedules.nightly]
cron = "30 2 * * *"
timezone = "UTC"
'''
        settings = self.load(text)
        self.assertEqual(settings.rules[0].profile, "nightly")
        self.assertIsNone(settings.schedules["default"].cron)
        self.assertEqual(settings.schedules["nightly"], Schedule("30 2 * * *", "UTC"))
        self.assertEqual(self.load(text, load_config), settings.rules)

    def test_additional_schedules_retain_implicit_hourly_default(self):
        settings = self.load(document() + '[schedules.nightly]\ncron = "0 2 * * *"\n')
        self.assertEqual(settings.schedules["default"], Schedule())
        self.assertEqual(settings.schedules["nightly"].timezone, "UTC")

    def test_unknown_top_schedule_rule_keys_and_references_rejected(self):
        invalid = [
            document() + 'unknown = true\n',
            document() + '[schedules.default]\ncrno = "17 * * * *"\n',
            document().replace("version = 1", 'version = 1\nunknown = true'),
            document(profile="missing"),
            document(profile="missing", enabled=False),
            document().replace("version = 1", "version = 1\nschedules = []"),
            document().replace("version = 1", "version = 1\nschedules = 1"),
            document() + '[[schedules.default]]\ncron = "17 * * * *"\n',
            document().replace("version = 1", "version = 1\nschedules.default = 5"),
        ]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(SyncError):
                self.load(text)

    def test_profile_and_schedule_names_are_safe_filename_slugs(self):
        invalid = ["", "A", "0profile", "with_space", "two words", "../escape",
                   "a/b", "a\\b", "a\n", "a'", "a" * 41, 1, True, None]
        for name in invalid:
            with self.subTest(name=name), self.assertRaises(SyncError):
                rule(profile=name)
            with self.subTest(schedule=name), self.assertRaises(SyncError):
                Settings(schedules={name: Schedule()})
        self.assertEqual(rule(profile="a" + "0-" * 19 + "z").profile,
                         "a" + "0-" * 19 + "z")

    def test_rule_ids_keep_existing_nonempty_string_contract_not_filenames(self):
        for identifier in ("Family Movies", "releases_2026", "viewer/favorites"):
            self.assertEqual(rule(id=identifier).id, identifier)


class FingerprintTests(unittest.TestCase):
    def test_current_hash_exactly_original_fields_except_enabled(self):
        config = rule()
        values = original_values(config)
        del values["enabled"]
        self.assertEqual(config.fingerprint, digest(values))
        self.assertEqual(config.fingerprint, replace(config, enabled=False).fingerprint)
        self.assertEqual(config.fingerprint, replace(config, profile="nightly").fingerprint)

    def test_legacy_hash_exactly_original_full_fields_without_profile(self):
        for enabled in (True, False):
            config = rule(enabled=enabled, profile="nightly")
            self.assertEqual(config.legacy_fingerprint, digest(original_values(config)))
            self.assertNotEqual(config.fingerprint, config.legacy_fingerprint)
        self.assertEqual(rule(max_delete_fraction=1).fingerprint,
                         rule(max_delete_fraction=1.0).fingerprint)
        self.assertEqual(rule(max_delete_fraction=1).legacy_fingerprint,
                         rule(max_delete_fraction=1.0).legacy_fingerprint)

    def test_match_accepts_both_historic_enable_values_and_current(self):
        for enabled in (True, False):
            config = rule(enabled=enabled, profile="nightly")
            self.assertTrue(config.matches_fingerprint(config.fingerprint))
            for historic_enabled in (True, False):
                values = original_values(config)
                values["enabled"] = historic_enabled
                self.assertTrue(config.matches_fingerprint(digest(values)))
        for invalid in (None, False, 1, [], {}, "not-a-hash", "0" * 64):
            with self.subTest(invalid=invalid):
                self.assertFalse(rule().matches_fingerprint(invalid))

    def test_changed_semantics_cannot_reuse_current_or_either_legacy_baseline(self):
        original = rule()
        changes = dict(
            id="other", source="https://mdblist.com/lists/curator/other",
            destination="https://trakt.tv/users/viewer/lists/other",
            direction="bidirectional", deletion_policy="additions_only",
            order_authority="destination", allow_empty=True,
            max_delete_fraction=0.75, max_delete_count=50,
        )
        for field, value in changes.items():
            changed = replace(original, **{field: value})
            for enabled in (True, False):
                old = replace(original, enabled=enabled)
                with self.subTest(field=field, enabled=enabled):
                    self.assertNotEqual(changed.fingerprint, old.fingerprint)
                    self.assertFalse(changed.matches_fingerprint(old.fingerprint))
                    self.assertFalse(changed.matches_fingerprint(old.legacy_fingerprint))

    def test_schedules_are_not_part_of_rule_fingerprint(self):
        first = Settings(rules=[rule()])
        second = Settings(rules=[rule(enabled=False)],
                          schedules={"default": Schedule(cron="manual")})
        self.assertEqual(first.rules[0].fingerprint, second.rules[0].fingerprint)


class PlannerCompatibilityTests(unittest.TestCase):
    def test_legacy_baseline_after_profile_or_enabled_toggle_preserves_deletion(self):
        for enabled in (True, False):
            with self.subTest(historic_enabled=enabled):
                historic = rule(direction="bidirectional", enabled=enabled)
                current = replace(historic, enabled=True, profile="nightly")
                records = [{"media_type": "movie", "ids": {"tmdb": number}}
                           for number in (1, 2)]
                baseline = {"fingerprint": historic.legacy_fingerprint,
                            "source": records, "destination": records}
                result = plan(current,
                              Snapshot(SOURCE, "", "", [Item("movie", {"tmdb": 1})]),
                              Snapshot(DESTINATION, "", "", [Item("movie", {"tmdb": 1}),
                                                              Item("movie", {"tmdb": 2})]),
                              baseline)
                self.assertEqual([item.ids["tmdb"] for item in result.destination], ["1"])
                self.assertEqual(result.baseline["fingerprint"], current.fingerprint)

    def test_semantic_change_still_refuses_bidirectional_legacy_baseline(self):
        original = rule(direction="bidirectional")
        changed = replace(original, max_delete_count=50, profile="nightly")
        baseline = {"fingerprint": original.legacy_fingerprint,
                    "source": [], "destination": []}
        with self.assertRaisesRegex(SyncError, "fingerprint mismatch"):
            plan(changed, Snapshot(SOURCE, "", "", []),
                 Snapshot(DESTINATION, "", "", []), baseline)


if __name__ == "__main__":
    unittest.main()