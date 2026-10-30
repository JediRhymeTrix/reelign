---
title: Credentials and Trakt device authorization
description: Register your own Trakt application, obtain an MDBList API key, authorize with the local device helper, install GitHub secrets safely, and back up the original state key.
permalink: /credentials/
---

# Credentials: your app, your account, your key

Reelign has no shared provider application or central token service. Each installation uses one Trakt account/application and one MDBList account key. Your GitHub repository stores its own Actions secrets; encrypted state holds the active OAuth pair after refresh.

## Register a Trakt API application

1. Sign in to **your** Trakt account and open [Your API applications](https://trakt.tv/oauth/applications).
2. Create an application with an honest name/description, such as your personal Reelign installation. Use a website/project URL appropriate to your installation. Follow the current registration form and Trakt application requirements; do not imply Trakt endorsement.
3. For the out-of-band/native redirect field, use `urn:ietf:wg:oauth:2.0:oob` where supported by the registration form. Reelign’s refresh request uses that redirect URI. Initial authorization itself uses Trakt’s device-code flow, not a hosted callback.
4. Save your application’s **client ID**. If your application has a **client secret**, save it too. Modern applications may not issue one; Reelign treats the secret as optional and omits it when absent.

Do not reuse a third-party integration’s client ID or tokens. Application/account limits and permissions are determined by Trakt. If registration or authorization is denied, resolve it with Trakt instead of switching identities to evade the restriction.

## Obtain your MDBList API key

Sign in to your MDBList account and locate the API key in [Preferences](https://mdblist.com/preferences/). Use your own key, even for publicly readable sources. Quotas depend on your provider account; choose a practical sync interval. Never append the key to a committed list URL.

## Authorize locally and install repository secrets

Use the current GitHub CLI, with `gh variable` and `gh secret list --json` support; upgrade older installations using the [official instructions](https://cli.github.com/). Authenticate using `gh auth login`, with access to administer secrets in your target repository. Then run:

```sh
python manage.py auth --repo OWNER/REPO
```

The helper reads `TRAKT_CLIENT_ID`, optional `TRAKT_CLIENT_SECRET`, and `MDBLIST_API_KEY` from the environment if set; otherwise it uses hidden prompts in an interactive terminal. Hidden prompting fails closed if terminal echo cannot be disabled. Noninteractive environments must provide the required variables. Input values are not logged.

The helper requests a device code, displays a verification URL and short-lived user code, and polls at the provider’s interval. Open that URL yourself and approve your app for the intended Trakt account. Treat the displayed device code as sensitive while active. Cancellation, expiry, denial, and ambiguous token failures do not trigger an automatic refresh/replay.

On success it generates a random encryption key and sends secret values to `gh secret set` on **stdin**. Values are not command-line arguments or implicit files. It checks for existing state/credentials and refuses to replace an established installation. Do not run concurrent setup commands.

## Exact Actions secret names

In your repository: **Settings → Secrets and variables → Actions → Repository secrets**.

| Secret | Required? | Meaning |
| --- | --- | --- |
| `MDBLIST_API_KEY` | Yes for list sync | Your MDBList account API key |
| `TRAKT_CLIENT_ID` | Yes | Client ID of your own Trakt app |
| `TRAKT_CLIENT_SECRET` | Only if your app has one | App secret; omit for an app with none |
| `TRAKT_OAUTH_JSON` | Initial setup/recovery | Complete OAuth token JSON as a **string**, including `access_token`, `refresh_token`, positive integer `expires_in`, and integer `created_at` |
| `SYNC_STATE_KEY` | Yes | Base64-encoded 32 random bytes for AES-256-GCM; preserve the original |

The initial helper accepts/persists provider token metadata and supplies `created_at` if needed. Do not hand-calculate token values or use a placeholder token JSON. Do not wrap the whole JSON string in another JSON object when setting the secret.

`TRAKT_OAUTH_JSON` is the initialization seed, **not the current token source after rotation**. The encrypted state becomes authoritative. Refreshes do not rewrite GitHub secrets. Replacing that secret alone will not repair an established installation.

`REELIGN_ENABLED` is an Actions **variable**, not a secret. Its exact value `true` enables provided apply/schedule paths. Never use workflow inputs for any secret.

## Back up the original key

GitHub secret values cannot be read back. Losing `SYNC_STATE_KEY` prevents decryption of the stored OAuth pair **and** the deletion baselines. A fresh key cannot decrypt old ciphertext. Keep the original key in an encrypted password manager or offline encrypted backup, separately from an encrypted-state backup.

If you want a backup export, request it on the **initial** auth command:

```sh
python manage.py auth --repo OWNER/REPO --output-secret-file secrets/backup.json
```

The export is written durably **before** repository secrets are installed. It includes all initial secrets and is **plaintext**. It creates a new file exclusively, refuses overwrite/symlinks, and requests mode `0600` on POSIX. There is no default plaintext output. `secrets/` is ignored by Git, but ignore rules do not encrypt files or make them safe to share.

Create a private parent directory before the command. For POSIX:

```sh
mkdir -p secrets
chmod 700 secrets
```

On Windows, `chmod` does not enforce restrictive NTFS ACLs. Use a local location accessible only to your account; inspect/restrict its ACLs before exporting (folder Properties → Security or your organization’s approved ACL tooling). Avoid shared/network or automatically shared cloud folders. Move the original key to encrypted storage and remove the plaintext file when no longer needed. File deletion is not guaranteed forensic erasure on all storage; full-disk encryption helps limit residual exposure.

Never run initial `auth` a second time to make a backup. The helper intentionally refuses established state or secrets. For a live installation, the initial exported OAuth token may become stale after rotation; keep **current encrypted state**, not only the initial export, for recovery.

## Manual secret installation

If you cannot use `gh` for installation, explicitly export first:

```sh
python manage.py auth --output-secret-file secrets/initial.json
```

Without `--repo`, an explicit export path is mandatory; the helper refuses before authorization if neither is supplied. Create the protected parent directory first. In your repository’s Actions secrets UI, create each named secret using the **value** from the export. `TRAKT_OAUTH_JSON` must be the decoded JSON string containing the token object, not the export object or an escaped representation copied blindly.

If you have `gh` and an export, this local Python snippet uploads values by stdin without exposing them as arguments (run from the repository root, edit only repository/path placeholders):

```python
import json
import subprocess
from pathlib import Path

values = json.loads(Path("secrets/initial.json").read_text(encoding="utf-8"))
for name, value in values.items():
    subprocess.run(
        ["gh", "secret", "set", name, "--repo", "OWNER/REPO"],
        input=value, text=True, check=True,
    )
```

Back up the key before removing the export. Initialize state using the **Initialize sync state** workflow. Local `init-state` instead requires the key/token in your process environment; installed GitHub secrets cannot be fetched back with `gh`.

## Refresh and credential lifetime

Trakt’s current OAuth model uses short-lived access tokens (commonly seven days) and rotating, single-use refresh tokens. Reelign uses the response’s `created_at` and `expires_in`, not a hardcoded lifetime. When an access token has at most one day remaining, a non-dry run checks state persistence, refreshes once, and durably saves the replacement pair **before** reading/syncing lists.

A daily maintenance workflow keeps rotation independent of list frequency. Dry runs never refresh or persist tokens and fail when expired/too close to expiry. Do not replay an old refresh token or restore an old seed over current state. A timeout after a refresh request may mean the old refresh token has already been consumed; [recover state first](recovery.md).

## GitHub permissions and revocation

- Local `gh` setup requires repository secret-management and Git-reference write rights. `doctor` does not verify these remotely.
- Provided operational Actions jobs request `contents: write` to persist `cron-state`; normal runs use the automatically issued repository `GITHUB_TOKEN`, not a PAT.
- Branch/ruleset restrictions may block persistence. Fix the state branch’s narrowly scoped permissions before consuming a refresh token.
- To stop an installation, disable operational workflows, wait for active work, revoke the Trakt app’s authorization and MDBList key as appropriate, and remove repository secrets. Review collaborator access and retain only backups you intend to keep.

For reauthorization or application changes, use [Recovery](recovery.md). Do **not** regenerate the key or rerun initialization.