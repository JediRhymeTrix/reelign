---
title: Recovery without losing baselines
description: Recover encrypted OAuth state safely, handle single-use refresh failures, reauthorize with the original encryption key, and preserve list reconciliation history.
permalink: /recovery/
---

# Recovery without losing baselines

**Do not delete `cron-state`, generate a replacement key, replay an old refresh token, or rerun initialization to fix an error.** State contains both the active OAuth pair and the deletion history that makes bidirectional mirror safe.

## First: stop concurrent writers

Set `REELIGN_ENABLED=false` and disable the generated schedule wrappers, **Maintain Trakt authorization**, and any other operational writers while recovering. Wait for in-progress jobs to finish; changing a variable does not stop them. Do not launch a local writer while an Actions job still owns the same state.

Keep the original key in encrypted storage. Preserve the current `cron-state` commit/file and any newer encrypted failure artifact before making changes. Encrypted artifacts are still sensitive; do not post them publicly.

## Missing, corrupt, or wrong-key state

1. Confirm the intended repository, `cron-state` branch, state path, and **original** `SYNC_STATE_KEY`.
2. Check whether branch/ruleset permissions or an incorrect checkout caused the apparent absence.
3. Restore the newest valid encrypted backup **with the matching original key** only after investigating which copy contains the latest OAuth rotation.
4. If decryption fails, do not try bootstrap as a bypass. It refuses corrupt existing state intentionally.

An older state copy may contain a consumed refresh token. A key without usable state is not a current token backup; ciphertext without its key is not recoverable through Reelign. If the key is irretrievably lost, ordinary recovery cannot preserve/decrypt those baselines. Treat any new installation as a separately reviewed migration, export/inspect actual lists first, and reconsider bidirectional deletions; there is no automatic safe reset.

## State push failed after OAuth refresh

Refresh tokens are single-use. A successful refresh followed by a failed Git push may leave the **only current pair** in the runner’s local encrypted state file. The provided worker attempts to upload that file as `encrypted-recovery-RUN_ID` on failure, retained for seven days. Artifact upload itself is not guaranteed, so act promptly and inspect the run.

1. Pause writers and preserve both remote state and any recovery artifact.
2. Fix state-branch push permissions/connectivity without invoking another refresh.
3. Compare the failing run, state commit history, and artifact provenance. Do not blindly overwrite a newer remote state with an older artifact.
4. If the artifact is the authoritative newest encrypted state, restore its `state.enc` into a clean, current local checkout of `cron-state`, review the encrypted-file-only diff, commit and push normally. Do not force-reset the branch or merge token pairs manually.
5. Reauthorize using the state-aware flow below if there is no safely recoverable current token pair. Preserve the rule baselines and original key.

If an OAuth POST timed out, the provider might already have consumed the old token. Automatic replay is intentionally disabled. Resolve the state/token situation before another normal run.

## Reauthorize while preserving the original key and rules

Use this when authorization is revoked/expired beyond recovery, the active token pair is lost, or you intentionally change your Trakt application. Use the same Trakt account for existing owned destinations; changing account ownership is a separate configuration migration.

### 1. Obtain fresh OAuth, without a new encryption key

In a local, trusted terminal, with a protected `secrets/` directory:

```sh
python manage.py auth --reauthorize --output-secret-file secrets/recovery.json
```

Provide your own app client ID and optional client secret through hidden prompts/environment, then complete device approval. This special mode requires explicit plaintext export, cannot install with `--repo`, and exports only `TRAKT_CLIENT_ID`, optional `TRAKT_CLIENT_SECRET`, and fresh `TRAKT_OAUTH_JSON`. It emits **no `SYNC_STATE_KEY` and no MDBList key**.

Do not use ordinary initial `auth`, `init-state`, or `--bootstrap-state` here.

### 2. Clone the live state branch and load secrets securely

With all other writers paused:

```sh
gh repo clone OWNER/REPO state-checkout -- --branch cron-state --single-branch
git -C state-checkout status --short
```

