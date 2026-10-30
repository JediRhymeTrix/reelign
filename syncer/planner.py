"""Pure identity-aware reconciliation; no provider calls or state mutations."""

from dataclasses import dataclass

from .config import Rule
from .model import Item, Snapshot, SyncError, identify


@dataclass
class Plan:
    source: list[Item]
    destination: list[Item]
    baseline: dict
    # Flags designate writable endpoints, not actual deltas. The runner must
    # independently compare membership and remote ranks before issuing writes.
    writes_source: bool
    writes_destination: bool


def _baseline_items(rule: Rule, baseline: dict | None) -> tuple[list[Item], list[Item]]:
    if baseline is None:
        return [], []
    if not isinstance(baseline, dict) or set(baseline) != {"fingerprint", "source", "destination"}:
        raise SyncError("Invalid reconciliation baseline; refusing unsafe reconciliation")
    if not rule.matches_fingerprint(baseline["fingerprint"]):
        if rule.direction == "bidirectional" and rule.deletion_policy == "mirror":
            raise SyncError("Baseline rule fingerprint mismatch; refusing bidirectional mirror")
        # A changed one-way/additions-only rule has no deletion history to reuse.
        return [], []
    collections = []
    for side in ("source", "destination"):
        records = baseline[side]
        if not isinstance(records, list):
            raise SyncError("Baseline endpoints must contain identity arrays")
        items = []
        for record in records:
            if (not isinstance(record, dict) or set(record) != {"media_type", "ids"}
                    or not isinstance(record["media_type"], str)
                    or not isinstance(record["ids"], dict)
                    or any(not isinstance(key, str) or type(value) not in (str, int)
                           for key, value in record["ids"].items())):
                raise SyncError("Invalid baseline media identity")
            items.append(Item(record["media_type"], dict(record["ids"])))
        collections.append(items)
    return collections[0], collections[1]


def _check_deletions(rule: Rule, current: list[str], desired: list[str]) -> None:
    removed = set(current) - set(desired)
    if not removed:
        return
    if not desired and not rule.allow_empty:
        raise SyncError("Refusing to empty a list; allow_empty is false")
    if len(removed) > rule.max_delete_count:
        raise SyncError("Deletion count exceeds max_delete_count")
    if len(removed) / len(current) > rule.max_delete_fraction:
        raise SyncError("Deletion fraction exceeds max_delete_fraction")


def validate_deletions(rule: Rule, current: list[Item], desired: list[Item]) -> None:
    """Guard actual identity removals, not net list-size differences.

    Identify both sides together so adding aliases is not mistaken for deletion.
    Empty opt-in does not bypass either mass-deletion limit.
    """
    sequences, _ = identify(current, desired)
    _check_deletions(rule, sequences[0], sequences[1])


def _render(keys: list[str], existing: dict[str, Item], representatives: dict[str, Item]) -> list[Item]:
    """Merge safe identity aliases while retaining target-local entry IDs/ranks."""
    items = []
    for key in keys:
        identity = representatives[key]
        local = existing.get(key)
        items.append(Item(
            identity.media_type, dict(identity.ids),
            added_at=local.added_at if local is not None else None,
            rank=local.rank if local is not None else 0,
            entry_id=local.entry_id if local is not None else None,
        ))
    return items


def plan(rule: Rule, source: Snapshot, destination: Snapshot, baseline: dict | None,
         bootstrap: bool = False) -> Plan:
    """Compute intended endpoint sequences and a checkpoint for verified success.

    Snapshot items already have newest-first ordering; the authority determines
    supplies authority; order_authority resolves bidirectional ordering conflicts.
    Bidirectional mirror uses per-endpoint history: remove anything missing from
    its prior endpoint, then retain genuine additions to either endpoint. Explicit
    bootstrap unions current membership only when no baseline exists.
    """
    prior_source, prior_destination = _baseline_items(rule, baseline)
    # Resolve all aliases in one graph BEFORE comparing baseline/current keys.
    sequences, representatives = identify(
        source.items, destination.items, prior_source, prior_destination,
    )
    source_keys, destination_keys, old_source_keys, old_destination_keys = sequences
    if (baseline is not None and rule.direction == "bidirectional"
            and rule.deletion_policy == "mirror"
            and set(old_source_keys) != set(old_destination_keys)):
        raise SyncError("Baseline endpoints did not converge; refusing bidirectional mirror")
    local_source = dict(zip(source_keys, source.items))
    local_destination = dict(zip(destination_keys, destination.items))
    desired_source = list(source_keys)
    desired_destination = list(destination_keys)
    writes_source = rule.enabled and rule.direction != "source_to_destination"
    writes_destination = rule.enabled and rule.direction != "destination_to_source"

    if not rule.enabled:
        pass
    elif rule.direction == "bidirectional":
        source_set, destination_set = set(source_keys), set(destination_keys)
        desired = source_set | destination_set
        if rule.deletion_policy == "mirror":
            if baseline is None and not bootstrap:
                raise SyncError("Bidirectional mirror requires a baseline or explicit bootstrap")
            if baseline is not None:
                old_source, old_destination = set(old_source_keys), set(old_destination_keys)
                removed = (old_source - source_set) | (old_destination - destination_set)
                added = (source_set - old_source) | (destination_set - old_destination)
                desired = (desired - removed) | added
        if rule.order_authority == "source":
            authority, minority = source_keys, local_destination
        else:
            authority, minority = destination_keys, local_source
        ordered = [key for key in authority if key in desired]
        extras = desired - set(ordered)

        def addition_order(key):
            timestamp = minority[key].added_at
            return (timestamp is None or timestamp == "", timestamp or "", key)

        ordered.extend(sorted(extras, key=addition_order))
        desired_source, desired_destination = list(ordered), list(ordered)
    else:
        if rule.direction == "source_to_destination":
            authority, target = source_keys, destination_keys
        else:
            authority, target = destination_keys, source_keys
        intended = list(authority)
        if rule.deletion_policy == "additions_only":
            authoritative_set = set(authority)
            intended.extend(key for key in target if key not in authoritative_set)
        if rule.direction == "source_to_destination":
            desired_destination = intended
        else:
            desired_source = intended

    if writes_source:
        _check_deletions(rule, source_keys, desired_source)
    if writes_destination:
        _check_deletions(rule, destination_keys, desired_destination)
    result_source = _render(desired_source, local_source, representatives)
    result_destination = _render(desired_destination, local_destination, representatives)

    def records(items):
        return [{"media_type": item.media_type, "ids": dict(item.ids)} for item in items]

    intended_baseline = {
        "fingerprint": rule.fingerprint,
        "source": records(result_source),
        "destination": records(result_destination),
    }
    return Plan(result_source, result_destination, intended_baseline, writes_source, writes_destination)
