---
title: Security and responsible use
description: Reelign trust boundaries, encrypted OAuth/baseline storage, GitHub permissions, private configuration risks, secret handling, and provider-policy responsibilities.
permalink: /security/
---

# Security and responsible use

Reelign is user-operated software, not a credential custodian. You control the repository, provider application/accounts, encryption key, workflow changes, and collaborator access. That control comes with operational responsibility.

## What is stored where

| Location | Contents and exposure |
| --- | --- |
| `config.toml` / Git history | List URLs, stable rule IDs, policies, schedules. **Not encrypted** |
| Actions secrets | MDBList key, Trakt app credentials, initial OAuth JSON, original state key |
| `cron-state:jobs/list-sync/state.enc` | AES-256-GCM encrypted current OAuth pair and rule baselines |
| Workflow summaries/logs | Rule IDs, counts, planned/applied operations, safe error messages. Review before sharing |
| Failure artifacts | Encrypted recovery state when upload succeeds; short retention does not make it public-safe |
| Explicit local secret export | **Plaintext** credentials; only created when you opt in, never a safe backup until secured |

A private repository is recommended. A public copy exposes committed list URLs, schedule metadata, and public log/history details even if OAuth state is encrypted. Encrypting state does not make public configuration private. Private URLs themselves can disclose interests/account identity without granting access.

## Encryption and its boundary

State uses a base64-encoded 32-byte key with AES-256-GCM authenticated encryption, a fresh random nonce, and an authenticated format marker. Atomic local writes and Git persistence preserve the current pair and baselines. Missing, corrupt, incompatible, or wrong-key state is refused, not silently reset. Current code reads compatible legacy encrypted files.

Encryption protects stored ciphertext **without the key**. It does not protect against a malicious authorized workflow/collaborator that can execute code with secrets, a compromised runner/local machine, or exposure of both ciphertext and key. Keep the key backed up separately in encrypted storage. GitHub cannot show it back to you.

## Secrets and trusted execution

- Register your **own** Trakt app; use your own MDBList key. There are no shared tokens or application credentials.
- The local helper uses hidden prompts/environment and `gh` stdin, not secret-bearing arguments. No plaintext output exists by default.
- `--output-secret-file` is explicit plaintext opt-in, exclusive file creation, POSIX `0600`, and a Windows ACL warning. Verify directory permissions yourself. Ignore rules do not protect a file outside Git.
- Do not enable shell tracing (`set -x`), dump environments, print API responses, or paste secrets into workflow inputs. GitHub masking is not a universal redaction guarantee, especially for encoded/transformed values.
- Operational workflows use trusted default-branch code and scoped `contents: write` for state. Review every change to engine, dependencies, and workflows before running with credentials. Ordinary runs use `GITHUB_TOKEN`; no PAT is required.
- Untrusted pull requests should run offline tests/validation only, never provider-authorized sync. Do not add `pull_request_target` execution of untrusted checkout code with secrets.
- State concurrency covers the provided Actions jobs in **one** repository. It does not coordinate arbitrary local processes or duplicated installations. Pause all writers before local recovery.

## Provider calls and failure safety

Only direct HTTPS list URLs are accepted in config. Transport rejects redirects rather than forwarding authorization to another origin, suppresses raw secret-bearing response bodies/URLs in normal failures, and uses bounded selective retries. OAuth refresh and list creation are not automatically replayed after ambiguous failure.

Read completeness, ownership, ID ambiguity, deletion limits, addition verification, and post-write checks reduce risk; they do not create a cross-provider transaction. A failed operation can leave partial remote changes. Keep independent list exports/backups appropriate to your needs. See [Behavior](behavior.md) and [Recovery](recovery.md).

## Responsible use and required notice

Reelign is independent and unofficial, not affiliated with or sponsored by Trakt or MDBList. **Reelign uses Trakt but is not endorsed or certified by Trakt.** Provider names and marks belong to their respective owners.

Use only data and API access you are authorized to use. Follow each provider’s terms, developer requirements, permissions, and rate limits. An independently registered, user-operated tool is **not an automatic exception** to provider policies or restrictions. Reelign provides no policy-compliance guarantee.

Do not use shared credentials, account/application rotation, proxying identities, or scraping blocked data to evade access restrictions. If a provider denies access, stop affected automation and resolve it through authorized channels. Public visibility of another curator’s list is not a blanket license for every use of its data.

## Report a vulnerability privately

Follow [SECURITY.md](https://github.com/JediRhymeTrix/reelign/blob/main/SECURITY.md). Do not put tokens, decrypted state, ciphertext/key pairs, live private URLs, or full environment dumps in public issues. For ordinary setup errors, use [Troubleshooting](troubleshooting.md) and submit only sanitized reproduction details.