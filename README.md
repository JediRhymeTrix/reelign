# <img src="docs/assets/favicon.svg" alt="Reelign logo" width="40" height="40"> Reelign — MDBList ↔ Trakt List Sync

**Your lists. Perfectly aligned.**

[![Tests](https://github.com/JediRhymeTrix/reelign/actions/workflows/ci.yml/badge.svg)](https://github.com/JediRhymeTrix/reelign/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-65e5c5)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/JediRhymeTrix/reelign)](https://github.com/JediRhymeTrix/reelign/releases)

**[Use this template →](https://github.com/JediRhymeTrix/reelign/generate)** · Create a private copy, add your own secrets, preview, then enable.

Sync movie and TV show lists between MDBList and Trakt, from your own GitHub repository. Keep your own lists aligned, or follow another curator’s publicly readable list into a destination you control. Both are first-class use cases.

[Get started](docs/setup.md) · [Documentation](https://jedirhymetrix.github.io/reelign/) · [Configuration](docs/configuration.md) · [Recovery](docs/recovery.md) · [MIT license](LICENSE)

## Your watchlist didn’t ask to be part of the breakup.

When MDBList and Trakt stop talking, your carefully curated lists shouldn’t become collateral damage. Reelign handles synchronization from your own GitHub repository, using credentials you control.

Less platform drama. More movies.

## What Reelign does

- **MDBList to Trakt, Trakt to MDBList, or both ways.** Choose a direction per rule.
- **Your lists and public curator lists.** Read your own accessible lists or someone else’s public list; write only to lists your authenticated accounts can modify.
- **Credit where it belongs.** Synced Trakt descriptions link the original list and creator, plus a compact Reelign setup link, without replacing your own description. [Formatting and platform limits](docs/behavior.md#source-credit-and-destination-descriptions).
- **Mirror or additions-only.** Propagate removals deliberately, or keep items already collected at the destination.
- **Ordered movie/show synchronization.** Match IMDb, TMDB, Trakt, and TVDB identifiers without title guessing; verify membership and custom rank after writes.
- **Multiple rules and schedule profiles.** Generate native GitHub Actions schedules with cron and IANA timezones, or run manually.
- **Preview before applying.** No list, OAuth, or baseline writes during a dry run. Explicit bootstrap is required for a new bidirectional mirror.
- **Guarded removals.** Empty-list protection, deletion count/fraction limits, complete-snapshot checks, and additions verified before removals.
- **Encrypted durable state.** AES-256-GCM protects rotating OAuth credentials and reconciliation baselines on `cron-state`. A separate daily maintenance job keeps authorization independent of list schedules.
- **No hosted Reelign service.** You run the code and supply your own application credentials. There is no shared Trakt application or credential pool.

There is no artificial rule-count cap, but API quotas, list/account permissions, GitHub Actions limits, and runner time still apply. One installation uses one authenticated account per provider. Only movie and show entries are supported—not episodes, seasons, watch history, ratings, or a general account backup.

## How to sync MDBList to a Trakt list

1. Choose an accessible MDBList source: one of **your own lists** or **another curator’s public list**.
2. Create a Trakt personal list in your own account. For a read-only curator source, use one-way sync; you do not need to own the source.
3. Configure `source_to_destination` and start with `additions_only`.
4. Install your own credentials, initialize encrypted state, and preview.
5. Review the counts, then explicitly enable and apply.

Use an **owned static MDBList list** when MDBList is the write target. Dynamic/generated MDBList lists are not writable destinations. Bidirectional sync requires write permission on **both** endpoints; it is not a way to edit another curator’s list.

## Quick start: your repository, your credentials

See the [complete setup guide](docs/setup.md) for Windows commands, permissions, optional list creation, manual secret installation, and recovery precautions.

### 1. Create a private copy

Choose **Use this template → Create a new repository** on [JediRhymeTrix/reelign](https://github.com/JediRhymeTrix/reelign). A **private repository is recommended**. A public fork exposes committed list URLs, rule names, workflow logs, and encrypted-state history; encryption does not hide your configuration.

Enable Actions in your copy. Install Git, the **current [GitHub CLI](https://cli.github.com/)** (with `gh variable` and `gh secret list --json` support), and Python **3.12+**, then run locally:

```sh
gh auth login
gh repo clone OWNER/REPO
cd REPO
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# PowerShell:  .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py doctor
```

Replace `OWNER/REPO` with **your** repository everywhere below. Ordinary Actions runs use GitHub’s repository-scoped `GITHUB_TOKEN`; you do not need a personal access token for them.

### 2. Register your own credentials and authorize

Create your own [Trakt API application](https://trakt.tv/oauth/applications), and obtain your own [MDBList API key](https://mdblist.com/preferences/). Follow [the credential guide](docs/credentials.md) for app registration and device authorization. Modern Trakt applications may not have a client secret; leave that optional prompt blank when appropriate.

```sh
python manage.py auth --repo OWNER/REPO
```

The helper reads environment variables or hidden prompts for `TRAKT_CLIENT_ID`, optional `TRAKT_CLIENT_SECRET`, and `MDBLIST_API_KEY`. Open the displayed Trakt verification URL and approve your own application. The helper installs secrets through authenticated `gh` **stdin**, not command-line secret arguments, and generates a fresh encryption key. Never enter credentials into workflow inputs.

**Back up the original `SYNC_STATE_KEY`.** GitHub secrets cannot be read back. If you need an export for an encrypted password manager/offline backup, opt in **on this initial authorization command**, not by rerunning it afterward:

```sh
# Create/protect the secrets directory first; see the credential guide for ACLs.
python manage.py auth --repo OWNER/REPO --output-secret-file secrets/backup.json
```

That file contains **plaintext credentials**. Restrict permissions/Windows ACLs, move the key into secure storage, and remove the export when finished. No plaintext file is written by default. Do not run both authorization examples in sequence. Established installations must use [state-aware recovery](docs/recovery.md), not a new key or another initialization.

### 3. Configure your lists

Edit `config.toml`. Replace **both** placeholder URLs, choose your policy, and set your rule’s `enabled = true`. Keep the repository variable `REELIGN_ENABLED` absent or `false` for now.

```toml
version = 1

[schedules.default]
cron = "17 * * * *"
timezone = "UTC"

[[rules]]
id = "my-first-list"
enabled = true
source = "https://mdblist.com/lists/your-user/your-list"
destination = "https://trakt.tv/users/your-user/lists/your-list"
direction = "source_to_destination"
deletion_policy = "additions_only"
profile = "default"
```

The example URLs are syntactically valid placeholders, not working sample lists. The shipped configuration is disabled. See [all fields and defaults](docs/configuration.md) and [disabled example configurations](examples/).

```sh
python manage.py validate
python manage.py render-workflows
python manage.py render-workflows --check
git add config.toml .github/workflows/
git commit -m "Configure my list sync"
git push
```

Regenerate and commit schedule wrappers whenever schedule configuration changes; CI checks for drift.

### 4. Initialize state, then preview

In **your repository → Actions → Initialize sync state → Run workflow**, use the default branch and select `confirm`. This creates the absent `cron-state` branch with `jobs/list-sync/state.enc`; it does **not** change lists and refuses to overwrite existing state.

Next run **Preview or sync lists** (`sync.yml`), leaving `dry_run = true`. Optionally select a `rule` or `profile`; blank means all matching enabled rules. Inspect the run summary’s addition, removal, and reorder counts.

For a brand-new **bidirectional mirror**, select `bootstrap = true` for this preview. Preview does not save a baseline, so the first apply must also explicitly select bootstrap. Bootstrap unions current membership; it never resets an existing baseline.

### 5. Enable and apply deliberately

After a successful, understood preview:

```sh
gh variable set REELIGN_ENABLED --repo OWNER/REPO --body true
```

Run **Preview or sync lists** again with `dry_run = false` and the same selection. For an initial bidirectional mirror, also select `bootstrap = true`. Use a manual-only profile during onboarding if you want to prevent an automatic list run between enablement and the first apply. Subsequent runs should not need bootstrap.

Scheduled profiles and daily credential maintenance can now run. Keep maintenance enabled even when your list profiles are manual-only or infrequent. GitHub schedules are best-effort, not an exact-time service; see [scheduling](docs/scheduling.md).

## Trakt to MDBList sync and bidirectional behavior

Set `direction = "destination_to_source"` to read the configured destination and write the configured source, or use `bidirectional` when you own both writable endpoints. `source` and `destination` are fixed labels, not automatically inferred from providers.

With `mirror`, one-way sync removes target entries absent from its authority. Bidirectional mirror uses the saved baseline to distinguish removals from new additions. With `additions_only`, extra items survive, but order may still change. Source snapshots are newest-added-first where complete timestamps exist; this is not an arbitrary source-view sort copier. [Read the exact ordering, conflict, and safety contract](docs/behavior.md) before enabling mirror.

## Commands at a glance

```sh
python manage.py validate
python manage.py doctor
python manage.py render-workflows --check
python manage.py init-state --repo OWNER/REPO --preview
python run.py --validate-only
python run.py --dry-run --rule my-first-list --rule another-list
python run.py --dry-run --profile default
python run.py --maintenance-only
```

Networked local runs need environment secrets and the **current encrypted state**. `run.py` defaults to `.state/state.enc`; Actions uses the checked-out state branch instead. Local apply is **not** gated by `REELIGN_ENABLED`. Do not run it in parallel with Actions or against stale state. [Full CLI reference](docs/cli.md).

## Documentation

| Guide | What you will find |
| --- | --- |
| [Setup](docs/setup.md) | Private template, own/public sources, preview, first apply |
| [Credentials](docs/credentials.md) | Own Trakt app, device auth, exact secrets, key backup |
| [Configuration](docs/configuration.md) | Every TOML field, URLs, ownership, validation |
| [Scheduling](docs/scheduling.md) | Cron, timezones, profiles, renderer, maintenance |
| [Sync behavior](docs/behavior.md) | Identity, ordering, mirror, deletion protections, failures |
| [CLI reference](docs/cli.md) | Setup and runtime commands, flags, local state |
| [Security](docs/security.md) | Trust boundaries, encryption, permissions, responsible use |
| [Recovery](docs/recovery.md) | Rotating-token failures, original-key recovery, reauthorization |
| [Troubleshooting](docs/troubleshooting.md) | Common errors and safe next steps |
| [Upgrading](docs/upgrading.md) | Template updates and allowlisted private-job backports |
| [Contributing](CONTRIBUTING.md) | Offline tests, docs/site checks, contribution boundaries |

## Responsible use and non-affiliation

Reelign is an independent, unofficial project, not affiliated with or sponsored by Trakt or MDBList. **Reelign uses Trakt but is not endorsed or certified by Trakt.** Provider names and marks belong to their respective owners.

Use only API access and content you are authorized to use, and follow provider terms, application requirements, permissions, and rate limits. An independent, user-operated tool is **not an automatic exception** to a provider policy or restriction. Reelign does not provide policy-compliance guarantees or mechanisms to evade restrictions through shared credentials, rotating accounts/applications, or scraping blocked data. If access is denied, stop and resolve authorization with the provider.

## License and support

[MIT](LICENSE) © 2026 Vedant / JediRhymeTrix. Provided without warranty; keep your own list and key backups. File a sanitized [bug report](https://github.com/JediRhymeTrix/reelign/issues/new/choose), read [release notes](CHANGELOG.md), or follow [SECURITY.md](SECURITY.md) for private vulnerability reporting. Never attach credentials, decrypted state, or private list URLs to a public issue.