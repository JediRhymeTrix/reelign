"""User-local onboarding. Secrets stay in memory unless explicitly exported.

GitHub CLI receives secret values and API bodies only on stdin. State initialization
creates an encrypted root commit and publishes its ref last; it never resets a ref.
"""

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings

from .http import ApiError, Http
from .model import SyncError
from .state import StateStore


ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = "jobs/list-sync/state.enc"
_REPO = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}")
_COMPONENT = re.compile(r"[A-Za-z0-9_-]+")
_WORKFLOW = re.compile(r"schedule-[a-z][a-z0-9-]{0,39}\.yml")


def _repository(value):
    if not isinstance(value, str) or not _REPO.fullmatch(value):
        raise SyncError("Repository must be a strict OWNER/NAME, not a URL or path")
    return value


def _required(name):
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SyncError(f"Missing environment secret {name}")
    return value.strip()


def _secret(name, optional=False):
    value = os.environ.get(name, "").strip()
    if not value and sys.stdin.isatty():
        suffix = " (optional for modern apps; Enter to skip)" if optional else ""
        # Fail closed when getpass cannot turn echo off; never fall back to input().
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass(name + suffix + ": ").strip()
            except (EOFError, getpass.GetPassWarning):
                raise SyncError(f"Use environment secret {name}; a hidden prompt is unavailable") from None
    if not value and not optional:
        raise SyncError(f"Set environment secret {name} or use an interactive hidden prompt")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SyncError(f"Invalid environment secret {name}")
    return value


def _oauth(value):
    try:
        token = json.loads(value) if isinstance(value, str) else dict(value)
        if not isinstance(token, dict):
            raise ValueError
        for field in ("access_token", "refresh_token"):
            if (not isinstance(token.get(field), str) or not token[field]
                    or any(ord(c) <= 32 or ord(c) == 127 for c in token[field])):
                raise ValueError
        if type(token.get("expires_in")) is not int or token["expires_in"] <= 0:
            raise ValueError
        token.setdefault("created_at", int(time.time()))
        if type(token["created_at"]) is not int or token["created_at"] < 0:
            raise ValueError
        # Reject NaN or non-JSON values without ever showing the response body.
        json.dumps(token, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise SyncError("Invalid TRAKT_OAUTH_JSON; need access/refresh tokens and positive expires_in") from None
    return token


def _gh(*args, data=None, allow_missing=False):
    try:
        result = subprocess.run(["gh", *args], input=data, capture_output=True,
                                text=True, timeout=60, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise SyncError("GitHub CLI failed or timed out; check gh authentication and permissions") from None
    if result.returncode:
        # Distinguish absent refs from permissions/network failures. Never echo stderr.
        if allow_missing and re.search(r"\(HTTP 404\)", result.stderr or ""):
            return None
        raise SyncError("GitHub CLI request failed; check gh authentication and repository permissions")
    return result.stdout


def _require_gh():
    if not shutil.which("gh"):
        raise SyncError("Install GitHub CLI (gh), then run gh auth login; no credentials were changed")
    _gh("auth", "status", "--hostname", "github.com")


def _api(repo, resource, body=None, allow_missing=False):
    args = ["api", f"repos/{repo}/git/{resource}"]
    data = None
    if body is not None:
        args += ["--method", "POST", "--input", "-"]
        data = json.dumps(body, separators=(",", ":"), allow_nan=False)
    else:
        args += ["--method", "GET"]
    raw = _gh(*args, data=data, allow_missing=allow_missing)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
    except (TypeError, ValueError):
        raise SyncError("GitHub API returned an invalid object") from None
    return value


def _new_branch_only(repo):
    if _api(repo, "ref/heads/cron-state", allow_missing=True) is not None:
        raise SyncError("Existing cron-state branch; refusing to reset state or reauthorize. Follow the recovery guide")


def _new_secrets_only(repo):
    raw = _gh("secret", "list", "--repo", repo, "--json", "name")
    try:
        rows = json.loads(raw)
        if (not isinstance(rows, list)
                or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows)):
            raise ValueError
        names = {row["name"] for row in rows}
    except (TypeError, ValueError):
        raise SyncError("Cannot safely inspect existing repository secret names") from None
    if "SYNC_STATE_KEY" in names:
        raise SyncError("Existing SYNC_STATE_KEY; refusing to rotate its key. Follow the recovery guide")
    if "TRAKT_OAUTH_JSON" in names:
        raise SyncError("Existing TRAKT_OAUTH_JSON; refusing to replace authorization. Follow the recovery guide")


def _device_authorize(client_id, client_secret):
    http = Http("https://auth.trakt.tv")
    try:
        device, _ = http.request("POST", "/oauth/device/code", body={"client_id": client_id})
    except ApiError:
        raise SyncError("Trakt device authorization failed; check your own application client ID") from None
    try:
        if not isinstance(device, dict):
            raise ValueError
        code, url = device["user_code"], device["verification_url"]
        if (not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9-]{4,32}", code)
                or not isinstance(url, str) or not re.fullmatch(r"https://(?:(?:www|auth)\.)?trakt\.tv/activate/?", url)
                or not isinstance(device["device_code"], str) or not device["device_code"]
                or type(device["interval"]) is not int or device["interval"] < 1
                or type(device["expires_in"]) is not int or device["expires_in"] < 1):
            raise ValueError
        interval = device["interval"]
        deadline = time.monotonic() + device["expires_in"]
    except (KeyError, TypeError, ValueError):
        raise SyncError("Trakt returned an invalid device authorization response") from None
    print(f"Code: {code}", flush=True)
    print(f"Verification URL: {url}", flush=True)
    body = {"code": device["device_code"], "client_id": client_id}
    if client_secret:
        body["client_secret"] = client_secret
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SyncError("Device authorization expired; run auth again")
        time.sleep(min(interval, remaining))
        if time.monotonic() >= deadline:
            raise SyncError("Device authorization expired; run auth again")
        try:
            token, _ = http.request("POST", "/oauth/device/token", body=body)
            return _oauth(token)
        except ApiError as exc:
            if exc.status == 400:  # Trakt: authorization_pending, not an OAuth refresh.
                continue
            if exc.status == 429:  # Trakt: slow_down; increase all subsequent intervals.
                interval += 5
                continue
            if exc.status == 410:
                raise SyncError("Device authorization expired; run auth again") from None
            raise SyncError("Trakt device authorization denied or failed; no automatic retry") from None


