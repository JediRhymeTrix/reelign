---
title: Setup — MDBList and Trakt list sync
description: Create a private Reelign installation, connect your own Trakt app and MDBList key, configure own or public sources, initialize encrypted state, preview, and apply.
permalink: /setup/
---

# Set up your own Reelign installation

**Goal:** a private GitHub repository that reads the lists you choose and writes only to authorized destinations. No shared provider account, application, or hosted Reelign service is involved.

## Before you begin

- GitHub account with permission to create a repository and run Actions. Account/organization policies and usage charges may apply.
- Git, the **current [GitHub CLI](https://cli.github.com/)** (supporting `gh variable` and `gh secret list --json`), and Python **3.12 or newer** on a computer you control.
- Your own Trakt account and registered API application; your own MDBList account/API key.
- An accessible source movie/show list and a writable destination. Source may be your own list **or another curator’s public list**.
- Secure storage for your original encryption key and an encrypted state backup.

Use only access permitted by provider policies. An accessible URL is not permission to circumvent an access restriction. Read [security and responsible use](security.md).

## 1. Create a private repository

On [JediRhymeTrix/reelign](https://github.com/JediRhymeTrix/reelign), select **Use this template → Create a new repository**. Choose **Private** unless you deliberately want your configuration public. Unlike a public fork, a private template copy keeps your committed list URLs and rule names out of public source history. Repository administrators and authorized collaborators can still see them.

Enable Actions if prompted. Work on your repository’s default branch for operational runs. The provided Pages deployment publishes only the upstream documentation site; your copy does not need Pages.

Authenticate and clone locally; replace `OWNER/REPO` with your own repository:

Upgrade an old `gh` first: Windows `winget upgrade --id GitHub.cli`, Homebrew `brew upgrade gh`, or your platform’s [official installation instructions](https://cli.github.com/). Older CLI releases may not provide the variable/JSON options required by setup.

```sh
gh auth login
gh repo clone OWNER/REPO
cd REPO
python -m venv .venv
```

Activate the environment:

```sh
# macOS/Linux
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

If activation is restricted by local policy, use `.venv/Scripts/python.exe` directly on Windows, or `.venv/bin/python` on macOS/Linux, instead of changing system execution policy.

```sh
python -m pip install -r requirements.txt
python manage.py doctor
```

`doctor` checks local tools, environment-variable presence, and configuration. It does **not** authenticate to providers, inspect stored `gh` credentials, or prove remote permissions. Missing local environment secrets are expected if you will run through Actions.

## 2. Connect your own provider credentials

Follow [Credentials](credentials.md) to register a Trakt application and find your MDBList API key. Never borrow a shared client ID or somebody else’s tokens.

For a first installation:

```sh
python manage.py auth --repo OWNER/REPO
```

This uses hidden prompts or existing environment variables for your client ID, optional client secret, and MDBList key. It displays a short-lived Trakt device code and verification URL; open that URL, sign in to your account, and approve your own app. Tokens are not printed. `gh` installs the secrets through stdin using your authenticated GitHub permissions.

**Choose key backup before running auth.** GitHub secrets cannot be read back. For an explicit plaintext backup in addition to installation, use this command **instead**:

```sh
python manage.py auth --repo OWNER/REPO --output-secret-file secrets/backup.json
```

Create and protect the parent directory first; see [permissions and backup](credentials.md#back-up-the-original-key). The helper creates a new file only, requests POSIX mode `0600`, and warns about Windows ACLs. Put the original `SYNC_STATE_KEY` in a password manager/encrypted offline store and remove the plaintext export. No file is written unless you request one. Do not run `auth` again after installation just to obtain a backup: it refuses an existing state branch, key, or OAuth secret.

Manual installation without `gh` is available through an explicit export; see [manual secrets](credentials.md#manual-secret-installation). Never paste tokens into workflow inputs, issue reports, terminal command arguments, or committed files.

## 3. Create or choose your destination

For **MDBList → Trakt**, create a personal list in your Trakt account. A private list is a sensible default; decide separately if you want to publish it. The source can be your own MDBList list or another user’s public list.

For **Trakt → MDBList**, create an **owned static** MDBList list. Generated/dynamic MDBList lists are read-only to Reelign. For bidirectional rules, both endpoints must be writable by the configured accounts.

An optional helper can create a **new, empty private Trakt list** using the source’s name/description:

```sh
python manage.py create-destination \
  --source https://mdblist.com/lists/source-user/source-list \
  --provider trakt
```

The multiline example uses a POSIX shell; in PowerShell run it on one line. Supply current `TRAKT_CLIENT_ID` and `TRAKT_OAUTH_JSON` in the local environment, plus `MDBLIST_API_KEY` for an MDBList source. The helper does not read GitHub secrets, refresh tokens, copy items, edit config, or adopt an existing list. If the name exists, choose `--title "My new destination"`; `--description` overrides the copied description. Inspect your account after an ambiguous creation failure before retrying. You may simply create the list in the Trakt website instead.

## 4. Configure, validate, and commit

Edit the root `config.toml`. Replace placeholder URLs and give the rule a stable ID. Choose `source_to_destination` plus `additions_only` for a conservative first setup. Set `enabled = true` on the rule so the preview can select it; leave **repository variable** `REELIGN_ENABLED` absent or `false`.

These are different controls: rule `enabled` controls selection; `REELIGN_ENABLED` is the provided Actions apply/schedule gate. It does not protect local `run.py` execution.

The complete schema is in [Configuration](configuration.md). Disabled files in [examples](https://github.com/JediRhymeTrix/reelign/tree/main/examples) provide own-list, public-list, bidirectional, and profile patterns. No example URL is a working sample list.

For entirely manual onboarding, set the rule’s profile to a schedule whose `cron = "manual"`, or temporarily set `[schedules.default]` to manual. This avoids a scheduled list apply immediately after enablement.

```sh
python manage.py validate
python manage.py render-workflows
python manage.py render-workflows --check
git add config.toml .github/workflows/
git commit -m "Configure my list sync"
git push
```

Commit to your default branch before running workflows. The renderer owns only `schedule-*.yml`; do not hand-edit those wrappers.

## 5. Initialize encrypted durable state once

In your repository, open **Actions → Initialize sync state → Run workflow**, choose the default branch, and select **confirm**. The default is false to avoid accidental initialization.

This runs `python manage.py init-state --repo OWNER/REPO` with repository secrets and the job’s `GITHUB_TOKEN`. It creates an orphan `cron-state` branch containing encrypted `jobs/list-sync/state.enc`, initialized with OAuth and empty rule baselines. It does **not** modify provider lists. It refuses an existing branch; it is not a reset/recovery command.

If organization rules or branch protections block the state push, arrange narrowly scoped permission for the state branch. Operational workflows need `contents: write`; they do not need a personal access token in ordinary repository installations. Do not broadly remove protections from application branches.

Local initialization is also possible with authenticated `gh` and environment secrets `SYNC_STATE_KEY` and `TRAKT_OAUTH_JSON`:

```sh
python manage.py init-state --repo OWNER/REPO --preview
python manage.py init-state --repo OWNER/REPO
```

`--preview` checks prerequisites/absence without writing the remote branch; it is not a list preview.

## 6. Preview your intended changes

Open **Actions → Preview or sync lists → Run workflow** on the default branch:

| Input | First preview |
| --- | --- |
| `dry_run` | `true` (default) |
| `profile` | Blank for all enabled profiles, or one exact profile name |
| `rule` | Blank for all matching rules, or one exact rule ID |
| `bootstrap` | `true` only when explicitly bootstrapping a new bidirectional mirror |

Read the run’s JSON/step summary: selected rule, source/destination counts, and planned additions, removals, and reordering per writable endpoint. A dry run performs API reads and ownership checks, but no list writes, no OAuth refresh, and no baseline save. It can fail near token expiry; use normal credential maintenance, not repeated dry runs, to persist a safe token rotation.

For a new **bidirectional mirror**, bootstrap proposes the union of current items. Preview does not save that decision, so select bootstrap again for the first apply. Never use bootstrap as a way to clear or replace an existing baseline. [Behavior](behavior.md) explains deletion history and conflicts.

If the preview shows unexpected counts, stop. Check URLs, direction, policy, permissions, and source completeness before proceeding.

## 7. Explicitly enable and apply

Only after reviewing a successful preview:

```sh
gh variable set REELIGN_ENABLED --repo OWNER/REPO --body true
```

Or add repository **Actions variable** `REELIGN_ENABLED` with the exact string `true` in Settings → Secrets and variables → Actions → Variables. It is not a secret.

Run **Preview or sync lists** again with `dry_run = false` and the same profile/rule selection. If this is the first bidirectional mirror apply, select `bootstrap = true` again. Review actual lists and their custom/rank ordering after completion. Run another preview to verify membership has converged.

The provided schedule wrappers now apply selected enabled rules. The daily **Maintain Trakt authorization** workflow runs independently at **06:43 UTC**, gated by the same variable. Keep maintenance active for manual-only or infrequent list schedules.

## Pause, maintain, or stop

- Set `REELIGN_ENABLED=false` to stop provided scheduled list runs, maintenance, and non-preview manual list applies. Already-running work is not cancelled; wait for it to finish.
- Disable only the `Reelign schedule (…)` wrappers to pause list changes while allowing daily token maintenance. This is useful during an extended list-editing pause.
- Disable all operational workflows and wait for in-flight jobs before [credential recovery](recovery.md) or any local state writer.
- Keep account/API permissions and GitHub billing/usage in view. GitHub schedules can be delayed and can be disabled after public-repository inactivity; see [Scheduling](scheduling.md).

Next: [configuration](configuration.md), [behavior](behavior.md), [CLI](cli.md), and [recovery](recovery.md).