Use a new checkout location if that directory already exists. It must be clean, current, and writable to `origin/cron-state`. Local Git authentication must permit pushing. Never stage the plaintext export into this checkout.

Load the **original backed-up key** and fresh OAuth into the environment without putting them in command history. This portable local Python pattern prompts invisibly for the original key and invokes both preview and apply from the same process:

```python
import getpass
import json
import os
from pathlib import Path
import subprocess
import sys

fresh = json.loads(Path("secrets/recovery.json").read_text(encoding="utf-8"))
env = os.environ.copy()
env["SYNC_STATE_KEY"] = getpass.getpass("ORIGINAL SYNC_STATE_KEY: ").strip()
env["TRAKT_OAUTH_JSON"] = fresh["TRAKT_OAUTH_JSON"]
command = [sys.executable, "manage.py", "update-auth",
           "--state", "state-checkout/jobs/list-sync/state.enc",
           "--state-repo", "state-checkout"]
subprocess.run(command + ["--preview"], env=env, check=True)
if input("Replace OAuth only and push existing state? Type UPDATE: ") == "UPDATE":
    subprocess.run(command, env=env, check=True)
```

Run the snippet as a local Python script outside any committed source file, or interactively in Python; do not paste it directly into a shell. It prints no token/key. Preview decrypts existing state without saving; apply additionally preflights clean/current/writable Git state, replaces only OAuth, and commits/pushes via StateStore. Existing rule baselines and the original encryption key are preserved. If preflight fails, fix it before retrying; do not disable the check.

Equivalent commands, when your approved secret manager already supplies the two environment variables:

```sh
python manage.py update-auth --state state-checkout/jobs/list-sync/state.enc --state-repo state-checkout --preview
python manage.py update-auth --state state-checkout/jobs/list-sync/state.enc --state-repo state-checkout
```

### 3. Update app secrets if the app changed

If the client ID/secret changed, install the new values from the fresh export by stdin; do **not** replace `SYNC_STATE_KEY`:

```python
import json
from pathlib import Path
import subprocess

fresh = json.loads(Path("secrets/recovery.json").read_text(encoding="utf-8"))
for name in ("TRAKT_CLIENT_ID", "TRAKT_CLIENT_SECRET", "TRAKT_OAUTH_JSON"):
    if name in fresh:
        subprocess.run(["gh", "secret", "set", name, "--repo", "OWNER/REPO"],
                       input=fresh[name], text=True, check=True)
```

If your new app has **no** client secret, remove the stale repository `TRAKT_CLIENT_SECRET` with `gh secret delete TRAKT_CLIENT_SECRET --repo OWNER/REPO`. Also clear any stale local environment secret. The OAuth secret remains only a seed; `update-auth` is what repairs active encrypted state.

### 4. Verify before resuming

Run a preview against current state, inspect ownership/membership/counts, and confirm the original baselines remain valid. Re-enable workflows and set `REELIGN_ENABLED=true` only when ready. Check the next maintenance run. Move any desired backup to encrypted storage and remove the plaintext recovery export.

## Intentional rule changes

Timing/profile/enabled changes preserve fingerprints. Endpoint, direction, policy, ordering, safety-limit, or ID changes can invalidate deletion history. Bidirectional mirror refuses mismatched baselines instead of interpreting that history under a new contract.

There is no automatic “accept new semantics/reset baseline” command. Preserve state and list exports, pause writers, preview the intended migration, and use a separately identified rule only if you consciously want a **new** reconciliation relationship. A new bidirectional mirror requires explicit union bootstrap, which may re-add items; an old rule should be disabled to avoid writer conflicts. Do not treat renaming the ID as a generic error fix.

## Credential exposure

Disable writers, revoke exposed provider credentials/authorization, and assess repository/log/artifact/history exposure. If the encryption key and ciphertext were exposed together, assume the decrypted state could be compromised. Key rotation/re-encryption is **not** automated by current CLI; do not simply replace the key secret. Follow [SECURITY.md](https://github.com/JediRhymeTrix/reelign/blob/main/SECURITY.md) and provider incident guidance, and preserve evidence privately without distributing tokens.