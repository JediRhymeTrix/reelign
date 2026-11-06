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

### Trakt API-contract reads (1.2)

Controlled 251-item tests reproduce mixed old/new pages after acknowledged
reorder, including on this documented route. Standard no-cache headers do not
guarantee freshness. The reader rejects those responses; durable staged recovery
below handles later verification without weakening consistency checks.

The documented `/items/movie,show,season,episode/rank/asc` route requests explicit
rank/ascending order with requested page size 250. All four types are included so
unsupported seasons/episodes fail explicitly rather than being silently filtered.
Every response must supply its effective page size, page/count headers and complete
rows. Multi-page reads are independently confirmed with a second complete traversal
using different page boundaries (at most 100 items/page). Entry IDs, typed identities,
ranks and addition dates must agree. Missing headers, gaps, partial pages, conflicting
identities and unsupported media fail closed; no deduplication repairs a snapshot.

This detects many mixed-generation responses but **does not provide API snapshot
isolation**. Two matching observations are not a server transaction. If distinct
effective partitions cannot be obtained, the paginated read fails closed.

After our own acknowledged write only, a lagging summary count may be superseded by
independently confirmed complete items matching the exact expected membership and
acknowledged list count. Unrelated metadata edits remain errors. Original metadata
is retained, not fabricated; in-memory confirmation evidence is rechecked through
the checkpoint. Other count disagreements remain failures, not permission to delete.

### Bounded recovery when a list changes during a read

If otherwise valid responses show a changed total/page count or metadata, Reelign
discards that entire snapshot and starts again from fresh metadata and page one
(or a fresh MDBList cursor). It makes **at most three total attempts**, waiting
**1 second, then 2 seconds** between attempts. Stable reads add no retry delay;
multi-page Trakt confirmation still makes the additional traversal described above.

Observed malformed data is validated **before** deciding to retry: wrong pages,
invalid IDs, duplicate identities/ranks, unsupported media, invalid cursors,
internally impossible page counts and incomplete final snapshots remain errors,
even when count/metadata drift is also present. No partial snapshot is returned.
If all read attempts change, the rule fails closed without advancing its baseline.

This is a read-only retry boundary, **not** an entire-sync retry. It does not
replay additions/removals/reorders, OAuth refresh, or exhausted transport/auth/quota
failures. GET transport retries remain bounded; ambiguous mutation replay is
disabled as described below. All convergence/deletion checks remain in force.
A failure after earlier writes can still require a new preview;
read recovery does not make cross-service operations transactional.

Warnings identify only provider and attempt. These retries address transient
snapshot failures, not delayed/missing GitHub cron triggers; scheduling is unchanged.

### Trakt read-after-write settling

Trakt may briefly return mixed old/new counts or ranks after acknowledging a
write. Following **our own validated add, remove or reorder response only**, the
next verification snapshot has up to **five full read attempts**, waiting
**2, 5, 10 and 20 seconds**. It verifies acknowledged additions are visible,
removed items are absent and reordered list-entry IDs match the requested order.
A complete but stale response therefore does not prematurely pass verification.

Temporary overlapping ranks/entry IDs in this acknowledged-write context trigger
a fresh read, **not deduplication or acceptance**. The same duplicate response
without our own pending write still fails immediately. Conflicting provider IDs,
malformed/mixed dates for addition ordering, unsupported media, invalid pagination,
auth/quota/transport failures remain terminal. Neither a successful write nor a
failed/ambiguous write response is replayed by this verification policy.

Successful verified visibility clears the in-memory marker. If an acknowledged
write remains inconsistent, the encrypted journal survives for the next scheduled
run. Its report is `status: pending_verification`, `converged: false`; the confirmed
baseline does not advance and no further mutations occur for that rule. Healthy
deferral exits zero, which means the executor safely deferred—not that lists synced.
Dependent endpoint readers/writers are blocked, including across selection filters.
After24hours unresolved pending work becomes action-required/nonzero. There is no
automatic whole-workflow rerun or consistency deadline guaranteed by Trakt.

### Mutation outcomes and quota compliance

Authenticated Trakt POST/PUT/DELETE sends share a minimum one-second interval per
HTTP client across rules. Explicit rejected 429 responses may retry supported list
operations within bounds, honoring Retry-After; long/exhausted rejection quiet
periods also block later independent rules on that client. OAuth and list creation
are not replayed. Missing Retry-After uses bounded exponential backoff with jitter.

Timeouts, connection/read loss, 5xx or invalid JSON after a mutation have an
**unknown outcome** and are never automatically replayed. Their intent/outcome
remains encrypted. A later strict observation may prove the requested effect;
otherwise recovery stays blocked/nonzero, even if the old before-state reappears.
Partial/malformed mutation responses also require reviewed recovery. Baselines do
not advance until every phase, final membership/order and attribution are verified.

### Durable staged recovery

Each actual membership batch, rank request and metadata PUT has a durable intent
before send and an outcome checkpoint afterward. A process interrupted between
these saves cannot cause a blind resend. State persistence errors stop the entire
run. Confirmed baseline advancement and journal removal happen in one atomic save.

The journal retains intended reconciliation, expected own progress, endpoint IDs,
operation predicates and cumulative deletion limits. After verification, one-way
rules replan against fresh authority; bidirectional rules merge observed external
net changes against expected progress, rather than treating our partial writes as
user deletions. Conflicting or regressed evidence fails closed. This observes net
changes, not invisible remove/re-add history; providers offer no cross-service CAS.

Schema1 encrypted state remains readable. The first journaled mutation writes
payload schema2 with the same encryption key and OAuth. Old binaries intentionally
refuse schema2 instead of silently discarding pending operations. Do not downgrade
an active journal or delete it as a retry mechanism. Local atomic replacement
protects process interruption; power-loss durability depends on filesystem/storage.

