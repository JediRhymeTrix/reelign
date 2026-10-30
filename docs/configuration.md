---
title: Configuration reference
description: Complete Reelign TOML schema for list URLs, directions, mirror and additions-only policies, deletion safeguards, enabled rules, and cron/timezone profiles.
permalink: /configuration/
---

# Configuration reference

Reelign reads strict, credential-free TOML from root `config.toml` by default. Unknown fields and incorrect types are errors, including on disabled rules. Credentials belong in Actions secrets or the local process environment, never in this file.

## Complete example

All URLs below are **placeholders requiring edits**. The rule is intentionally disabled.

```toml
version = 1

[schedules.default]
cron = "17 * * * *"
timezone = "UTC"

[schedules.weekend]
cron = "23 9 * * SAT"
timezone = "Europe/London"

[schedules.on-demand]
cron = "manual"
timezone = "UTC"

[[rules]]
id = "my-movies"
enabled = false
source = "https://mdblist.com/lists/your-user/your-list"
destination = "https://trakt.tv/users/your-user/lists/your-list"
direction = "source_to_destination"
deletion_policy = "additions_only"
order_authority = "source"
allow_empty = false
max_delete_fraction = 0.5
max_delete_count = 100
profile = "default"
```

Validate without credentials or network access:

```sh
python manage.py validate
python manage.py validate --config examples/public-curator.toml
python run.py --validate-only
```

## Top-level fields

| Field | Required | Contract |
| --- | --- | --- |
| `version` | Yes | Integer `1`; not a quoted string or boolean |
| `rules` | Yes | Nonempty array of `[[rules]]` tables, even if all are disabled |
| `schedules` | No | Named tables. Missing schedules imply `default` at minute 17 each hour, UTC |

Only these top-level keys are allowed. An omitted `default` table still exists with its default hourly UTC schedule, even when you define other profiles. To make **all** lists manual-only, set the default profile’s cron to `"manual"` too.

## Every rule field

| Field | Required / default | Values and effect |
| --- | --- | --- |
| `id` | Required | Unique nonempty string. Stable key for saved baseline and CLI selection; keep IDs non-sensitive |
| `source` | Required | Direct credential-free HTTPS provider list URL |
| `destination` | Required | Direct credential-free HTTPS provider list URL, distinct from source |
| `direction` | Required | `"source_to_destination"`, `"destination_to_source"`, or `"bidirectional"` |
| `deletion_policy` | Required | `"mirror"` or `"additions_only"` |
| `order_authority` | `"source"` | `"source"` or `"destination"`; selects ordering authority in bidirectional rules |
| `allow_empty` | `false` | Boolean; permits a planned deletion to empty a target, but does not bypass either deletion limit |
| `max_delete_fraction` | `0.5` | Finite number from `0` through `1`; maximum fraction of **current target membership** removable per endpoint/run |
| `max_delete_count` | `100` | Nonnegative integer; maximum removals per endpoint/run |
| `enabled` | `true` | Boolean. Disabled rules are still validated but are not selected/executed |
| `profile` | `"default"` | Existing named schedule profile; affects selection/timing, not reconciliation semantics |

The template explicitly sets `enabled = false`; the **parser default is true** if you omit it. Copy examples deliberately.

Deletion limits are inclusive: removing exactly the configured count/fraction is permitted, exceeding either stops the rule. They count actual identity removals, **not net size change**. Replacing 60 of 100 entries is 60 removals even if 60 others are added. Set either limit to zero to forbid removals. `allow_empty=true` alone is insufficient to clear a list: both limits must also permit all removals.

## Accepted list URLs

```text
https://mdblist.com/lists/USER/SLUG
https://trakt.tv/users/USER/lists/SLUG
```

User/slug components accept ASCII letters, digits, underscores, and hyphens. A single trailing slash is allowed. Case/trailing-slash variants identify the same endpoint for writer validation.