def _export_secrets(path, values):
    # O_EXCL refuses overwrite and existing symlinks; 0600 on POSIX. Windows users
    # must additionally verify ACLs, because chmod does not restrict Windows ACLs.
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        raise SyncError("Cannot create secret export; use a new private path, never overwrite an existing file") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(values, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise SyncError("Secret export failed; securely remove any partial export before retrying") from None
    print("WARNING: explicit plaintext secret export. Restrict file permissions/Windows ACLs, "
          "install secrets manually, then securely remove it; never commit it.", file=sys.stderr)
    print("Secret names: " + ", ".join(sorted(values)), file=sys.stderr)


def _auth(args):
    repo, output = args.repo, args.output_secret_file
    if args.reauthorize and (repo or output is None):
        raise SyncError("Reauthorization requires --output-secret-file PATH and cannot install repository secrets; "
                        "then use update-auth with your ORIGINAL SYNC_STATE_KEY to preserve baselines")
    if not repo and output is None:
        raise SyncError("Use auth --repo OWNER/NAME with gh (recommended), or explicitly opt into plaintext "
                        "with --output-secret-file PATH")
    if repo:
        repo = _repository(repo)
        _require_gh()
        _new_branch_only(repo)
        _new_secrets_only(repo)
    if output is not None:
        if output.exists() or output.is_symlink() or not output.parent.is_dir():
            raise SyncError("Secret export path must be new with an existing private parent directory")
        print("WARNING: --output-secret-file explicitly requests plaintext secrets; verify private "
              "permissions/Windows ACLs before continuing.", file=sys.stderr)
    client_id = _secret("TRAKT_CLIENT_ID")
    client_secret = _secret("TRAKT_CLIENT_SECRET", optional=True)
    mdb_key = None if args.reauthorize else _secret("MDBLIST_API_KEY")
    token = _device_authorize(client_id, client_secret)
    values = {"TRAKT_CLIENT_ID": client_id,
              "TRAKT_OAUTH_JSON": json.dumps(token, separators=(",", ":"))}
    if not args.reauthorize:
        values["MDBLIST_API_KEY"] = mdb_key
        values["SYNC_STATE_KEY"] = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
    if client_secret:
        values["TRAKT_CLIENT_SECRET"] = client_secret
    if output is not None:
        # An explicit backup must be durable before changing repository secrets.
        _export_secrets(output, values)
    if repo:
        # Recheck after the user's potentially long browser authorization. This
        # narrows accidental concurrent setups; gh offers no conditional secret set.
        _new_branch_only(repo)
        _new_secrets_only(repo)
        print("Installing repository secrets via gh stdin; do not run another setup concurrently.", file=sys.stderr)
        # Key last: failed earlier installs remain recoverable without replacing a key.
        for name, value in values.items():
            if name != "SYNC_STATE_KEY":
                _gh("secret", "set", name, "--repo", repo, data=value)
        _gh("secret", "set", "SYNC_STATE_KEY", "--repo", repo, data=values["SYNC_STATE_KEY"])
        print("Secrets installed. Initialize encrypted state, then preview before enabling apply.", file=sys.stderr)


def _sha(value):
    sha = value.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
        raise SyncError("GitHub API returned an invalid Git object ID; state ref was not reset")
    return sha


def _init_state(args):
    repo = _repository(args.repo)
    token = _oauth(_required("TRAKT_OAUTH_JSON"))
    key = _required("SYNC_STATE_KEY")
    # Only ciphertext reaches disk. Validate key and build state before any gh call.
    with tempfile.TemporaryDirectory(prefix="reelign-init-") as directory:
        path = Path(directory) / "state.enc"
        StateStore(path, key).save({"version": 1, "oauth": token, "rules": {}})
        encrypted = base64.b64encode(path.read_bytes()).decode("ascii")
    _require_gh()
    _new_branch_only(repo)
    if args.preview:
        print(f"Preview: would create cron-state with encrypted {STATE_PATH}; no remote writes.")
        return
    blob = _sha(_api(repo, "blobs", {"content": encrypted, "encoding": "base64"}))
    tree = _sha(_api(repo, "trees", {"tree": [{"path": STATE_PATH, "mode": "100644",
                                             "type": "blob", "sha": blob}]}))
    commit = _sha(_api(repo, "commits", {"message": "Initialize encrypted Reelign state",
                                       "tree": tree, "parents": []}))
    # POST ref is create-only: a concurrent/existing ref fails instead of resetting.
    _api(repo, "refs", {"ref": "refs/heads/cron-state", "sha": commit})
    print("Encrypted cron-state initialized with empty baselines; no lists changed. "
          "Preview first with --dry-run --bootstrap-state (workflow bootstrap_state=true); "
          "enable apply only after reviewing, with explicit --bootstrap-state for first bidirectional apply.")


def _settings(path):
    from .config import load_settings
    return load_settings(path)


def _update_auth(args):
    token = _oauth(_required("TRAKT_OAUTH_JSON"))
    key = _required("SYNC_STATE_KEY")
    if args.state_repo and not args.state.resolve().is_relative_to(args.state_repo.resolve()):
        raise SyncError("State file must be inside --state-repo; no files changed")
    store = StateStore(args.state, key, args.state_repo)
    state = store.load()  # Missing/corrupt/wrong-key state must never be bootstrapped.
    if args.preview:
        print("Preview: existing state decrypted; would replace OAuth only, preserving key and rule baselines.")
        return
    store.preflight()  # Ensure the existing state checkout is current and writable.
    state["oauth"] = token
    store.save(state)
    print("OAuth updated in existing encrypted state; original key and all rule baselines preserved. "
          "If the app changed, install its client ID/optional secret via gh stdin before resuming.")


def _validate(args):
    settings = _settings(args.config)
    print(f"Configuration valid: {len(settings.rules)} rule(s), "
          f"{sum(rule.enabled for rule in settings.rules)} enabled. No network or list changes.")


def _doctor(args):
    print("Local diagnostics only; no credential values, network calls, or authentication probes.")
    print("Python 3.12+: " + ("available" if sys.version_info >= (3, 12) else "upgrade required"))
    for tool in ("git", "gh"):
        print(f"{tool}: {'available' if shutil.which(tool) else 'missing; install before repository setup'}")
    for name in ("TRAKT_CLIENT_ID", "TRAKT_CLIENT_SECRET", "MDBLIST_API_KEY", "TRAKT_OAUTH_JSON", "SYNC_STATE_KEY"):
        optional = " (optional for modern Trakt applications)" if name == "TRAKT_CLIENT_SECRET" else ""
        print(f"{name}: {'set' if os.environ.get(name) else 'not set'}{optional}")
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        print("GitHub token environment: set (not validated)")
    else:
        print("GitHub auth: run gh auth login yourself; stored credentials were not read")
    _validate(args)
    print("Use auth --repo OWNER/NAME, then init-state, preview, and explicitly enable apply. "
          "Do not rerun auth on an established state/key; follow the recovery guide.")


def _render(args):
    # Lazy import keeps auth/init/doctor independent of workflow rendering imports.
    from .scheduling import render_workflows
    files = render_workflows(_settings(args.config))
    if (not isinstance(files, dict)
            or any(not isinstance(name, str) or not _WORKFLOW.fullmatch(name)
                   or not isinstance(content, str) for name, content in files.items())):
        raise SyncError("Renderer may only own schedule-*.yml files")
    folder = args.output_dir
    existing = {path.name: path for path in folder.glob("schedule-*.yml")}
    if any(path.is_symlink() or not path.is_file() for path in existing.values()):
        raise SyncError("Refusing non-file or symlink schedule workflows")
    drift = set(existing) - files.keys()
    for name, content in files.items():
        path = folder / name
        if name not in existing or path.read_bytes() != content.encode("utf-8"):
            drift.add(name)
    if args.check:
        if drift:
            raise SyncError("Generated schedule workflows differ; run manage.py render-workflows")
        print("Generated schedule workflows are up to date.")
        return
    folder.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        # Explicit bytes avoid CRLF drift on Windows.
        (folder / name).write_bytes(content.encode("utf-8"))
    for name in set(existing) - files.keys():
        existing[name].unlink()
    print(f"Rendered {len(files)} schedule workflow(s); other workflow names unchanged.")


def _metadata(value):
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, dict):
        raise SyncError("Source list metadata was missing or ambiguous")
    return value