Current staged recovery supports one complete MDBList reorder request (up to1000
items). Larger MDBList reorders are refused before the first reorder mutation;
anchored multi-request recovery needs a separate verified projection contract.
Trakt reorders and membership batching retain their existing supported behavior.

## Read-only and writable endpoints

Your own source and another curator’s publicly readable source are equally supported. Read-only endpoints need API read access, not ownership. Writable Trakt lists must belong to the authenticated account. Writable MDBList lists must belong to the API-key account and be explicitly **static**, not dynamic/generated. Bidirectional rules require both sides writable.

These ownership checks run even during a preview. A preview failing ownership is not fixed by enabling apply.

## Ordering is explicit—not a screenshot of a source view

Source snapshots use newest-added-first order. Trakt uses addition timestamps when complete, with ascending rank as the stable tie-breaker; if all timestamps are absent it falls back to rank. Partly missing or invalid timestamps are rejected rather than guessed. MDBList uses the API’s newest-first `added` ordering and preserves server ties/cursors, including when addition dates are null.

Reelign writes **custom/rank order**, then rereads that order to verify it. It does not preserve an arbitrary source UI view sorted by rating, release date, title, or another display preference. A written Trakt list is set to default `rank` ascending so rank 1 appears first; this may be a metadata write even with no membership changes. MDBList custom ordering is set through its reorder operation.

`reorder: true` in a preview means the ordered membership differs, potentially because additions/removals are planned; it is not a count of an exact number of reorder API requests. Apply summaries can additionally report `default_sort_changed`. Baselines advance only after successful verification.

## One-way rules

### Source credit and destination descriptions

By default, supported destinations retain their existing description and receive
one compact credit block linking the **original list**, **its creator**, and
[Reelign](https://jedirhymetrix.github.io/reelign/) so readers can set up their own
sync. Trakt receives escaped Markdown links, not raw HTML or BBCode:

```markdown
Synced from [Original list](https://mdblist.com/lists/curator/films) by [Creator (@curator)](https://mdblist.com/@curator).

Powered by [Reelign](https://jedirhymetrix.github.io/reelign/). Your lists. Perfectly aligned. Set up automatic MDBList ↔ Trakt syncing for your own lists or public favorites.
```

Reelign replaces only its own complete generated block, preserves surrounding
owner prose, checks for concurrent description edits, and verifies a
description-only update. Unchanged descriptions are not written. Dry runs report
`would_update` without mutation; apply reports `updated` or `unchanged`.

Set `attribution = false` on a rule to leave descriptions unmanaged. A private or
unknown-visibility source is never automatically linked from a public Trakt
destination: the rule stops before list changes so you can choose a private target
or explicitly disable attribution. Descriptions are never silently truncated.

**MDBList API limitation:** current documented metadata updates accept only name
and privacy, not description. MDBList destinations report
`unsupported_destination_api` without speculative writes; reverse/bidirectional
item sync still works. You can copy the credit manually into MDBList. A two-way
rule credits the opposite list on each writable, supported destination and does
not copy the opposite list's description recursively.

The explicit `create-destination` helper includes credit in new private Trakt
lists as well. Name, privacy and list items are not changed by description updates.

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
2. Preflights every writable endpoint and rereads membership to detect concurrent edits. Every refreshed target snapshot revalidates identity and permissions before later writes/checkpointing.
3. On a dry run, returns counts here—no lists, OAuth, or baselines are written.
4. Ensures supported custom-sort settings, adds missing members on **all** written endpoints, and verifies additions before **any** removals for that rule.
5. Rechecks concurrent membership/read-only authority, removes approved extras, verifies membership, reorders when needed using the current validated entry IDs, and rereads all endpoints plus any read-only authority. Empty phases reuse validated observations rather than performing redundant full reads.
6. Saves that rule’s new encrypted baseline only after successful convergence.

“Additions before removals” is a per-rule contract, not a transaction across every rule. Neither provider offers a cross-service transaction. Failures may leave successful additions, metadata changes, deletions, or reorder operations already applied on a remote list. Reelign cannot roll those back atomically. Inspect actual lists and run a fresh preview after failure; do not assume “failed” means “nothing changed.”

Independent rule failures are reported and later independent rules can continue. Later rules touching a failed rule’s writable endpoint are skipped. Selected rules execute in dependency order, with upstream writers before readers and stable ordering among unconstrained rules. A selection filter does not implicitly select upstream rules. An authorization `401/403` or state-persistence failure stops the run globally. Any rule failure produces a nonzero overall exit status, even if earlier independent rules succeeded and checkpointed.

## OAuth and durable state

Encrypted state contains the active OAuth token pair and confirmed rule baselines
(`fingerprint`, source identities, destination identities). Payload schema2 adds
pending operation journals; schema1 and compatible legacy state/fingerprints remain
readable. The file has an authenticated format marker, random nonce and AES-256-GCM
ciphertext. Missing, corrupt or wrong-key state is never silently discarded.

Before consuming a single-use refresh token, normal execution preflights persistence. A new token pair is saved **before list work**, so unrelated list errors cannot discard successful rotation. A failed state push after rotation is still a recovery incident: the local encrypted file/artifact may be the only copy containing the new token. Dry runs never rotate or advance baselines.

There is no list write when membership/order/default sort already match. A first successful rule run may still save its initial baseline; OAuth rotation may also produce a state-only commit. Thus “no list changes” is not a promise of zero state writes in every normal run.

For key backup, persistence failures, intentional semantic config changes, and reauthorization without losing deletion history, see [Recovery](recovery.md).