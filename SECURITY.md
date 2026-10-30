# Security policy

## Supported releases
Security fixes target the latest stable release. Review the changelog before
upgrading; never replace your configuration, encryption key or state blindly.

## Report a vulnerability privately
Use **Security → Advisories → Report a vulnerability** in the upstream GitHub
repository. Do not post credentials, decrypted state, private list URLs, recovery
artifacts or raw environment output in a public issue. If private reporting is
unavailable, open a minimal issue requesting a private reporting channel without
disclosing exploit details or secrets.

Include the version/commit, affected component, sanitized reproduction and impact.
Use synthetic credentials and lists. Do not test against other people's accounts.

## Deployment boundaries
- Public repository configuration, rule names, logs and encrypted history are public.
  Use a private template-generated installation to keep configuration private.
- Repository write access can modify code that receives secrets. Only trust your
  collaborators; review dependency/workflow updates before merging.
- PR CI must not receive installation secrets. Do not add `pull_request_target`
  jobs that execute untrusted PR code.
- Never share an OAuth refresh token with another installation: refresh is single-use.
- Losing `SYNC_STATE_KEY` prevents decryption; old GitHub bootstrap secrets cannot
  recover a rotated token pair. Back up the original key securely.
- Encrypted recovery artifacts remain sensitive. Retain only as needed and never
  publish the decryption key with the ciphertext.

See [security](docs/security.md) and [recovery](docs/recovery.md) for the threat model,
rotation, least-privilege workflow permissions and recovery procedures.