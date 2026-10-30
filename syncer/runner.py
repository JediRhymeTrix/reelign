"""Safe execution: all preflight checks, all additions, verification, then removals."""

import argparse
from graphlib import CycleError, TopologicalSorter
import json
import os
import re
from pathlib import Path
import sys
import time

from .model import SyncError, identify
from .state import StateStore


def delta(actual, desired):
    (old, new), _ = identify(actual, desired)
    old_set, new_set = set(old), set(new)
    return ([item for key, item in zip(new, desired) if key not in old_set],
            [item for key, item in zip(old, actual) if key not in new_set],
            old == new)


def execute_targets(targets, dry_run=False, authority_guard=None):
    """Verify every addition before ANY deletion, including across both endpoints."""
    prepared = []
    summaries = []
    for provider, snapshot, desired in targets:
        provider.preflight(snapshot)
        actual = provider.snapshot(order="rank")
        if delta(snapshot.items, actual.items)[0:2] != ([], []):
            raise SyncError("Destination changed while planning; retry next run")
        additions, removals, ordered = delta(actual.items, desired)
        prepared.append((provider, actual, desired, additions, removals))
        summaries.append({"add": len(additions), "remove": len(removals),
                          "reorder": not ordered, "dry_run": dry_run})
    if dry_run:
        return summaries
    for (provider, snapshot, _, _, _), summary in zip(prepared, summaries):
        summary["default_sort_changed"] = provider.ensure_rank_sort(snapshot)
    for provider, _, _, additions, _ in prepared:
        if additions:
            provider.add(additions)
    verified = []
    for provider, initial, desired, additions, removals in prepared:
        current = provider.snapshot(order="rank")
        missing, _, _ = delta(current.items, desired)
        if missing:
            raise SyncError("Addition verification failed; no removals performed")
        # New concurrent items must not be silently deleted by a stale plan.
        expected = initial.items + additions
        unexpected, missing_expected, _ = delta(expected, current.items)
        if unexpected or missing_expected:
            raise SyncError("Destination changed during additions; no removals performed")
        verified.append((provider, current, desired, removals))
    if authority_guard:
        authority_guard()
    for provider, _, _, removals in verified:
        if removals:
            provider.remove(removals)
    for provider, current, desired, removals in verified:
        current = provider.snapshot(order="rank")
        additions, extras, ordered = delta(current.items, desired)
        if additions or extras:
            raise SyncError("Membership verification failed; baseline not advanced")
        if not ordered:
            provider.reorder(desired, current)
            final = provider.snapshot(order="rank")
            if not delta(final.items, desired)[2]:
                raise SyncError("Rank verification failed; baseline not advanced")
    # Re-read every endpoint after all writes, including endpoints that were no-op.
    # No API offers cross-service transactions; minimize, detect and fail on races.
    for provider, _, desired, _ in verified:
        if not delta(provider.snapshot(order="rank").items, desired)[2]:
            raise SyncError("List changed before checkpoint; baseline not advanced")
    if authority_guard:
        authority_guard()
    return summaries


def required(name):
    value = os.environ.get(name)
    if not value:
        raise SyncError(f"Missing environment secret {name}")
    return value


def order_rules(rules):
    """Execute selected upstream writers before their readers, regardless of TOML order."""
    from .config import parse_url
    writers = {}
    for rule in rules:
        if rule.direction != "source_to_destination":
            writers[parse_url(rule.source)] = rule.id
        if rule.direction != "destination_to_source":
            writers[parse_url(rule.destination)] = rule.id
    dependencies = {}
    by_id = {rule.id: rule for rule in rules}
    for rule in rules:
        endpoints = (parse_url(rule.source), parse_url(rule.destination))
        dependencies[rule.id] = {writers[endpoint] for endpoint in endpoints
                                 if endpoint in writers and writers[endpoint] != rule.id}
    try:
        return [by_id[rule_id] for rule_id in TopologicalSorter(dependencies).static_order()]
    except CycleError:
        raise SyncError("Selected rules contain a dependency cycle; refusing execution") from None


