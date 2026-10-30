<!-- Sync impact: template -> 1.0.0; all principles populated. Templates compatible; no deferred items. -->
# Reelign Constitution

## Core Principles
### I. Safe, Independent Synchronization
Use complete validated snapshots and typed media IDs. Preserve deterministic
newest-first source sequence through destination ranks. Never remove items after
incomplete reads, silently reset established state, or use title-based matching.
### II. Secrets and User Control
Ship no credentials, personal configurations or operational state. Every user
owns their authorizations. Encrypt durable OAuth/baselines, persist rotations
before mutations, minimize workflow permissions and never expose secrets to PRs.
### III. Test-First Compatibility
Write and observe failing tests before implementation. Preserve the 101-test
baseline; validate setup, scheduling, failure isolation and legacy state migration.
Shared engine changes MUST be backported with parity verification.
### IV. Efficient, Observable Operation
Batch deltas, honor rate limits, make no remote writes when unchanged, isolate
independent rule failures and provide safe summaries. No artificial rule count
cap; disclose provider/runtime limits and do not promise exact cron execution.
### V. Approachable, Honest Product
Provide complete cross-platform instructions, safe preview-first onboarding,
clear permissions, recovery and upgrade paths. Support own and public readable
sources, not unauthorized writes. No misleading API-policy or ranking claims.

## UX, Security and Performance Requirements
Fresh template copies MUST remain inert until configured and explicitly enabled.
Schedules use native validated GitHub cron/timezone workflows, never dynamic
expressions in on.schedule. Timing/enable changes MUST NOT erase deletion history.
Fresh allowlisted export only; no private Git history. Brand: Reelign, tagline
"Your lists. Perfectly aligned." Documentation must be searchable and accessible.

## Development Workflow and Quality Gates
Follow specify, clarify, plan, tasks, implement; tests first and exact paths in
traceable tasks. Validate clean install, secret audit, public/private sync parity,
CI, dry run, live order and repeat no-op before release. Keep personal deployment
credentials and membership unchanged; explicit reviewed migrations only.

## Governance
Changes require rationale, updated tests/docs and reviewed backport compatibility.
Use semantic versioning. This constitution overrides convenience shortcuts.

**Version**: 1.0.0 | **Ratified**: 2026-10-30 | **Last Amended**: 2026-10-30