No query strings (even empty `?`), fragments, credentials, custom ports, `www` variants, escaped path separators, whitespace, watchlist URLs, or shortened/embed/API URLs are accepted. Use the direct personal-list URL. Read access depends on the provider’s API and authenticated account, not merely browser visibility.

Both URL fields can refer to either supported provider; they are endpoint labels. The ordinary cross-provider patterns are:

| Source URL | Destination URL | Direction | Writes |
| --- | --- | --- | --- |
| MDBList | Trakt | `source_to_destination` | Trakt |
| MDBList | Trakt | `destination_to_source` | MDBList |
| Trakt | MDBList | `source_to_destination` | MDBList |
| Either | Either | `bidirectional` | Both |

A read-only curator source does not require ownership. Any written Trakt endpoint must belong to the authenticated Trakt account. Any written MDBList endpoint must belong to the API-key account and be explicitly static (`dynamic=false`). Both requirements apply to both sides of a bidirectional rule.

## Directions and policies

- **One-way mirror:** the writing target adopts the authority’s ordered membership, subject to deletion safeguards.
- **One-way additions-only:** authority items come first; target-only items survive afterward in their current snapshot order. This can reorder a list even without removing anything.
- **Bidirectional additions-only:** union membership on both endpoints; no removal propagation.
- **Bidirectional mirror:** saved converged baseline distinguishes removals from additions. A new pair requires explicit bootstrap and preview. Read [the conflict and ordering contract](behavior.md) before using it.

`order_authority` does not reverse membership direction and has no operational effect on one-way ordering: that direction’s read-only side is always authoritative. Even otherwise irrelevant reconciliation-field edits can change the fingerprint, so avoid cosmetic changes to operational fields on a live mirror.

## Multiple rules, writers, and execution order

There is no artificial rule cap. Practical limits include provider quotas, available Actions minutes, workflow/schedule limits, and the worker’s execution timeout. More frequent/more numerous rules make more API calls.

Enabled rules may not write the same canonical endpoint twice—even with different profiles. A bidirectional rule owns both writer slots. Multiple rules may read one source. Feedback cycles across one-way rules are rejected; use one explicit bidirectional rule for an intentional two-list pair.

Selected rules run in dependency order: upstream writers precede readers of their targets, with stable ordering where no dependency constrains them. Selection does not automatically add an unselected upstream rule. If a rule fails, dependent rules that touch its failed writable endpoint are skipped. Independent rules can continue; global authorization/state failures stop the run.

## Schedule fields

`[schedules.NAME]` accepts only:

| Field | Default | Contract |
| --- | --- | --- |
| `cron` | `"17 * * * *"` | Standard five-field cron, or exact string `"manual"` to generate no wrapper |
| `timezone` | `"UTC"` | Valid IANA timezone name, e.g. `Europe/London`, `America/New_York`, `Asia/Tokyo` |

Names must match `^[a-z][a-z0-9-]{0,39}$` (lowercase letter first, at most 40 characters). Every rule’s profile must exist. Install pinned `tzdata` from requirements for timezone portability, particularly on Windows. See [Scheduling](scheduling.md) for cron spacing, rendering, native timezone/DST, and maintenance.

## Changes and baseline compatibility

Baseline identity is keyed by rule ID and a fingerprint of reconciliation fields. Changing `enabled`, `profile`, cron, or timezone preserves the existing reconciliation baseline. Current code also accepts compatible legacy fingerprints/encrypted state formats.

Changing endpoints, direction, policy, ordering authority, or safety limits changes reconciliation semantics. For bidirectional mirror, a mismatched baseline is rejected rather than silently reset. Changing the rule ID creates a new baseline identity; it is not a harmless rename. Do not delete/recreate state to dismiss the error. Preserve a backup and follow [recovery/change guidance](recovery.md#intentional-rule-changes).

After any edit, validate. After schedule changes, run `python manage.py render-workflows`, review and commit `schedule-*.yml`, then check drift with `--check` before pushing.