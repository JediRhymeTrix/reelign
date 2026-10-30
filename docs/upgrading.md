---
title: Upgrading and private-job backports
description: Review upstream Reelign updates, preserve configuration and encryption keys, regenerate schedule wrappers, and use the allowlisted private-job backport/parity script.
permalink: /upgrading/
---

# Upgrading without replacing your installation

Template-generated repositories do not automatically receive source updates. You own reviewing and merging them. Read the upstream [changelog](https://github.com/JediRhymeTrix/reelign/blob/main/CHANGELOG.md) and compare the code/workflows before giving a new revision access to your secrets.

## Update a standalone template installation

1. Pause scheduled list writers and maintenance; wait for active jobs. Back up the latest encrypted state and original key separately.
2. Record your current commit and review upstream changes. If desired, add the upstream remote:

   ```sh
   git remote add upstream https://github.com/JediRhymeTrix/reelign.git
   git fetch upstream
   git diff HEAD..upstream/main -- syncer/ run.py manage.py requirements.txt .github/workflows/
   ```

   Skip `remote add` if already configured. Fetching does not merge anything.
3. Integrate the reviewed changes in a branch. A template copy may not share Git ancestry; do not blindly force a merge or use `--allow-unrelated-histories` as an upgrade strategy. Copy/cherry-pick reviewed engine, tests, tools, and workflow changes as appropriate to your repository.
4. Preserve your `config.toml`, secrets, original key, state branch, and rule IDs. Never copy the upstream disabled sample over your actual config. Do not import someone else’s config/state.
5. Install updated pinned dependencies and run offline checks:

   ```sh
   python -m pip install -r requirements.txt
   python -m unittest discover -s tests -v
   python manage.py validate
   python manage.py render-workflows
   python manage.py render-workflows --check
   ```

6. Review/commit generated workflows and trusted-code changes. Merge to your default branch. Run a preview, inspect counts/order, and perform a deliberate apply only if intended. Resume maintenance and schedules after validation.

Compatible legacy encrypted state and fingerprints are accepted by current code. Schedule/profile/enabled changes preserve baselines. A pure dry run does not rewrite legacy state; an actual changed save may write the current authenticated envelope. Never use a new state key merely because source code was updated.

## Backport the shared engine into an existing private job

For an existing integration that embeds this engine rather than using the standalone workflows, run the public checkout’s allowlisted script. The target must be a **separate existing job directory**, not the source or an ancestor/descendant. Replace the path below with your own job location:

```sh
python scripts/backport.py --target /absolute/path/to/job --check
python scripts/backport.py --target /absolute/path/to/job
python scripts/backport.py --target /absolute/path/to/job --check
```

`--check` is a read-only parity check: `0` means shared bytes match; `1` means drift/failure. Apply copies only shared Python modules under `syncer/`, `tests/`, and `scripts/`, test fixtures, and root `run.py`, `manage.py`, `requirements.txt`. It removes stale top-level `.py` files inside those shared directories, so keep custom Python modules elsewhere or review them before applying.

It writes `UPSTREAM.json` with source commit/hash provenance. It does **not** copy configuration, state, secrets, application workflows, docs, or unrelated jobs, and does not contact providers. It is not a package manager or a credential migration tool. Review the diff and test the target’s own integration before deploying.

Pause the target’s writers and back up current state/key first. Keep its existing durable-state path, account credentials, and operational workflow contract. Preview, apply if intended, then preview again and verify actual rank ordering; do not assume byte parity proves remote correctness.

## Rollback cautions

Reverting code is not permission to restore old rotating OAuth state. An earlier token snapshot may have a consumed refresh token; old/new semantic fingerprints may also differ. Retain the newest valid encrypted state and original key, inspect version compatibility, and use [Recovery](recovery.md) if authorization needs repair. Never reset the state branch to an older commit as an automatic code rollback.