---
title: CLI reference
description: Exact setup and runtime commands for validation, device authorization, encrypted state initialization, workflow rendering, destination creation, selective previews, and recovery.
permalink: /cli/
---

# CLI reference

Run from your checkout with Python **3.12+** and requirements installed. `python manage.py --help`, subcommand `--help`, and `python run.py --help` show the installed version’s parser. Setup commands return `0` on success, `1` on safe reported failure; interrupting setup returns `130`. Argparse usage errors return `2`. Runtime returns `1` for aggregate rule failure or global failure and `0` for success/no selected enabled rules.

No command takes secret values as command-line options. Use hidden prompts where supported or your process environment. No workflow input should contain secrets.

## Setup commands: `manage.py`

### `validate [--config PATH]`

```sh
python manage.py validate
python manage.py validate --config examples/own-bidirectional.toml
```

Strictly loads TOML including schedules, URLs, types, writer conflicts, and cycles. No credentials, network requests, or mutations. Default config is `config.toml` beside `manage.py`, regardless of current working directory.

### `doctor [--config PATH]`

Local-only diagnostics: Python version, `git`/`gh` availability, credential-variable **presence**, and config validation. No secret values, provider requests, stored `gh` credential reads, or remote permission proof. Reports an optional missing Trakt client secret without requiring it.

### `auth [--repo OWNER/REPO] [--output-secret-file PATH] [--reauthorize]`

```sh
python manage.py auth --repo OWNER/REPO
python manage.py auth --repo OWNER/REPO --output-secret-file secrets/backup.json
python manage.py auth --output-secret-file secrets/initial.json
```

Choose **one** initial authorization form. Reads/prompts for `TRAKT_CLIENT_ID`, optional `TRAKT_CLIENT_SECRET`, and `MDBLIST_API_KEY`; performs device authorization and generates `SYNC_STATE_KEY`. With `--repo`, requires authenticated `gh` and installs secrets through stdin. Refuses existing `cron-state`, `SYNC_STATE_KEY`, or `TRAKT_OAUTH_JSON` in the target repository. Repository must be `OWNER/REPO`, not a URL/path.

Explicit `--output-secret-file` opts into a **plaintext** export/backup; the parent must already exist privately and the file must not exist. File creation is exclusive with POSIX mode `0600`; Windows ACLs require separate care. With no repository, an explicit file is required before authorization can proceed. Combined repository+file writes the backup before installation. No file is created by default.

Recovery only:

```sh
python manage.py auth --reauthorize --output-secret-file secrets/recovery.json
```

`--reauthorize` requires an export file and **cannot** use `--repo`. It emits fresh client/OAuth values only—**no new key or MDBList key**. Then use `update-auth` with the original encryption key. Full [recovery procedure](recovery.md).

### `init-state --repo OWNER/REPO [--preview]`

Requires local `TRAKT_OAUTH_JSON`, original/generated `SYNC_STATE_KEY`, and authenticated `gh`. Validates the token/key and absent remote `cron-state`; preview stops before remote writes. Normal execution creates encrypted `jobs/list-sync/state.enc` in a new orphan state branch. No provider list operations. Refuses existing state; never use it to reset a live installation. Prefer the provided **Initialize sync state** workflow after secret installation.

### `update-auth --state PATH [--state-repo DIR] [--preview]`

```sh
python manage.py update-auth --state state-checkout/jobs/list-sync/state.enc --state-repo state-checkout --preview
python manage.py update-auth --state state-checkout/jobs/list-sync/state.enc --state-repo state-checkout
```

Requires **original** `SYNC_STATE_KEY` and fresh `TRAKT_OAUTH_JSON` in the environment. Loads/decrypts existing state, preserves rules/baselines, and replaces OAuth only. No initialization on missing/corrupt/wrong-key state. With a state repository, the file must be inside it; apply preflights current/clean/writable `cron-state` and commits/pushes via StateStore. Preview decrypts and describes the change without saving, but does not prove remote write permissions. Pause other writers first.

### `render-workflows [--config PATH] [--output-dir DIR] [--check]`

Defaults: project `config.toml`, project `.github/workflows`. Writes deterministic `schedule-PROFILE.yml` for non-manual profiles and removes obsolete `schedule-*.yml`. Other names are unchanged; non-file/symlink schedule wrappers are refused. `--check` writes nothing and fails on drift. [Scheduling](scheduling.md).

### `create-destination --source URL --provider trakt [--title TEXT] [--description TEXT]`

Explicit mutation: creates one **new, empty private Trakt list**, with comments/display numbers disabled and rank ascending. Uses source name/description unless overridden; prints the new URL. No item copy, config write, adoption, or token refresh. Current local `TRAKT_OAUTH_JSON` and `TRAKT_CLIENT_ID` are required; an MDBList source also needs `MDBLIST_API_KEY`. Rejects expired/near-expiry OAuth, malformed metadata, incomplete name enumeration, and case-insensitive name collisions. On ambiguous POST failure, inspect your account before retrying. Only provider `trakt` is supported.

## Runtime: `run.py`

| Flag | Default / behavior |
| --- | --- |
| `--config PATH` | Project-root `config.toml` |
| `--state PATH` | Project-root `.state/state.enc` |
| `--state-repo DIR` | Optional Git checkout containing the state; enables persistence preflight and commit/push |
| `--dry-run` | Read/plan/preflight only; no OAuth refresh, list writes, or baseline saves |
| `--rule ID` | Repeatable exact rule selection; unknown IDs fail |
| `--profile NAME` | Select enabled rules in one profile; combines with rule filter as intersection; a profile with no rules fails |
| `--maintenance-only` | Load/refresh credentials without fetching lists; still validates config and requires state/app credentials |
| `--validate-only` | Offline config validation, returns before secrets or network |
| `--bootstrap-state` | Explicit initial state/bidirectional mirror bootstrap; never overwrites existing corrupt or mismatched state |

No enabled selected rules exits successfully before secrets/state access, except maintenance explicitly continues. Disabled rules are never executed, even when selected by ID. Configuration is always validated.

For actual list runs, provide `SYNC_STATE_KEY`, `TRAKT_CLIENT_ID`, `MDBLIST_API_KEY`, and optional `TRAKT_CLIENT_SECRET`. `TRAKT_OAUTH_JSON` is needed when explicitly bootstrapping absent local state; established state holds the active OAuth pair. Maintenance does not need the MDBList key because it reads no lists.

### Safe local preview against current Git-backed state

Pause operational Actions and wait for existing jobs before cloning/using state:

```sh
gh repo clone OWNER/REPO state-checkout -- --branch cron-state --single-branch
python run.py --state state-checkout/jobs/list-sync/state.enc --state-repo state-checkout --dry-run --rule my-first-list
```

Set secrets through your approved environment/secret-manager mechanism. GitHub secrets cannot be read back. A dry run does not require a refresh token mutation, but needs a valid access token in state and provider read access.

**Local apply is not gated by `REELIGN_ENABLED`.** Omitting `--dry-run` permits writes immediately. Never run multiple state writers or work from an old downloaded state while Actions is active. For a first bidirectional mirror add `--bootstrap-state` to preview and first apply; not as a general repair flag.

The manual Actions workflow takes only one rule input; for multiple explicit local rules, repeat the flag:

```sh
python run.py --dry-run --rule first-list --rule second-list --profile weekend
```

Without explicit state flags that example uses local `.state/state.enc`, **not** the GitHub state branch. Always choose the correct state location for your installation. See [recovery](recovery.md) and [security](security.md).