def refresh_oauth(state, store, client_id, client_secret, dry_run):
    from .http import Http
    token = state["oauth"]
    expiry = token.get("created_at", 0) + token.get("expires_in", 0)
    if expiry > time.time() + 86400:
        return token
    if dry_run:
        if expiry <= time.time() + 60:
            raise SyncError("OAuth expired/near expiry; run normally to persist safe rotation")
        return token
    # Check durable storage BEFORE consuming a single-use refresh token.
    store.preflight()
    # Refresh POST must NOT automatically retry: Trakt refresh tokens are single-use.
    http = Http("https://auth.trakt.tv", headers={"Content-Type": "application/json"})
    body = {
        "grant_type": "refresh_token", "refresh_token": token["refresh_token"],
        "client_id": client_id,
        "redirect_uri": "urn:ietf:wg:oauth:2.0:oob",
    }
    if client_secret:
        body["client_secret"] = client_secret
    token, _ = http.request("POST", "/oauth/token", body=body)
    if not all(token.get(key) for key in ("access_token", "refresh_token", "expires_in")):
        raise SyncError("Invalid OAuth refresh response; cannot continue")
    token.setdefault("created_at", int(time.time()))
    state["oauth"] = token
    # Persist before any sync, so an unrelated list error never loses rotated tokens.
    store.save(state)
    return token


