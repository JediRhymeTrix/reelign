---
title: Scheduling and credential maintenance
description: Configure native GitHub Actions cron/timezone profiles, manual-only rules, rendered workflow drift checks, serialized state writes, and independent OAuth maintenance.
permalink: /scheduling/
---

# Scheduling and credential maintenance

Reelign uses **native GitHub Actions schedule events**, not a server, polling daemon, or custom cron runner. TOML is the source of truth; a renderer produces static wrappers that call the shared sync worker.

## Default and named profiles

```toml
[schedules.default]
cron = "17 * * * *"
timezone = "UTC"

[schedules.morning]
cron = "23 8 * * *"
timezone = "America/New_York"

[schedules.weekend]
cron = "41 10 * * SAT,SUN"
timezone = "Europe/London"

[schedules.on-demand]
cron = "manual"
timezone = "UTC"
```

Assign a rule with `profile = "morning"`. An omitted profile means `default`. Names must match `^[a-z][a-z0-9-]{0,39}$`; referenced profiles must exist. If no schedules are declared, `default` is **minute 17 every hour, UTC**. Defining another named schedule does not remove that default.

To make all lists manual-only, explicitly set `[schedules.default] cron = "manual"` and ensure every other assigned profile is manual too. A manual profile generates **no** schedule wrapper; it remains selectable through the manual workflow or `run.py --profile NAME`.

## Cron validation

Cron has exactly five fields: **minute hour day-of-month month day-of-week**. Supported syntax is numeric values, `*`, comma-separated selections, ascending ranges, and positive `/` steps. Month/day names such as `JAN`, `MON`, and `SAT` are supported. Day of week accepts `0`–`6`, with Sunday represented by `0` or `SUN`; `7` is not accepted.

No seconds/year field, `@hourly`, `?`, `L`, or `#` extensions. Selected minutes must be at least **five minutes apart**, including the wrap from the last minute of one hour to the first of the next. The validator checks the minute field conservatively even if the hour/day fields are sparse.

| Cron | Result |
| --- | --- |
| `17 * * * *` | Hourly at :17 |
| `*/15 * * * *` | Four times an hour; assess API/Actions usage first |
| `23 8 * * MON-FRI` | Weekdays at 08:23 in the configured timezone |
| `41 10 * * SAT,SUN` | Weekend mornings |
| `manual` | No scheduled wrapper |
| `* * * * *` | Rejected: less than five-minute spacing |
| `0,58 * * * *` | Rejected: two-minute wraparound gap |

## Timezones and daylight saving

`timezone` is an IANA name, not a fixed locale abbreviation. Use `UTC`, `Europe/London`, `America/New_York`, or another valid zone. Install requirements (including `tzdata`) so validation works consistently on Windows and systems without zoneinfo data.

The renderer passes the timezone to GitHub’s native schedule event. It does not translate a local cron into a fixed UTC cron, so daylight-saving handling belongs to GitHub’s scheduler. If a run must be independent of seasonal clock changes, choose UTC. Avoid relying on nonexistent/repeated local wall-clock times for critical operations, and consult [GitHub’s current schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule) for platform behavior.

## Render and commit whenever schedules change

```sh
python manage.py validate
python manage.py render-workflows
python manage.py render-workflows --check
git diff -- .github/workflows/
git add config.toml .github/workflows/
git commit -m "Update list schedules"
git push
```

The renderer owns only `.github/workflows/schedule-*.yml`. Each non-manual profile generates `schedule-NAME.yml`, including profiles currently used by no enabled rules. Stale wrappers are removed when profiles become manual or are deleted. Other workflow filenames are untouched. Do not store hand-written workflows under `schedule-*.yml`.

`--config PATH` and `--output-dir DIR` support alternate configuration/output locations. `--check` compares expected bytes and wrapper filenames without writing; stale, missing, or edited wrappers fail the check. CI and the worker check drift, so commit regenerated files alongside the TOML change.

Changing profiles, timing, or enabled status does **not** reset rule fingerprints or deletion baselines.

## Manual preview and selection

**Preview or sync lists** (`sync.yml`) has `dry_run` (default true), `profile`, `rule`, and `bootstrap` inputs. Blank profile/rule selects all enabled rules. Supplying both intersects the selections. The UI accepts one rule ID; local `run.py --rule ID` is repeatable.

Manual runs are not confined to the selected profile’s scheduled time. Profile filtering is selection, not an interval limiter. Disabled rules never execute. Use bootstrap explicitly for the first preview **and first apply** of a new bidirectional mirror; a preview saves no baseline.

## Enablement, concurrency, and practical limits

- Provided schedule wrappers and daily maintenance require repository variable `REELIGN_ENABLED` to equal the exact string `true`.
- Manual non-dry list apply also checks that variable. Preview remains available without it. Local CLI execution is not gated by the variable.
- Operational manual workflows run from the default branch; schedules are a default-branch mechanism. Commit configuration there before expecting it to take effect.
- The shared worker and initializer use concurrency group `reelign-state` with `cancel-in-progress: false`, preventing simultaneous Actions state writers in one repository. GitHub concurrency is **not a durable FIFO queue**; pending runs can be replaced under platform rules. Local processes or other repositories are not covered.
- There is no artificial profile/rule cap in Reelign. Each scheduled profile creates a workflow. GitHub’s practical workflow, scheduling, concurrency, usage, and execution limits still apply. The provided worker has a 30-minute timeout. Avoid proliferating near-identical frequent profiles.
- GitHub schedules are best-effort: runs can be delayed or dropped during load, particularly around the start of the hour. Off-hour minutes reduce contention but do not guarantee punctuality.
- In public repositories, scheduled workflows may be disabled after **60 days without repository activity**. Check Actions status/notifications rather than assuming encrypted state commits or any particular activity pattern will keep schedules alive. Re-enable affected workflows deliberately.

See [GitHub Actions limits](https://docs.github.com/en/actions/reference/limits) and your account’s usage/billing. Reelign does not promise unlimited or free operation.

## Independent credential maintenance

**Maintain Trakt authorization** (`maintenance.yml`) is scheduled daily at **06:43 UTC**, independent of list profiles. It calls the shared worker with `--maintenance-only`: load/decrypt state, validate/refresh OAuth if due, persist rotation, and exit **without fetching lists**.

Maintenance is still gated by `REELIGN_ENABLED=true` and requires initialized, writable state plus the original key and app credentials. The workflow validates configuration/render drift before execution, so a malformed config can also stop maintenance. Watch for failures even when lists rarely change.

Trakt access tokens commonly have a seven-day lifetime; response metadata is authoritative. A normal run rotates when expiry is within a day; dry runs never rotate. A manual-only or monthly list schedule is **not** enough reason to disable daily maintenance. For a long list pause, disable the profile wrappers but leave maintenance active. For credential recovery, disable **all** state-writing workflows and wait for in-flight work.

Next: [behavior](behavior.md), [recovery](recovery.md), and [troubleshooting](troubleshooting.md).