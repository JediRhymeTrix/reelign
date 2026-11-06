"""Offline execution phase barriers, freshness and minimal snapshot reads."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.model import Item, Snapshot, SyncError
from syncer.runner import execute_targets


def movie(number):
    return Item("movie", {"tmdb": number}, entry_id=1000 + number)


class PhaseProvider:
    """No hidden reads: reorder must use the exact snapshot supplied by runner."""

    def __init__(self, name, items, events=None, sort_changed=False):
        self.name = name
        self.items = list(items)
        self.events = events if events is not None else []
        self.sort_changed = sort_changed
        self.reads = []
        self.reorders = []
        self.hooks = {}

    def event(self, phase):
        self.events.append((self.name, phase))
        if hook := self.hooks.get(phase):
            hook()

    def snapshot(self, order="added"):
        self.event("snapshot")
        result = Snapshot(self.name, self.name, "", list(self.items), writable=True)
        self.reads.append((order, result))
        return result

    def preflight(self, snapshot):
        self.event("preflight")
        if not snapshot.writable:
            raise SyncError("Not writable")

    def ensure_rank_sort(self, snapshot):
        self.event("sort")
        return self.sort_changed

    def add(self, additions):
        self.items.extend(additions)
        self.event("add")

    def remove(self, removals):
        aliases = set().union(*(item.aliases() for item in removals))
        self.items = [item for item in self.items if not item.aliases() & aliases]
        self.event("remove")

    def reorder(self, desired, current):
        self.reorders.append((list(desired), current))
        self.items = list(desired)
        self.event("reorder")


class ExecutionPhaseTests(unittest.TestCase):
    def test_fresh_snapshot_is_repreflighted_before_any_write(self):
        class Changed(PhaseProvider):
            def snapshot(self, order="added"):
                result = super().snapshot(order)
                if order == "rank":
                    result.writable = False
                return result
        provider = Changed("changed", [movie(1), movie(2)])
        target = (provider, provider.snapshot(), [movie(1)])
        with self.assertRaises(SyncError):
            execute_targets([target])
        self.assertFalse(any(phase in {"sort", "add", "remove", "reorder"}
                             for _, phase in provider.events))

    def test_changed_remote_list_identity_is_pinned_before_writes(self):
        class Changed(PhaseProvider):
            def snapshot(self, order="added"):
                result = super().snapshot(order)
                result.metadata["id"] = 13 if order == "rank" else 12
                return result
        provider = Changed("changed", [movie(1), movie(2)])
        with self.assertRaises(SyncError):
            execute_targets([(provider, provider.snapshot(), [movie(1)])])
        self.assertFalse(any(phase in {"sort", "add", "remove", "reorder"}
                             for _, phase in provider.events))

    def test_predelete_refresh_revalidates_destination(self):
        class Changed(PhaseProvider):
            def snapshot(self, order="added"):
                result = super().snapshot(order)
                if len(self.reads) == 3:
                    result.writable = False
                return result
        provider = Changed("changed", [movie(1), movie(2)])
        with self.assertRaises(SyncError):
            execute_targets([(provider, provider.snapshot(), [movie(1)])])
        self.assertFalse(any(phase == "remove" for _, phase in provider.events))

    def setUp(self):
        self.a, self.b, self.c = [movie(number) for number in (1, 2, 3)]
        self.events = []

    def provider(self, name, items, **kwargs):
        return PhaseProvider(name, items, self.events, **kwargs)

    def target(self, provider, desired):
        return provider, provider.snapshot(), desired

    def assert_no_phase(self, *phases):
        self.assertFalse([event for event in self.events if event[1] in phases])

    def test_phase_read_budget_includes_one_planning_read(self):
        cases = [
            ("noop", [self.a], [self.a], False, 2),
            ("add", [self.a], [self.a, self.b], False, 3),
            ("rank", [self.b, self.a], [self.a, self.b], False, 3),
            ("add_rank", [self.a], [self.b, self.a], False, 4),
            ("remove", [self.a, self.b], [self.a], False, 4),
            ("remove_rank", [self.b, self.a, self.c], [self.a, self.b], False, 5),
            ("default_sort", [self.a], [self.a], True, 3),
        ]
        for name, initial, desired, sort_changed, execution_reads in cases:
            with self.subTest(phase=name):
                provider = self.provider(name, initial, sort_changed=sort_changed)
                summary = execute_targets([self.target(provider, desired)])
                self.assertEqual(len(provider.reads), execution_reads + 1)
                self.assertEqual([order for order, _ in provider.reads[1:]],
                                 ["rank"] * execution_reads)
                self.assertEqual(provider.items, desired)
                self.assertEqual(summary[0]["default_sort_changed"], sort_changed)

    def test_all_preflights_and_fresh_reads_precede_writes(self):
        left = self.provider("left", [])
        right = self.provider("right", [])
        targets = [self.target(left, [self.a]), self.target(right, [self.a])]
        self.events.clear()
        execute_targets(targets)
        first_write = min(i for i, event in enumerate(self.events)
                          if event[1] in {"sort", "add", "remove", "reorder"})
        self.assertEqual(self.events[:first_write], [
            ("left", "preflight"), ("left", "snapshot"),
            ("left", "preflight"),
            ("right", "preflight"), ("right", "snapshot"),
            ("right", "preflight"),
        ])

    def test_planning_membership_race_stops_before_default_sort(self):
        provider = self.provider("left", [self.a])
        target = self.target(provider, [self.b])
        provider.items.append(self.c)
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "changed while planning"):
            execute_targets([target])
        self.assert_no_phase("sort", "add", "remove", "reorder")

    def test_planning_rank_race_uses_fresh_rank_order(self):
        provider = self.provider("left", [self.a, self.b])
        target = self.target(provider, [self.a, self.b])
        provider.items.reverse()
        execute_targets([target])
        self.assertEqual(len(provider.reads), 4)
        self.assertIs(provider.reorders[0][1], provider.reads[1][1])

    def test_noop_endpoints_reuse_initial_until_final_read(self):
        left = self.provider("left", [self.a])
        right = self.provider("right", [self.a])
        execute_targets([self.target(left, [self.a]), self.target(right, [self.a])])
        self.assertEqual(len(left.reads), 3)
        self.assertEqual(len(right.reads), 3)

    def test_deletion_only_does_not_refresh_unchanged_endpoint_until_final(self):
        left = self.provider("left", [self.a])
        right = self.provider("right", [self.a, self.b])
        execute_targets([self.target(left, [self.a]), self.target(right, [self.a])])
        self.assertEqual(len(left.reads), 3)
        self.assertEqual(len(right.reads), 5)

    def test_all_additions_and_all_verifications_precede_any_delete(self):
        left = self.provider("left", [self.a])
        right = self.provider("right", [self.a])
        targets = [self.target(left, [self.b]), self.target(right, [self.c])]
        self.events.clear()
        execute_targets(targets)
        first_delete = self.events.index(("left", "remove"))
        last_add = max(i for i, event in enumerate(self.events) if event[1] == "add")
        self.assertLess(last_add, first_delete)
        self.assertEqual(self.events[last_add + 1:first_delete],
                         [("left", "snapshot"), ("left", "preflight"),
                          ("right", "snapshot"), ("right", "preflight")])

    def test_addition_refreshes_unchanged_other_endpoint_before_any_delete(self):
        for mutation in ([], [movie(3)]):
            with self.subTest(mutation=mutation):
                left = self.provider("left", [self.a])
                right = self.provider("right", [self.a])
                targets = [self.target(left, [self.a]), self.target(right, [self.b])]
                right.hooks["add"] = lambda: setattr(left, "items", list(mutation))
                self.events.clear()
                with self.assertRaises(SyncError):
                    execute_targets(targets)
                self.assert_no_phase("remove", "reorder")
                self.assertEqual(len(left.reads), 3)

    def test_sort_write_refreshes_unchanged_other_endpoint_before_any_delete(self):
        left = self.provider("left", [self.a])
        right = self.provider("right", [self.a, self.b], sort_changed=True)
        targets = [self.target(left, [self.a]), self.target(right, [self.a])]
        right.hooks["sort"] = lambda: left.items.clear()
        self.events.clear()
        with self.assertRaises(SyncError):
            execute_targets(targets)
        self.assert_no_phase("remove", "reorder")

    def test_pure_deletion_refreshes_immediately_before_destructive_phase(self):
        provider = self.provider("left", [self.a, self.b])
        target = self.target(provider, [self.a])
        provider.hooks["sort"] = lambda: provider.items.append(self.c)
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "changed during additions"):
            execute_targets([target])
        self.assert_no_phase("remove", "reorder")

    def test_missing_addition_never_removes_existing_items(self):
        provider = self.provider("left", [self.a])
        target = self.target(provider, [self.b])
        provider.hooks["add"] = lambda: provider.items.remove(self.b)
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "Addition verification"):
            execute_targets([target])
        self.assert_no_phase("remove", "reorder")

    def test_missing_initial_item_never_removes_from_other_endpoint(self):
        left = self.provider("left", [self.a, self.c])
        right = self.provider("right", [self.a, self.c])
        targets = [self.target(left, [self.b]), self.target(right, [self.b])]
        left.hooks["add"] = lambda: left.items.remove(self.a)
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "changed during additions"):
            execute_targets(targets)
        self.assert_no_phase("remove", "reorder")

    def test_reorder_receives_latest_validated_snapshot_without_an_extra_read(self):
        provider = self.provider("left", [self.a])
        desired = [self.b, self.a]
        execute_targets([self.target(provider, desired)])
        self.assertEqual(len(provider.reorders), 1)
        passed_desired, current = provider.reorders[0]
        self.assertEqual(passed_desired, desired)
        self.assertIs(current, provider.reads[2][1])
        self.assertEqual(current.items, [self.a, self.b])
        self.assertEqual([item.entry_id for item in current.items], [1001, 1002])

    def test_all_removal_memberships_checked_before_any_reorder(self):
        left = self.provider("left", [self.b, self.a, self.c])
        right = self.provider("right", [self.a, self.c])
        targets = [self.target(left, [self.a, self.b]), self.target(right, [self.a])]
        right.hooks["remove"] = lambda: right.items.clear()
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "Membership verification"):
            execute_targets(targets)
        self.assert_no_phase("reorder")

    def test_cross_endpoint_removal_cannot_be_masked_by_reordering_other_endpoint(self):
        left = self.provider("left", [self.b, self.a])
        right = self.provider("right", [self.a, self.c])
        targets = [self.target(left, [self.a, self.b]), self.target(right, [self.a])]
        right.hooks["remove"] = lambda: left.items.clear()
        self.events.clear()
        with self.assertRaises(SyncError):
            execute_targets(targets)
        self.assert_no_phase("reorder")

    def test_cross_endpoint_removal_from_noop_blocks_another_endpoint_reorder(self):
        left = self.provider("left", [self.a])
        right = self.provider("right", [self.b, self.a, self.c])
        targets = [self.target(left, [self.a]), self.target(right, [self.a, self.b])]
        right.hooks["remove"] = lambda: left.items.clear()
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "Membership verification"):
            execute_targets(targets)
        self.assert_no_phase("reorder")

    def test_valid_removal_refreshes_other_endpoint_only_when_reorder_needed(self):
        left = self.provider("left", [self.b, self.a])
        right = self.provider("right", [self.a, self.c])
        execute_targets([self.target(left, [self.a, self.b]), self.target(right, [self.a])])
        self.assertEqual(len(left.reads), 5)
        self.assertEqual(len(right.reads), 5)
        self.assertIs(left.reorders[0][1], left.reads[2][1])

    def test_final_read_catches_noop_endpoint_changed_during_other_removal(self):
        left = self.provider("left", [self.a])
        right = self.provider("right", [self.a, self.b])
        targets = [self.target(left, [self.a]), self.target(right, [self.a])]
        right.hooks["remove"] = lambda: left.items.clear()
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "before checkpoint"):
            execute_targets(targets)
        self.assert_no_phase("reorder")

    def test_final_read_catches_noop_endpoint_changed_during_other_reorder(self):
        left = self.provider("left", [self.a])
        right = self.provider("right", [self.b, self.a])
        targets = [self.target(left, [self.a]), self.target(right, [self.a, self.b])]
        right.hooks["reorder"] = lambda: left.items.clear()
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "before checkpoint"):
            execute_targets(targets)

    def test_authority_failure_prevents_even_default_sort_phase(self):
        provider = self.provider("left", [self.a], sort_changed=True)
        target = self.target(provider, [self.b])

        def guard():
            raise SyncError("Authority changed")

        self.events.clear()
        with self.assertRaisesRegex(SyncError, "Authority changed"):
            execute_targets([target], authority_guard=guard)
        self.assert_no_phase("sort", "add", "remove", "reorder")

    def test_authority_guard_precedes_each_write_phase_and_follows_final_read(self):
        provider = self.provider("left", [self.a, self.c])
        target = self.target(provider, [self.b, self.a])
        self.events.clear()
        execute_targets([target], authority_guard=lambda: self.events.append(("source", "guard")))
        sort = self.events.index(("left", "sort"))
        remove = self.events.index(("left", "remove"))
        reorder = self.events.index(("left", "reorder"))
        self.assertEqual(self.events[sort - 1], ("source", "guard"))
        self.assertEqual(self.events[remove - 1], ("source", "guard"))
        self.assertEqual(self.events[reorder - 1], ("source", "guard"))
        self.assertEqual(self.events[-3:], [("left", "snapshot"), ("left", "preflight"), ("source", "guard")])

    def test_authority_change_after_additions_blocks_deletions(self):
        provider = self.provider("left", [self.a])
        target = self.target(provider, [self.b])

        def guard():
            self.events.append(("source", "guard"))
            if ("left", "add") in self.events:
                raise SyncError("Authority changed")

        self.events.clear()
        with self.assertRaisesRegex(SyncError, "Authority changed"):
            execute_targets([target], authority_guard=guard)
        self.assert_no_phase("remove", "reorder")

    def test_authority_change_after_removal_blocks_rank_phase(self):
        provider = self.provider("left", [self.b, self.a, self.c])
        target = self.target(provider, [self.a, self.b])

        def guard():
            self.events.append(("source", "guard"))
            if ("left", "remove") in self.events:
                raise SyncError("Authority changed")

        self.events.clear()
        with self.assertRaisesRegex(SyncError, "Authority changed"):
            execute_targets([target], authority_guard=guard)
        self.assert_no_phase("reorder")

    def test_authority_change_at_final_checkpoint_is_not_success(self):
        provider = self.provider("left", [self.a])
        target = self.target(provider, [self.a])

        def guard():
            if len(provider.reads) == 3:
                raise SyncError("Authority changed at checkpoint")

        with self.assertRaisesRegex(SyncError, "at checkpoint"):
            execute_targets([target], authority_guard=guard)

    def test_failed_rank_write_verification_prevents_checkpoint(self):
        provider = self.provider("left", [self.b, self.a])
        target = self.target(provider, [self.a, self.b])
        provider.hooks["reorder"] = lambda: provider.items.reverse()
        self.events.clear()
        with self.assertRaisesRegex(SyncError, "Rank verification"):
            execute_targets([target])
        self.assertEqual(len(provider.reads), 3)

    def test_unknown_add_error_propagates_without_deletions_or_checkpoint(self):
        provider = self.provider("left", [self.a])
        target = self.target(provider, [self.b])

        def fail():
            raise RuntimeError("unknown outcome")

        provider.hooks["add"] = fail
        self.events.clear()
        with self.assertRaisesRegex(RuntimeError, "unknown outcome"):
            execute_targets([target], authority_guard=lambda: self.events.append(("source", "guard")))
        self.assert_no_phase("remove", "reorder")
        self.assertEqual(self.events[-1], ("left", "add"))

    def test_unknown_verification_error_never_deletes_or_checkpoints(self):
        provider = self.provider("left", [self.a])
        target = self.target(provider, [self.b])

        def fail_later_reads():
            def fail():
                raise RuntimeError("unknown read outcome")
            provider.hooks["snapshot"] = fail

        provider.hooks["add"] = fail_later_reads
        self.events.clear()
        with self.assertRaisesRegex(RuntimeError, "unknown read outcome"):
            execute_targets([target], authority_guard=lambda: self.events.append(("source", "guard")))
        self.assert_no_phase("remove", "reorder")
        self.assertEqual(self.events[-1], ("left", "snapshot"))

    def test_dry_run_has_only_prechecks_and_never_invokes_guard(self):
        provider = self.provider("left", [self.a], sort_changed=True)
        target = self.target(provider, [self.b])
        self.events.clear()
        summary = execute_targets([target], dry_run=True,
                                  authority_guard=lambda: self.fail("Dry run invoked authority guard"))
        self.assertEqual(self.events, [("left", "preflight"), ("left", "snapshot"), ("left", "preflight")])
        self.assertEqual(len(provider.reads), 2)
        self.assertEqual(summary, [{"add": 1, "remove": 1, "reorder": True, "dry_run": True}])
        self.assertEqual(provider.items, [self.a])


if __name__ == "__main__":
    unittest.main()