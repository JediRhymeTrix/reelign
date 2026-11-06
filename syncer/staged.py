"""Encrypted write-ahead intents and restart-safe observed-effect reconciliation."""

from copy import deepcopy
from datetime import datetime, timezone
import re

from .config import parse_url
from .http import ApiError
from .model import Item, Snapshot, SyncError, StateError, identify
from .planner import Plan, plan, validate_deletions


class Deferred(SyncError):
    """Acknowledged effects await a later strict observation, not convergence."""


class Blocked(SyncError):
    """Ambiguous intent or conflicting recovery requires explicit review."""


def _date(value):
    if not isinstance(value, str):
        raise SyncError("Invalid journal timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise SyncError("Invalid journal timestamp") from None


def records(items):
    from .providers import _ids
    return [{"media_type": item.media_type, "ids": _ids(item)} for item in items]


def items(value):
    if not isinstance(value, list):
        raise SyncError("Invalid journal identity array")
    result = []
    for row in value:
        if (not isinstance(row, dict) or set(row) != {"media_type", "ids"}
                or not isinstance(row["media_type"], str) or not isinstance(row["ids"], dict)
                or any(not isinstance(key, str) or type(value) not in (str, int)
                       for key, value in row["ids"].items())):
            raise SyncError("Invalid journal identity record")
        item = Item(row["media_type"], dict(row["ids"]))
        item.aliases()
        result.append(item)
    identify(result)
    return result


def encode(snapshot):
    return {"url": snapshot.url, "name": snapshot.name, "description": snapshot.description,
            "version": snapshot.version, "metadata": deepcopy(snapshot.metadata),
            "items": [{**row, "added_at": item.added_at, "rank": item.rank,
                       "entry_id": item.entry_id}
                      for row, item in zip(records(snapshot.items), snapshot.items)]}


def decode(value):
    if (not isinstance(value, dict) or set(value) != {
            "url", "name", "description", "version", "metadata", "items"}
            or not isinstance(value["metadata"], dict) or not isinstance(value["items"], list)
            or any(not isinstance(value[key], str) for key in ("url", "name", "description", "version"))):
        raise SyncError("Invalid journal snapshot")
    parse_url(value["url"])
    metadata = value["metadata"]
    for key in ("id", "item_count", "items"):
        if key in metadata and (type(metadata[key]) is not int or metadata[key] < (1 if key == "id" else 0)):
            raise SyncError("Invalid journal metadata count or identity")
    if "ids" in metadata:
        if not isinstance(metadata["ids"], dict):
            raise SyncError("Invalid journal metadata identity")
        if "trakt" in metadata["ids"] and (type(metadata["ids"]["trakt"]) is not int or metadata["ids"]["trakt"] <= 0):
            raise SyncError("Invalid journal metadata identity")
    if "updated_at" in metadata and metadata["updated_at"] not in {"v1", "v2"}:
        _date(metadata["updated_at"])
    result = []
    for row in value["items"]:
        if (not isinstance(row, dict) or set(row) != {
                "media_type", "ids", "added_at", "rank", "entry_id"}
                or type(row["rank"]) is not int or row["rank"] < 0
                or (row["entry_id"] is not None and
                    (type(row["entry_id"]) is not int or row["entry_id"] <= 0))
                or (row["added_at"] is not None and not isinstance(row["added_at"], str))):
            raise SyncError("Invalid journal media row")
        identity = items([{"media_type": row["media_type"], "ids": row["ids"]}])[0]
        if row["added_at"] is not None:
            _date(row["added_at"])
        identity.added_at, identity.rank, identity.entry_id = row["added_at"], row["rank"], row["entry_id"]
        result.append(identity)
    identify(result)
    return Snapshot(value["url"], value["name"], value["description"], result,
                    value["version"], metadata=deepcopy(value["metadata"]))


def validate_journals(state):
    """Validate encrypted untrusted state before any provider use."""
    if type(state.get("version")) is not int or state.get("version") != 2 or not isinstance(state.get("journals"), dict):
        raise SyncError("Invalid staged state schema")
    for rule_id, journal in state["journals"].items():
        if (not isinstance(rule_id, str) or not rule_id or not isinstance(journal, dict)
                or set(journal) - {"version", "fingerprint", "urls", "desired", "expected", "operations", "created_at", "deletions", "origin_counts"}
                or not {"version", "fingerprint", "urls", "desired", "expected", "operations", "created_at"} <= set(journal)
                or type(journal["version"]) is not int or journal["version"] != 1
                or not isinstance(journal["fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", journal["fingerprint"])
                or not isinstance(journal["created_at"], str)
                or not isinstance(journal["urls"], list) or len(journal["urls"]) != 2
                or not isinstance(journal["desired"], list) or len(journal["desired"]) != 2
                or not isinstance(journal["expected"], list) or len(journal["expected"]) != 2
                or not isinstance(journal["operations"], list)):
            raise SyncError("Invalid pending journal")
        _date(journal["created_at"])
        if "deletions" in journal or "origin_counts" in journal:
            if (not isinstance(journal.get("deletions"), list) or len(journal["deletions"]) != 2
                    or not isinstance(journal.get("origin_counts"), list) or len(journal["origin_counts"]) != 2
                    or any(type(count) is not int or count < 0 for count in journal["origin_counts"])):
                raise SyncError("Invalid journal deletion ledger")
            for value in journal["deletions"]:
                items(value)
        endpoints = [parse_url(url) for url in journal["urls"]]
        if endpoints[0] == endpoints[1]:
            raise SyncError("Pending journal endpoints overlap")
        for index in range(2):
            items(journal["desired"][index])
            if parse_url(decode(journal["expected"][index]).url) != endpoints[index]:
                raise SyncError("Pending journal endpoint mismatch")
        for op in journal["operations"]:
            if (not isinstance(op, dict) or set(op) != {"side", "action", "outcome", "items", "entries", "fields", "after"}
                    or type(op["side"]) is not int or op["side"] not in {0, 1}
                    or not isinstance(op["action"], str) or op["action"] not in {"add", "remove", "reorder", "metadata"}
                    or not isinstance(op["outcome"], str) or op["outcome"] not in {"intent", "acknowledged", "unknown", "rejected", "invalid_response"}
                    or not isinstance(op["entries"], list) or not isinstance(op["fields"], dict)
                    or set(op["fields"]) - {"sort_by", "sort_how", "description"}
                    or any(not isinstance(value, str) for value in op["fields"].values())
                    or any(type(entry) is not int or entry <= 0 for entry in op["entries"])
                    or len(set(op["entries"])) != len(op["entries"])):
                raise SyncError("Invalid pending operation")
            items(op["items"])
            if op["action"] in {"add", "remove"} and (not op["items"] or op["entries"] or op["fields"]):
                raise SyncError("Invalid membership operation payload")
            if op["action"] == "metadata" and (op["items"] or op["entries"] or not op["fields"]):
                raise SyncError("Invalid metadata operation payload")
            if op["action"] == "reorder" and (op["fields"] or bool(op["items"]) == bool(op["entries"])):
                raise SyncError("Invalid reorder operation payload")
            if parse_url(decode(op["after"]).url) != endpoints[op["side"]]:
                raise SyncError("Pending operation endpoint mismatch")
            projected = decode(op["after"])
            if op["action"] in {"add", "remove"}:
                actual, changed = _keys(projected.items, items(op["items"]))
                if ((op["action"] == "add" and not set(changed) <= set(actual))
                        or (op["action"] == "remove" and set(changed) & set(actual))):
                    raise SyncError("Operation projection contradicts membership action")
            if op["action"] == "reorder":
                if [item.rank for item in projected.items] != list(range(1, len(projected.items) + 1)):
                    raise SyncError("Invalid projected reorder ranks")
                if op["entries"] and [item.entry_id for item in projected.items] != op["entries"]:
                    raise SyncError("Invalid projected reorder entry sequence")
                if op["items"] and _keys(projected.items, items(op["items"]))[0] != _keys(projected.items, items(op["items"]))[1]:
                    raise SyncError("Invalid projected reorder membership")
            if op["action"] == "metadata" and any(projected.metadata.get(k) != v for k, v in op["fields"].items()):
                raise SyncError("Invalid projected metadata action")


def _keys(left, right):
    return identify(left, right)[0]


def _project(snapshot, action, payload, entries, fields):
    result = deepcopy(snapshot)
    (current, changed), _ = identify(result.items, payload)
    if action == "add":
        present = set(current)
        result.items.extend(item for key, item in zip(changed, payload) if key not in present)
    elif action == "remove":
        removed = set(changed)
        result.items = [item for key, item in zip(current, result.items) if key not in removed]
    elif action == "reorder":
        if entries:
            by_entry = {item.entry_id: item for item in result.items}
            if set(entries) != set(by_entry):
                raise SyncError("Pending reorder entry mapping is incomplete")
            result.items = [by_entry[entry] for entry in entries]
        else:
            wanted, found = _keys(payload, result.items)
            if set(wanted) != set(found):
                raise SyncError("Staged MDBList reorder above 1000 items is unsupported")
            result.items = list(payload)
    else:
        result.metadata.update(fields)
        if "description" in fields:
            result.description = fields["description"]
    for rank, item in enumerate(result.items, 1):
        item.rank = rank
    return result


def _operation(side, method, path, body, expected):
    if method == "PUT":
        if not isinstance(body, dict) or set(body) - {"description", "sort_by", "sort_how"}:
            raise SyncError("Unsupported journal metadata mutation")
        action, payload, entries, fields = "metadata", [], [], dict(body)
    else:
        action = "reorder" if path.endswith("/reorder") else "remove" if path.endswith("/remove") else "add"
        payload, entries, fields = [], [], {}
        if action == "reorder":
            if "rank" in body:
                entries = list(body["rank"])
            else:
                # MDBList custom reorder is one request <=1000. Multiple chunks
                # require a separate projection contract, not guessed geometry.
                if body.get("position") != "top" or "after" in body or "before" in body:
                    raise SyncError("Staged MDBList reorder above 1000 items is unsupported")
                payload = [Item(row["mediatype"], row["ids"]) for row in body["items"]]
        else:
            for kind in ("movies", "shows"):
                for row in body.get(kind, []):
                    payload.append(Item(kind[:-1], dict(row.get("ids", row))))
            if not payload or len(payload) > 100:
                raise SyncError("Invalid staged membership batch")
    before = decode(expected)
    if payload:
        sequences, merged = identify(before.items, payload)
        payload = [merged[key] for key in sequences[1]]
    after = _project(before, action, payload, entries, fields)
    return {"side": side, "action": action, "outcome": "intent", "items": records(payload),
            "entries": entries, "fields": fields, "after": encode(after)}


class Session:
    """Wrap each actual HTTP mutation, not a provider's hidden batch loop."""

    def __init__(self, rule, providers, snapshots, result, state, store):
        self.rule, self.providers, self.state, self.store = rule, providers, state, store
        self.originals = []
        self.observations = list(snapshots)
        self.verified_operations = 0
        prior_baseline = state.get("rules", {}).get(rule.id)
        identity_context = [snapshot.items for snapshot in snapshots]
        identity_context += [result.source, result.destination]
        if prior_baseline is not None and rule.matches_fingerprint(prior_baseline.get("fingerprint")):
            identity_context += [items(prior_baseline[side]) for side in ("source", "destination")]
        prior_journal = state.get("journals", {}).get(rule.id)
        if prior_journal:
            identity_context += [decode(value).items for value in prior_journal["expected"]]
            identity_context += [items(value) for value in prior_journal["desired"]]
            identity_context += [items(op["items"]) for op in prior_journal["operations"]]
        sequences, merged = identify(*identity_context)
        enriched = deepcopy(snapshots)
        for snapshot, keys in zip(enriched, sequences[:2]):
            for item, key in zip(snapshot.items, keys):
                item.ids = dict(merged[key].ids)
        self.journal = {"version": 1, "fingerprint": rule.fingerprint,
            "urls": [rule.source, rule.destination],
            "desired": [records(result.source), records(result.destination)],
            "expected": [encode(snapshot) for snapshot in enriched], "operations": [],
            "created_at": datetime.now(timezone.utc).isoformat()}
        self.started = rule.id in state.get("journals", {})
        prior = state.get("journals", {}).get(rule.id)
        self.journal["deletions"] = deepcopy(prior.get("deletions", [[], []])) if prior else [[], []]
        self.journal["origin_counts"] = list(prior.get("origin_counts", [len(s.items) for s in snapshots])) if prior else [len(s.items) for s in snapshots]
        if prior:
            self.journal["created_at"] = prior["created_at"]
            # A verified observation does not authorize replay if a subsequent
            # read regresses. Keep prior write predicates until final checkpoint.
            self.journal["operations"] = deepcopy(prior["operations"])

    def persist(self):
        self.state["version"] = 2
        self.state.setdefault("journals", {})[self.rule.id] = self.journal
        try:
            self.store.save(self.state)
        except Exception:
            raise StateError("Cannot persist staged operation; no further mutations permitted") from None
        self.started = True

    def observe(self, provider, snapshot):
        side = self.providers.index(provider)
        self.observations[side] = snapshot
        if not self.journal["operations"]:
            return
        context = [decode(value).items for value in self.journal["expected"]]
        context += [decode(op["after"]).items for op in self.journal["operations"]]
        for original, check in _outstanding(self.journal["operations"]):
            if check["side"] == side and not _verified(check, snapshot, context):
                if original["outcome"] in {"intent", "unknown", "invalid_response"}:
                    raise Blocked("Prior operation evidence regressed; refusing automatic replay")
                raise Deferred("A journaled effect is not visible in the current execution snapshot")
        self.verified_operations = len(self.journal["operations"])

    def __enter__(self):
        self.store.preflight()
        session = self
        for side, provider in enumerate(self.providers):
            # Non-network fixture providers remain usable by the low-level runner;
            # production adapters always have this transport interface.
            if not hasattr(provider, "http"):
                continue
            original = provider.http
            class Proxy:
                def __init__(self, index, transport):
                    self.index, self.transport = index, transport
                def request(self, method, path, params=None, body=None):
                    if method.upper() == "GET":
                        return self.transport.request(method, path, params, body)
                    return session.send(self.index, self.transport, method, path, params, body)
            provider.http = Proxy(side, original)
            self.originals.append((provider, original))
        return self

    def send(self, side, transport, method, path, params, body):
        if path.endswith("/reorder") and "rank" in body:
            latest = getattr(self.providers[side], "_last_snapshot", None)
            expected = decode(self.journal["expected"][side])
            if isinstance(latest, Snapshot):
                (old, found), _ = identify(expected.items, latest.items)
                if set(old) != set(found):
                    raise Blocked("Destination membership changed before journaled reorder")
                local = dict(zip(found, latest.items))
                for key, item in zip(old, expected.items):
                    item.entry_id = local[key].entry_id
                self.journal["expected"][side] = encode(expected)
        op = _operation(side, method, path, body, self.journal["expected"][side])
        if op["action"] == "remove":
            old, new = items(self.journal["deletions"][side]), items(op["items"])
            sequences, reps = identify(old, new)
            keys = set(sequences[0]) | set(sequences[1])
            if (len(keys) > self.rule.max_delete_count
                    or len(keys) > self.rule.max_delete_fraction * self.journal["origin_counts"][side]):
                raise Blocked("Cumulative staged deletion limit exceeded; reviewed recovery required")
            self.journal["deletions"][side] = records([reps[key] for key in sorted(keys)])
        self.journal["operations"].append(op)
        self.persist()
        try:
            response = transport.request(method, path, params, body)
        except ApiError as exc:
            op["outcome"] = "rejected" if exc.outcome == "rejected" else "unknown"
            self.persist()
            raise
        try:
            data = response[0]
            if op["action"] in {"add", "remove"}:
                from .providers import _write_result
                counts = {}
                for item in items(op["items"]):
                    key = item.media_type + "s"
                    counts[key] = counts.get(key, 0) + 1
                field = "added" if op["action"] == "add" else "removed" if parse_url(self.journal["urls"][side])[0] == "mdblist" else "deleted"
                _write_result(data, counts, field, allow_existing=op["action"] == "add")
            elif op["action"] == "reorder":
                from .providers import _object, _integer, _has_value
                result = _object(data)
                expected = len(op["entries"] or op["items"])
                if _has_value(result.get("not_found")) or _has_value(result.get("skipped_ids")) or _has_value(result.get("error")):
                    raise SyncError("Invalid reorder acknowledgement")
                count = _integer(result.get("updated"))
                if count > expected or op["entries"] and count != expected:
                    raise SyncError("Partial reorder acknowledgement")
        except SyncError:
            op["outcome"] = "invalid_response"
            self.persist()
            raise
        op["outcome"] = "acknowledged"
        self.journal["expected"][side] = op["after"]
        self.persist()
        return response

    def __exit__(self, kind, error, traceback):
        for provider, original in self.originals:
            provider.http = original
        if error is not None and self.started and isinstance(error, SyncError) and not isinstance(error, StateError):
            if isinstance(error, ApiError) or "partial" in str(error).lower() or "could not" in str(error).lower():
                return False
            from .providers import _SnapshotChanged, _SnapshotOverlap
            if isinstance(error, (_SnapshotChanged, _SnapshotOverlap, Deferred)):
                raise Deferred("Pending verification; confirmed baseline unchanged") from None
        return False

    def commit(self, baseline):
        self.state["rules"][self.rule.id] = baseline
        if "journals" in self.state:
            self.state["journals"].pop(self.rule.id, None)
        try:
            self.store.save(self.state)
        except Exception:
            raise StateError("Cannot checkpoint verified reconciliation; recovery state retained") from None


def _verified(op, current, context=()):
    action = op["action"]
    if action in {"add", "remove"}:
        sequences, _ = identify(current.items, items(op["items"]), *context)
        actual, changed = sequences[:2]
        return set(changed) <= set(actual) if action == "add" else not set(changed) & set(actual)
    if action == "metadata":
        return all(current.metadata.get(key) == value for key, value in op["fields"].items())
    if op["entries"]:
        return [item.entry_id for item in sorted(current.items, key=lambda item: item.rank)] == op["entries"]
    wanted, found = _keys(items(op["items"]), current.items)
    return wanted == found


def _outstanding(operations):
    """Later own actions supersede earlier postconditions, retaining aliases."""
    payloads = [items(op["items"]) for op in operations]
    sequences, _ = identify(*payloads)
    latest = {}
    for index, (op, keys) in enumerate(zip(operations, sequences)):
        if op["outcome"] != "rejected" and op["action"] in {"add", "remove"}:
            for key in keys:
                latest[op["side"], key] = index
    for index, op in enumerate(operations):
        if op["outcome"] == "rejected":
            continue
        check = deepcopy(op)
        if op["action"] in {"add", "remove"}:
            check["items"] = records([item for key, item in zip(sequences[index], payloads[index])
                                      if latest[op["side"], key] == index])
        elif op["action"] == "metadata":
            later = {key for other in operations[index + 1:]
                     if other["side"] == op["side"] and other["outcome"] != "rejected"
                     for key in other["fields"]}
            check["fields"] = {key: value for key, value in op["fields"].items() if key not in later}
        elif any(other["side"] == op["side"] and other["outcome"] != "rejected"
                 and other["action"] in {"add", "remove", "reorder"} for other in operations[index + 1:]):
            continue
        yield op, check


def resume(rule, providers, journal):
    validate_journals({"version": 2, "journals": {rule.id: journal}})
    if (journal["fingerprint"] != rule.fingerprint
            or [parse_url(url) for url in journal["urls"]] != [parse_url(rule.source), parse_url(rule.destination)]):
        raise Blocked("Pending reconciliation configuration changed; reviewed recovery required")
    if (datetime.now(timezone.utc) - datetime.fromisoformat(journal["created_at"].replace("Z", "+00:00"))).total_seconds() > 86400:
        raise Blocked("Pending reconciliation exceeded 24 hours; reviewed recovery required")
    if any(op["outcome"] == "invalid_response" for op in journal["operations"]):
        raise Blocked("Prior mutation response was partial or malformed; reviewed recovery required")
    current = []
    try:
        for side, provider in enumerate(providers):
            snapshot = provider.snapshot()
            original = decode(journal["expected"][side])
            for key in ("id", "ids"):
                if snapshot.metadata.get(key) != original.metadata.get(key):
                    raise Blocked("Pending destination identity changed")
            if ((side == 0 and rule.direction != "source_to_destination")
                    or (side == 1 and rule.direction != "destination_to_source")):
                provider.preflight(snapshot)
            current.append(snapshot)
    except (Blocked, ApiError):
        raise
    except SyncError as error:
        from .providers import _SnapshotChanged, _SnapshotOverlap
        if not isinstance(error, (_SnapshotChanged, _SnapshotOverlap)):
            raise
        if any(op["outcome"] in {"intent", "unknown"} for op in journal["operations"]):
            raise Blocked("Unknown operation cannot be verified from inconsistent observations") from None
        raise Deferred("Pending verification; provider snapshot is not yet consistent") from None
    expected = [decode(value) for value in journal["expected"]]
    for op, check in _outstanding(journal["operations"]):
        if op["outcome"] == "invalid_response":
            raise Blocked("Prior mutation response was partial or malformed; reviewed recovery required")
        context = [decode(value).items for value in journal["expected"]]
        context += [items(value) for value in journal["desired"]]
        context += [decode(value["after"]).items for value in journal["operations"]]
        if not _verified(check, current[op["side"]], context):
            if op["outcome"] in {"intent", "unknown"}:
                raise Blocked("Unknown operation outcome; refusing automatic replay")
            raise Deferred("Acknowledged operation is pending verification")
        if op["outcome"] in {"intent", "unknown"}:
            expected[op["side"]] = _project(expected[op["side"]], check["action"],
                items(check["items"]), check["entries"], check["fields"])
    return current[0], current[1], rebase(rule, journal, expected, current)


def rebase(rule, journal, expected, current):
    if not rule.enabled:
        return plan(rule, *current, None)
    if rule.direction != "bidirectional":
        return plan(rule, *current, None)
    desired = [items(value) for value in journal["desired"]]
    sequences, representatives = identify(*desired, expected[0].items, expected[1].items,
                                          current[0].items, current[1].items)
    ds, dd, es, ed, cs, cd = sequences
    wanted = set(ds) | set(dd)
    removed = (set(es) - set(cs)) | (set(ed) - set(cd))
    added = (set(cs) - set(es)) | (set(cd) - set(ed))
    if rule.deletion_policy == "mirror":
        wanted = (wanted - removed) | added
    else:
        wanted |= set(cs) | set(cd)
    authority = cs if rule.order_authority == "source" else cd
    ordered = [key for key in authority if key in wanted]
    seen = set(ordered)
    ordered += [key for key in ds + dd if key in wanted and key not in seen and not seen.add(key)]
    ordered += sorted(wanted - set(ordered))
    outputs = []
    for snapshot, keys in zip(current, (cs, cd)):
        local = dict(zip(keys, snapshot.items))
        outputs.append([Item(representatives[key].media_type, dict(representatives[key].ids),
            added_at=local[key].added_at if key in local else None,
            rank=local[key].rank if key in local else 0,
            entry_id=local[key].entry_id if key in local else None) for key in ordered])
    for snapshot, target in zip(current, outputs):
        validate_deletions(rule, snapshot.items, target)
    baseline = {"fingerprint": rule.fingerprint, "source": records(outputs[0]), "destination": records(outputs[1])}
    return Plan(*outputs, baseline, True, True)