def main(argv=None):
    parser = argparse.ArgumentParser(description="Reelign: ordered MDBList/Trakt list sync")
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.toml")
    parser.add_argument("--state", type=Path, default=Path(__file__).resolve().parents[1] / ".state/state.enc")
    parser.add_argument("--state-repo", type=Path)
    parser.add_argument("--bootstrap-state", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rule", action="append", default=[], help="Run only this rule (repeatable)")
    parser.add_argument("--profile", help="Run only rules in this schedule profile")
    parser.add_argument("--maintenance-only", action="store_true", help="Refresh credentials without list reads")
    parser.add_argument("--validate-only", action="store_true", help="Validate configuration without secrets")
    args = parser.parse_args(argv)
    try:
        from .config import load_config, parse_url
        from .http import ApiError, Http
        from .planner import plan
        from .providers import MDBList, Trakt
        from .attribution import compose_description
        rules = load_config(args.config)
        if args.validate_only:
            print(json.dumps({"valid": True, "rules": len(rules)}))
            return 0
        known = {rule.id for rule in rules}
        if set(args.rule) - known:
            raise SyncError("Unknown selected rule; check rule IDs")
        if args.profile and not any(rule.profile == args.profile for rule in rules):
            raise SyncError("Selected profile has no rules")
        rules = [rule for rule in rules if rule.enabled
                 and (not args.rule or rule.id in args.rule)
                 and (not args.profile or rule.profile == args.profile)]
        rules = order_rules(rules)
        if not rules and not args.maintenance_only:
            print(json.dumps({"skipped": "No enabled selected rules; configure your list URLs first"}))
            return 0
        store = StateStore(args.state, required("SYNC_STATE_KEY"), args.state_repo)
        if args.state.exists():
            state = store.load()
        elif args.bootstrap_state:
            state = {"version": 1, "rules": {}, "oauth": json.loads(required("TRAKT_OAUTH_JSON"))}
        else:
            state = store.load()
        client_id = required("TRAKT_CLIENT_ID")
        token = refresh_oauth(state, store, client_id, os.environ.get("TRAKT_CLIENT_SECRET", ""), args.dry_run)
        if args.maintenance_only:
            print(json.dumps({"maintenance": "Credentials valid; no lists fetched", "dry_run": args.dry_run}))
            return 0
        trakt_http = Http("https://api.trakt.tv", headers={
            "trakt-api-key": client_id, "trakt-api-version": "2",
            "Authorization": "Bearer " + token["access_token"],
        })
        mdb_http = Http("https://api.mdblist.com", params={"apikey": required("MDBLIST_API_KEY")})
        account, _ = trakt_http.request("GET", "/users/settings")
        try:
            user = account["user"]["ids"]["slug"]
            if not isinstance(user, str) or re.fullmatch(r"[A-Za-z0-9_-]+", user) is None:
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise SyncError("Cannot determine authenticated Trakt account") from None

        def provider(url):
            return MDBList(url, mdb_http) if "mdblist.com/" in url else Trakt(url, trakt_http, user)

        summary = []
        failed_writers = set()
        had_error = False
        for rule in rules:
            try:
                endpoints = {parse_url(rule.source), parse_url(rule.destination)}
                if endpoints & failed_writers:
                    raise SyncError("Dependent rule skipped after upstream failure")
                source, destination = provider(rule.source), provider(rule.destination)
                left, right = source.snapshot(), destination.snapshot()
                result = plan(rule, left, right, state["rules"].get(rule.id), bootstrap=args.bootstrap_state)
                targets = []
                if result.writes_source:
                    targets.append((source, left, result.source))
                if result.writes_destination:
                    targets.append((destination, right, result.destination))
                credits = []
                attribution_report = []
                if rule.attribution:
                    for adapter, original, origin_adapter, origin in (
                        (source, left, destination, right),
                        (destination, right, source, left),
                    ):
                        written = result.writes_source if adapter is source else result.writes_destination
                        if not written:
                            continue
                        if not adapter.supports_description:
                            attribution_report.append({"provider": original.provider,
                                                       "status": "unsupported_destination_api"})
                            continue
                        desired_description = compose_description(
                            original.description, origin, origin_adapter.creator(origin),
                            formatting=adapter.description_format,
                            destination_public=original.metadata.get("privacy") != "private",
                        )
                        report = {"provider": original.provider,
                            "status": "would_update" if desired_description != original.description else "unchanged"}
                        credits.append((adapter, original, desired_description, report))
                        attribution_report.append(report)

                def authority_guard():
                    for adapter, original, written in (
                        (source, left, result.writes_source),
                        (destination, right, result.writes_destination),
                    ):
                        if not written:
                            current = adapter.snapshot()
                            if not delta(current.items, original.items)[2] or current.version != original.version:
                                raise SyncError("Read-only authority changed; retry without advancing baseline")

                changes = execute_targets(targets, dry_run=args.dry_run,
                                          authority_guard=authority_guard)
                if not args.dry_run:
                    for adapter, original, description, report in credits:
                        changed = adapter.ensure_description(original, description)
                        report["status"] = "updated" if changed else "unchanged"
                entry = {"rule": rule.id, "source_count": len(left.items),
                         "destination_count": len(right.items), "targets": changes,
                         "attribution": attribution_report}
            except SyncError as exc:
                if isinstance(exc, ApiError) and exc.status in {401, 403}:
                    raise
                had_error = True
                if rule.direction != "source_to_destination":
                    failed_writers.add(parse_url(rule.source))
                if rule.direction != "destination_to_source":
                    failed_writers.add(parse_url(rule.destination))
                entry = {"rule": rule.id, "error": str(exc)}
                print(json.dumps(entry, sort_keys=True), flush=True)
                summary.append(entry)
                continue
            print(json.dumps(entry, sort_keys=True), flush=True)
            summary.append(entry)
            if not args.dry_run:
                state["rules"][rule.id] = result.baseline
                store.save(state)
        if path := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(path, "a", encoding="utf-8") as stream:
                stream.write("## List sync\n```json\n" + json.dumps(summary, indent=2) + "\n```\n")
        return 1 if had_error else 0
    except (SyncError, ValueError, KeyError, TypeError, OSError) as exc:
        # Unexpected payloads are never dumped: may contain auth or secret query URLs.
        message = str(exc) if isinstance(exc, SyncError) else "Invalid configuration/state/API shape"
        print("ERROR: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())