def _text(value, field, limit):
    if (not isinstance(value, str) or len(value) > limit or (field == "title" and not value.strip())
            or any(ord(c) < 32 and c not in "\n\r\t" for c in value)):
        raise SyncError(f"Invalid destination {field}; provide an explicit --{field}")
    return value.strip()


def _create_destination(args):
    from .config import parse_url
    provider, owner, slug = parse_url(args.source)
    token = _oauth(_required("TRAKT_OAUTH_JSON"))
    if token["created_at"] + token["expires_in"] <= time.time() + 60:
        raise SyncError("OAuth expired/near expiry; use the documented state-aware recovery flow; no automatic refresh")
    client_id = _required("TRAKT_CLIENT_ID")
    trakt = Http("https://api.trakt.tv", headers={"trakt-api-key": client_id, "trakt-api-version": "2",
                                                "Authorization": "Bearer " + token["access_token"]})
    settings, _ = trakt.request("GET", "/users/settings")
    try:
        user = settings["user"]["ids"]["slug"]
        if not isinstance(user, str) or not _COMPONENT.fullmatch(user):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SyncError("Cannot determine authenticated Trakt account") from None
    if provider == "trakt":
        source, _ = trakt.request("GET", f"/users/{owner}/lists/{slug}")
    else:
        mdb = Http("https://api.mdblist.com", params={"apikey": _required("MDBLIST_API_KEY")})
        source, _ = mdb.request("GET", f"/lists/{owner}/{slug}")
    source = _metadata(source)
    title = _text(args.title if args.title is not None else source.get("name"), "title", 200)
    description = _text(args.description if args.description is not None else source.get("description", ""), "description", 10000)
    # Trakt get-lists is unpaginated; a future paginated response must not silently
    # bypass collision detection. Fail closed rather than adopting an existing list.
    lists, headers = trakt.request("GET", f"/users/{user}/lists")
    if not isinstance(lists, list) or any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in lists):
        raise SyncError("Cannot safely inspect destination list names")
    if any(key.lower() == "x-pagination-page-count" and str(value) != "1" for key, value in headers.items()):
        raise SyncError("Destination list enumeration incomplete; refusing to create")
    if any(row["name"].strip().casefold() == title.casefold() for row in lists):
        raise SyncError("A Trakt list with that name already exists; refusing adoption. Choose --title explicitly")
    created, _ = trakt.request("POST", f"/users/{user}/lists", body={"name": title, "description": description,
                                "privacy": "private", "display_numbers": False, "allow_comments": False,
                                "sort_by": "rank", "sort_how": "asc"})
    try:
        new_slug = created["ids"]["slug"]
        if created.get("privacy") != "private" or not isinstance(new_slug, str) or not _COMPONENT.fullmatch(new_slug):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SyncError("List creation response invalid; inspect your account before retrying (no automatic retry)") from None
    print(f"https://trakt.tv/users/{user}/lists/{new_slug}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Safe local Reelign setup; use your own provider applications and keys")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "doctor", "render-workflows"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=ROOT / "config.toml")
        if name == "render-workflows":
            command.add_argument("--output-dir", type=Path, default=ROOT / ".github" / "workflows")
            command.add_argument("--check", action="store_true")
    auth = commands.add_parser("auth", help="Authorize your own Trakt app and install secrets via gh stdin")
    auth.add_argument("--repo", metavar="OWNER/NAME")
    auth.add_argument("--output-secret-file", type=Path, help="Explicit plaintext export/backup; protect ACLs and never commit")
    auth.add_argument("--reauthorize", action="store_true", help="Export fresh OAuth only; no new state key. Use update-auth next")
    init = commands.add_parser("init-state", help="Create absent cron-state with encrypted empty baselines, never reset")
    init.add_argument("--repo", required=True, metavar="OWNER/NAME")
    init.add_argument("--preview", action="store_true", help="Validate and inspect the branch without remote writes")
    update = commands.add_parser("update-auth", help="Replace OAuth in existing state, preserving original key and baselines")
    update.add_argument("--state", type=Path, required=True)
    update.add_argument("--state-repo", type=Path)
    update.add_argument("--preview", action="store_true")
    create = commands.add_parser("create-destination", help="Explicitly create a new empty PRIVATE Trakt list")
    create.add_argument("--source", required=True)
    create.add_argument("--provider", required=True, choices=("trakt",))
    create.add_argument("--title")
    create.add_argument("--description")
    args = parser.parse_args(argv)
    actions = {"validate": _validate, "doctor": _doctor, "render-workflows": _render,
               "auth": _auth, "init-state": _init_state, "update-auth": _update_auth,
               "create-destination": _create_destination}
    try:
        actions[args.command](args)
    except ApiError:
        print("Setup failed: provider request failed; inspect your account before retrying. No automatic OAuth retry.", file=sys.stderr)
        return 1
    except SyncError as exc:
        print(f"Setup failed: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        print("Setup failed: invalid input or local I/O failure; secret details suppressed.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Setup cancelled; inspect any partial setup before retrying.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())