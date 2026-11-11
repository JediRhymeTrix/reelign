# Changelog

## 1.2.1 — 2026-11-11

- Accept complete Trakt snapshots with positive, unique, strictly ascending rank
  labels even when manual deletions leave gaps. Rank labels are not pagination
  offsets; valid gaps must reach direction-aware reconciliation instead of
  permanently failing the rule before planning.
- One-way mirrors restore missing target items from their authority; reverse,
  bidirectional and additions-only policies retain their existing semantics.
  Gaps alone cause no writes when membership and relative order already match.
- Preserve duplicate-entry/rank, conflicting-ID, metadata/count, page geometry,
  partition confirmation and unknown-operation no-replay protections.
- Add regression coverage for multi-page gaps, staged restoration and direction/
  deletion-policy semantics. No schedule, state-format or retry-budget changes.

## 1.2.0 — 2026-11-06

- Use the documented typed/sorted Trakt list route instead of bare `/items`.
  This is protocol alignment, **not a verified fix** for server page consistency.
- Redesign Trakt reads around explicit rank order, effective pagination headers
  and independent complete traversal confirmation with different page boundaries.
  Reject mixed observations without claiming transactional API snapshots.
- Verify acknowledged writes against exact expected membership and acknowledgement
  list summaries, allowing an unchanged stale summary count only with independently
  confirmed complete items; retain original metadata and fail on unrelated edits.
- Remove redundant empty-phase reads and hidden reorder reads. Revalidate target
  identity/permissions on every refreshed snapshot before writes/checkpointing.
- Never replay ambiguous mutations after timeouts, 5xx or invalid responses.
  Pace authenticated Trakt writes and preserve account Retry-After quiet periods
  across independent rules. Explicit rejected 429s retain bounded safe retries.
- Add encrypted write-ahead journals: persist each batch/rank/metadata intent before
  sending, retain acknowledged/unknown outcomes, and resume verification on later
  scheduled runs without replaying ambiguous mutations or advancing baselines early.
- Distinguish `converged`, `pending_verification` and blocked/error outcomes. Healthy
  acknowledged deferral exits zero but is explicitly not synchronized; unknown,
  malformed/partial responses and expired (24-hour) pending work require review.
- Resume bidirectional work using saved logical intent and verified own progress,
  external net-change rebasing and cumulative deletion budgets. Pending endpoints
  remain reserved even when their writer is not selected.
- Migrate encrypted payloads to schema2 on first journaled mutation, retaining
  the original key/OAuth and compatible schema1 reads. Older executors refuse2.
  State persistence failures stop all further mutations globally.
- Keep read retry budgets, schedules and configuration unchanged. No alerts,
  cache-busting parameters or automatic whole-workflow reruns. Trakt itself may
  still serve inconsistent pages; staged verification does not make reads atomic.

## 1.1.2 — 2026-11-06

- Add an acknowledged-write-only Trakt visibility barrier: up to five full reads
  with 2/5/10/20s waits after validated add/remove/reorder responses. Verify actual
  addition/removal visibility and exact entry-rank order before continuing.
- Retry temporary count/rank overlap only in that pending-write context; never
  deduplicate or accept inconsistent snapshots, replay a write, or advance a
  baseline after failed verification. Pre-write duplicates remain terminal.
- Preserve conflicting-ID/malformed-date validation before overlap handling, and
  use incremental typed identity indexing to retain aliases without quadratic scans.
- Independently reviewed, covered by 22 new regression tests and disposable live
  write/read verification. No cron, alert, OAuth or state-format changes.

## 1.1.1 — 2026-11-03

- Restart unstable complete list reads up to three total attempts with 1s/2s
  backoff, discarding all old pages/cursors and rereading metadata from the start.
- Preserve fail-closed validation: malformed content, unsupported media, invalid
  identities/ranks/cursors and impossible/incomplete pages are terminal even if
  the response simultaneously reports changed counts or metadata.
- Independent adversarial review and 20 new retry regression tests; no mutation,
  OAuth, schedule, alert, configuration or state-format changes.

## 1.1.0 — 2026-10-30

- Credit source lists and their creators in supported destination descriptions,
  with destination-native Markdown and a compact Reelign setup link.
- Preserve existing prose, replace only the managed footer, and verify
  description-only writes; dry-run and unchanged runs do not mutate descriptions.
- Add per-rule `attribution` opt-out without resetting reconciliation history.
- Guard against exposing private sources in public destination descriptions.
- Include credit during explicit private Trakt list creation; document MDBList's
  current lack of a description-update API without blocking reverse item sync.

## 1.0.0 — 2026-10-30

Initial standalone public template release.

- Sync own lists or publicly readable curator movie/show lists between MDBList
  and Trakt using each user's own credentials.
- Forward, reverse and bidirectional rules with mirror/additions-only policies.
- Preserve newest-first addition order through verified destination custom ranks.
- Native cron/IANA-timezone schedule profiles, manual selection and independent
  daily OAuth maintenance. Generated workflows are checked for drift.
- Local hidden-prompt/device authorization, optional explicit credential backup,
  fresh encrypted state initialization and state-aware OAuth recovery.
- AES-GCM durable baselines, single-use refresh persistence, deletion safeguards,
  complete pagination, batched deltas and repeated convergence verification.
- Independent rule failure isolation and dependency-ordered execution.
- Legacy encrypted-state/fingerprint compatibility and allowlisted backport parity.
- Detailed setup, configuration, scheduling, behavior, security and recovery docs.

### Boundaries
One provider-account pair per installation. Movies/shows only. No hosted service,
watch-history synchronization, shared API credentials or guaranteed cron timing.
Provider quotas/account restrictions and GitHub limits still apply.