---
title: Troubleshooting
description: Safe fixes for Reelign setup, generated workflow drift, missing state, OAuth expiry, ownership checks, deletion guards, API failures, ordering, and inactive schedules.
permalink: /troubleshooting/
---

# Troubleshooting

Start with offline diagnostics:

```sh
python manage.py doctor
python manage.py validate
python manage.py render-workflows --check
```

Never enable apply to “see if it fixes” an unexpected preview. Do not delete state or rotate keys as a generic repair.

## Setup and workflow selection

| Symptom | Safe next step |
| --- | --- |
| `gh variable` or secret-list JSON options unknown | Upgrade to current GitHub CLI; old releases are unsupported. Windows: `winget upgrade --id GitHub.cli`; Homebrew: `brew upgrade gh`; otherwise [official instructions](https://cli.github.com/) |
| `gh` permission/secret install failure | Confirm `gh auth status`, intended `OWNER/REPO`, and your repository rights. Do not print stored tokens. Inspect any partial installation before retrying |
| Initial auth refuses existing key/OAuth/state | This is intentional overwrite protection. Use [state-aware recovery](recovery.md), not secret deletion followed by fresh auth |
| Hidden prompt unavailable | Use a real interactive terminal or an approved environment-secret mechanism; never replace it with visible input/logging |
| Device code expires/denied | Retry initial auth only if no installation/state was created; approve your own app/account and check current provider access requirements |
| No enabled selected rules | Replace the template URLs and set rule `enabled=true`. Check exact case-sensitive rule/profile selection. `REELIGN_ENABLED` does not enable individual rules |
| Manual workflow absent or skipped | Ensure workflows are committed, Actions enabled, and use the repository’s default branch; initialization requires `confirm=true` |
| Apply gate refuses | Review a successful preview, then set **variable** `REELIGN_ENABLED` to exact `true`. Do not put it in secrets or bypass the gate to dismiss a data error |
| Generated schedule workflows differ | Run renderer, review, commit TOML and `schedule-*.yml` together. Remove hand-written work from the renderer’s filename namespace |

## Configuration, permissions, and data

| Symptom | Safe next step |
| --- | --- |
| Invalid TOML/unknown field | Check [all fields/types](configuration.md), top-level `version=1`, and a nonempty `[[rules]]` array; disabled rules still validate |
| Unknown timezone | Install requirements including `tzdata`; use an IANA name, not an abbreviation. `UTC` always works |
| Invalid cron/minute spacing | Use standard five-field cron or exact `manual`; selections must have five-minute spacing including hour wraparound |
| Invalid list URL | Use direct `https://mdblist.com/lists/USER/SLUG` or `https://trakt.tv/users/USER/lists/SLUG` without query/fragment |
| Multiple writers/cycle | One enabled writer per endpoint across all profiles; use one bidirectional rule for an intentional pair, not two opposing one-way rules |
| Destination not owned / MDBList not static | Authenticate the actual owner, choose an owned writable target, and use an explicitly static MDBList destination. A public source can be read-only; a target cannot |
| API 401/403 | Stop; confirm current app/account permissions, key, active encrypted OAuth, and provider restrictions. Do not rotate identities to evade denial |
| API 404/private source | Verify direct URL and API read permission. A browser session’s visibility does not prove API access |
| Unsupported item/duplicate/conflicting IDs | Inspect source data and provider IDs. Movies/shows only; ambiguous or partial data is deliberately not synced |
| Pagination/count/metadata changed | Let remote edits settle, inspect source completeness, then run a fresh preview. Never treat a failed snapshot as an empty list |
| Addition verification or partial write failed | Some changes may already exist. Inspect actual lists and rerun preview before another apply; baseline did not advance for the failed rule |
| Refusing to empty / delete threshold exceeded | Confirm intended source/policy/removals before consciously adjusting limits. `allow_empty` does not bypass count/fraction safeguards |
| Bidirectional mirror requires baseline/bootstrap | For a truly new rule, bootstrap-preview then explicit bootstrap-apply. Never use bootstrap to overwrite corrupt/mismatched existing history |
| Fingerprint mismatch | Inspect semantic rule changes. Scheduling/enabled edits preserve baseline; operational-field/ID changes may not. Follow [intentional changes](recovery.md#intentional-rule-changes) |

## State and OAuth

- **Missing/corrupt/wrong key:** verify branch/path/original key and preserve existing backups. Do not regenerate a key or reinitialize a live branch.
- **OAuth expired/near expiry in dry run:** previews never refresh. Run **Maintain Trakt authorization** normally if refresh is still valid and persistence is healthy; otherwise recover/reauthorize with the original key.
- **Dirty/stale state checkout:** stop writers; fetch and use a clean/current checkout. Do not force-push or discard a newer encrypted file containing a rotated token.
- **Push failure or refresh timeout:** stop immediately and preserve the encrypted failure artifact/local file. An old refresh token may be consumed. [Recovery](recovery.md) explains safe ordering.
- **Updated OAuth secret but nothing changed:** established runtime uses encrypted state, not the initial GitHub seed. Use `update-auth` to replace OAuth in existing state.

## Schedules, order, and quotas

- Schedules run from the default branch and require exact `REELIGN_ENABLED=true`. Profile wrappers do not exist for `cron="manual"`.
- GitHub schedules are best-effort, and public repositories can have scheduled workflows disabled after 60 days without activity. Check Actions status, account usage, and the next maintenance run.
- Multiple scheduled profiles share a concurrency group; it is not a guaranteed FIFO queue. A long-running backlog is a reason to reduce frequency/profiles.
- **Maintenance failing while list jobs are manual?** Maintenance still validates config/render drift and needs healthy state/app credentials. Fix it before token expiry.
- **List order differs from a website’s selected sort?** Reelign writes custom rank from newest-added snapshots, not arbitrary source display sorting. Trakt’s default becomes rank ascending. See [Behavior](behavior.md).
- **429 or long retry delay:** reduce frequency/rules, honor provider reset windows, and inspect account quotas. A no-change run still makes snapshot and verification requests; it is not API-free. Do not multiply keys/accounts to evade limits.

## Ask for help safely

Use the [issue templates](https://github.com/JediRhymeTrix/reelign/issues/new/choose) with Python/OS/CLI versions, upstream commit, command/flags, sanitized error, and a minimal config using fake URLs. Omit tokens, private URLs, device codes, exported secrets, decrypted state, ciphertext/key pairs, and full logs that have not been reviewed. Report suspected vulnerabilities privately via [SECURITY.md](https://github.com/JediRhymeTrix/reelign/blob/main/SECURITY.md).