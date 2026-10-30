# Changelog

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