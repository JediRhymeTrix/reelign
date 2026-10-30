---
title: Sync behavior and safety contract
description: Understand identity matching, newest-added ordering, one-way and bidirectional reconciliation, deletion safeguards, verified writes, partial failures, and encrypted baseline semantics.
permalink: /behavior/
---

# Sync behavior and safety contract

Preview is a plan, not a guarantee that remote data cannot change. Read this contract before enabling mirror or bidirectional synchronization.

## Supported data and identity

Reelign synchronizes **movies and shows** in personal lists. It does not synchronize seasons, episodes, ratings, watched progress, watch history, comments, or account settings. Unsupported entries fail the rule rather than silently producing a partial list.

Identity is resolved using shared **IMDb, TMDB, Trakt, and TVDB IDs**, qualified by media type. Aliases can connect the same movie/show across providers even when one side supplies fewer IDs. Reelign does not guess by title/year or perform per-item title lookups. Conflicting aliases, invalid/missing usable IDs, or duplicate identities within a list stop reconciliation.

Snapshots must be complete. Counts, pagination/cursors, and relevant metadata are checked; a moving, truncated, or inconsistent response is not treated as an empty source. All enabled rules still need a source and destination accessible through the provider APIs.

## Read-only and writable endpoints

Your own source and another curator’s publicly readable source are equally supported. Read-only endpoints need API read access, not ownership. Writable Trakt lists must belong to the authenticated account. Writable MDBList lists must belong to the API-key account and be explicitly **static**, not dynamic/generated. Bidirectional rules require both sides writable.

These ownership checks run even during a preview. A preview failing ownership is not fixed by enabling apply.

## Ordering is explicit—not a screenshot of a source view

Source snapshots use newest-added-first order. Trakt uses addition timestamps when complete, with ascending rank as the stable tie-breaker; if all timestamps are absent it falls back to rank. Partly missing or invalid timestamps are rejected rather than guessed. MDBList uses the API’s newest-first `added` ordering and preserves server ties/cursors, including when addition dates are null.

Reelign writes **custom/rank order**, then rereads that order to verify it. It does not preserve an arbitrary source UI view sorted by rating, release date, title, or another display preference. A written Trakt list is set to default `rank` ascending so rank 1 appears first; this may be a metadata write even with no membership changes. MDBList custom ordering is set through its reorder operation.

`reorder: true` in a preview means the ordered membership differs, potentially because additions/removals are planned; it is not a count of an exact number of reorder API requests. Apply summaries can additionally report `default_sort_changed`. Baselines advance only after successful verification.

## One-way rules

For `source_to_destination`, source is the authority and destination is written. For `destination_to_source`, those roles reverse. The configured labels do not change based on provider.

- **`mirror`:** target membership and order become the authority’s snapshot. Target-only entries are removed subject to safeguards.
- **`additions_only`:** authority items come first in authority order, then target-only items in the target snapshot’s order. No target-only members are removed, but existing members can still be reordered.

`order_authority` is only relevant to bidirectional ordering; it does not override one-way direction.

## Bidirectional rules

Both endpoints converge to the same ordered membership:

- **`additions_only`:** take the union of current members. Removing an item on one side alone does not remove it from the other; a remaining copy can be added back.
- **`mirror`:** compare both current snapshots to the last **converged baseline**. A member removed from either prior endpoint is removed from the reconciled set; genuine new additions on either endpoint are retained. A genuine addition takes precedence when it overlaps a removal event under the identity-based comparison.

For example, if both baseline lists contain A and B, and you remove B from one while adding C to the other, the desired result is A and C on both. This is baseline-aware reconciliation, **not** “most recently edited list wins.” There are no item-level edit clocks or universal conflict-resolution guarantees.

Ordering starts with `order_authority`’s current sequence, filtered to surviving members. Items absent from that authority but present on the other side are appended in ascending available addition-timestamp order, missing timestamps last, with deterministic identity tie-breaking. This appended segment differs from the normal newest-first snapshot ordering. Both endpoints receive the resulting sequence.

### First bidirectional mirror: explicit bootstrap

A new bidirectional mirror with no saved rule baseline refuses to run unless `--bootstrap-state` is explicitly supplied (the manual workflow’s `bootstrap` checkbox).

Bootstrap unions the **current** members and applies the ordering contract; it cannot infer deletions predating that first baseline. Run a bootstrap preview, inspect it, then deliberately repeat bootstrap on first apply. A dry run saves no baseline. Existing corrupt/incompatible state, a mismatched rule fingerprint, or nonconverged stored baselines are not reset by bootstrap.

Bidirectional additions-only does not require baseline bootstrap, but still deserves preview and both-side ownership checks.

## Deletion protections

Before any mutation, planned removals on each written endpoint must pass all checks:

1. Refuse to empty a nonempty list unless `allow_empty=true`.
2. Refuse more than `max_delete_count` removals (default 100).
3. Refuse a removal fraction greater than `max_delete_fraction` of current membership (default 0.5).

These count **actual removed identities**, not net list-size changes. Allowing empty does not bypass count/fraction limits. If a mirror is blocked, verify the source and proposed removals before adjusting limits. Do not automatically increase them or divide a destructive change into runs just to bypass review.

## Apply sequence and partial failures

For each rule, Reelign:

1. Reads complete snapshots and calculates a pure plan using the current baseline.
2. Preflights every writable endpoint and rereads membership to detect concurrent edits.
3. On a dry run, returns counts here—no lists, OAuth, or baselines are written.
4. Ensures supported custom-sort settings, adds missing members on **all** written endpoints, and verifies additions before **any** removals for that rule.
5. Rechecks concurrent membership/read-only authority, removes approved extras, verifies membership, reorders when needed, and rereads all endpoints plus any read-only authority.
6. Saves that rule’s new encrypted baseline only after successful convergence.

“Additions before removals” is a per-rule contract, not a transaction across every rule. Neither provider offers a cross-service transaction. Failures may leave successful additions, metadata changes, deletions, or reorder operations already applied on a remote list. Reelign cannot roll those back atomically. Inspect actual lists and run a fresh preview after failure; do not assume “failed” means “nothing changed.”

Independent rule failures are reported and later independent rules can continue. Later rules touching a failed rule’s writable endpoint are skipped. Selected rules execute in dependency order, with upstream writers before readers and stable ordering among unconstrained rules. A selection filter does not implicitly select upstream rules. An authorization `401/403` or state-persistence failure stops the run globally. Any rule failure produces a nonzero overall exit status, even if earlier independent rules succeeded and checkpointed.

## OAuth and durable state

The encrypted version-1 state contains the active OAuth token pair and rule baselines (`fingerprint`, source identities, destination identities). The file has an authenticated format marker, random nonce, and AES-256-GCM ciphertext. Current code also reads compatible legacy state/fingerprints. It never silently discards missing, corrupt, or wrong-key state.

Before consuming a single-use refresh token, normal execution preflights persistence. A new token pair is saved **before list work**, so unrelated list errors cannot discard successful rotation. A failed state push after rotation is still a recovery incident: the local encrypted file/artifact may be the only copy containing the new token. Dry runs never rotate or advance baselines.

There is no list write when membership/order/default sort already match. A first successful rule run may still save its initial baseline; OAuth rotation may also produce a state-only commit. Thus “no list changes” is not a promise of zero state writes in every normal run.

For key backup, persistence failures, intentional semantic config changes, and reauthorization without losing deletion history, see [Recovery](recovery.md).