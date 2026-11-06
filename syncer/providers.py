"""Complete list snapshots and batched movie/show mutations, with no ID lookups.

Snapshots default to newest-first addition order. ``order='rank'`` instead
returns actual custom order for post-write verification. Mutation callers must
preflight destinations and reread after writes (the runner owns convergence).
"""

from datetime import datetime, timezone
import sys
import time

from .config import parse_url
from .http import ApiError
from .model import Item, Snapshot, SyncError, identify


class _SnapshotChanged(SyncError):
    """A well-formed list changed while being read; discard and restart its read."""


class _SnapshotOverlap(SyncError):
    """Ambiguous ranks/entry IDs; retryable only after our acknowledged write."""


class _IdentityIndex:
    """Incremental typed-ID components; preserve aliases without quadratic scans.

    Each alias points at its component record. Union by size bounds reassignment;
    conflicting IDs within one component are terminal, never hidden as overlap.
    """

    def __init__(self):
        self.by_alias = {}
        self.records = {}
        self.next_key = 0

    def record(self, item, action=None):
        aliases = item.aliases()
        keys = {self.by_alias[alias] for alias in aliases if alias in self.by_alias}
        merged = {}
        for key in keys:
            merged.update(self.records[key]["aliases"])
        merged.update({alias: True for alias in aliases})
        ids = {}
        for alias in merged:
            _, provider, value = alias.split(":", 2)
            if provider in ids and ids[provider] != value:
                raise SyncError("Conflicting provider IDs; refusing ambiguous match")
            ids[provider] = value
        if keys:
            key = max(keys, key=lambda k: len(self.records[k]["aliases"]))
            record = self.records[key]
            if action is None:
                actions = {self.records[k]["action"] for k in keys}
                if len(actions) > 1:
                    raise SyncError("Conflicting pending membership expectations")
            for other in keys - {key}:
                self.records.pop(other)
        else:
            key = self.next_key
            self.next_key += 1
            record = {"aliases": {}, "action": action, "item": item}
            self.records[key] = record
        record["aliases"] = merged
        record["item"] = Item(item.media_type, ids)
        if action is not None:
            record["action"] = action
        for alias in merged:
            self.by_alias[alias] = key

    def items(self, action):
        return [record["item"] for record in self.records.values() if record["action"] == action]


def _object(value):
    if not isinstance(value, dict):
        raise SyncError("API returned an invalid object")
    return value


def _integer(value, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SyncError("API returned an invalid count or rank")
    if isinstance(value, str) and not value.isdigit():
        raise SyncError("API returned an invalid count or rank")
    number = int(value)
    if number < minimum:
        raise SyncError("API returned an invalid count or rank")
    return number


def _headers(headers):
    return {key.lower(): value for key, value in headers.items()}


def _order(order):
    if order not in {"added", "rank"}:
        raise SyncError("Snapshot order must be added or rank")


def _timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError):
        raise SyncError("API returned an invalid addition timestamp") from None


def _newest_first(items):
    if not items:
        return items
    if all(item.added_at is None for item in items):
        return sorted(items, key=lambda item: item.rank)
    if any(item.added_at is None for item in items):
        raise SyncError("Incomplete addition dates; refusing uncertain ordering")
    # Stable timestamp sort preserves ascending rank for simultaneous additions.
    return sorted(sorted(items, key=lambda item: item.rank),
                  key=lambda item: _timestamp(item.added_at), reverse=True)


def _ids(item):
    item.aliases()  # Validate type and every available supported identifier.
    return {key: (str(value).strip().lower() if key == "imdb" else int(value))
            for key, value in item.ids.items()
            if key in {"imdb", "tmdb", "trakt", "tvdb"} and value is not None and value != ""}


def _has_value(value):
    if isinstance(value, dict):
        return any(_has_value(v) for v in value.values())
    return bool(value)


def _write_result(result, expected, field, allow_existing=False):
    result = _object(result)
    if _has_value(result.get("not_found")) or _has_value(result.get("error")):
        raise SyncError("API could not resolve every requested item; verify before retrying")
    counts = _object(result.get(field))
    existing = _object(result.get("existing", {})) if allow_existing else {}
    for kind in ("movies", "shows", "seasons", "episodes"):
        count = _integer(counts.get(kind, 0)) + _integer(existing.get(kind, 0))
        if count != expected.get(kind, 0):
            raise SyncError("API reported a partial list write; verify before retrying")


def _same_membership(desired, actual):
    (wanted, found), _ = identify(desired, actual)
    if set(wanted) != set(found):
        raise SyncError("List membership changed before reorder; refusing stale order")
    return wanted, found


class _Provider:
    supports_description = False
    description_format = "plain"
    provider = ""

    def __init__(self, url, http, user=None):
        provider, self.owner, self.slug = parse_url(url)
        if provider != self.provider:
            raise SyncError("List URL does not match provider")
        self.url = url.rstrip("/")
        self.http = http
        self.user = user

    def snapshot(self, order="added"):
        """At most three complete reads; never replay mutations or API failures.

        Each _snapshot_once call owns fresh metadata, pages, items and cursors.
        Retry only explicitly classified list changes, leaving malformed responses
        and exhausted transport/rate-limit errors terminal. No partial read escapes.
        """
        return self._read_snapshot(order, (1, 2))

    def _read_snapshot(self, order, delays, *, settling=False):
        _order(order)
        attempts = len(delays) + 1
        for attempt in range(attempts):
            try:
                result = self._snapshot_once(order)
                if settling:
                    self._verify_pending_write(result)
                return result
            except (_SnapshotChanged, _SnapshotOverlap) as exc:
                if isinstance(exc, _SnapshotOverlap) and not settling:
                    raise
                if attempt == attempts - 1:
                    raise
                reason = "write verification not yet consistent" if settling else "list changed during read"
                print(f"WARNING: {self.provider} {reason}; "
                      f"restarting full snapshot ({attempt + 2}/{attempts})", file=sys.stderr)
                time.sleep(delays[attempt])

    def _snapshot(self, metadata, items, count):
        if len(items) != count:
            raise SyncError("List snapshot count mismatch; refusing incomplete snapshot")
        identify(items)
        return Snapshot(self.url, metadata.get("name", ""), metadata.get("description") or "",
                        items, str(metadata.get("updated_at") or metadata.get("updated") or ""),
                        provider=self.provider, metadata=metadata)

    def _mutate(self, items, action):
        items = list(items)
        identify(items)  # Validate the ENTIRE input before the first write.
        payloads = [(item.media_type + "s", _ids(item)) for item in items]
        for offset in range(0, len(payloads), 100):
            body, expected = {}, {}
            for kind, ids in payloads[offset:offset + 100]:
                body.setdefault(kind, []).append({"ids": ids} if self.provider == "trakt" else ids)
                expected[kind] = expected.get(kind, 0) + 1
            if self.provider == "mdblist":
                path = self._id_path() + "/items/" + action
                field = "added" if action == "add" else "removed"
            else:
                path = self.path + "/items" + ("/remove" if action == "remove" else "")
                field = "added" if action == "add" else "deleted"
            result, _ = self.http.request("POST", path, body=body)
            _write_result(result, expected, field, allow_existing=action == "add")
            self._acknowledge_membership(items[offset:offset + 100], action, result)

    def _acknowledge_membership(self, items, action, result=None):
        """Only Trakt currently needs a read-after-write visibility barrier."""

    def add(self, items):
        self._mutate(items, "add")

    def remove(self, items):
        self._mutate(items, "remove")

    def ensure_rank_sort(self, snapshot):
        """Provider has no separate default view to update (MDB reorder sets it)."""
        return False

    def creator(self, snapshot):
        """Stable username is always available; providers can resolve display names."""
        self._check_snapshot(snapshot)
        return self.owner

    def _check_snapshot(self, snapshot):
        if snapshot.url.rstrip("/") != self.url:
            raise SyncError("Preflight snapshot belongs to a different list")
        return _object(snapshot.metadata)


class MDBList(_Provider):
    provider = "mdblist"

    def __init__(self, url, http, user=None):
        super().__init__(url, http, user)
        self.path = f"/lists/{self.owner}/{self.slug}"
        self.list_id = None
        self._creator_name = None

    def creator(self, snapshot):
        self._check_snapshot(snapshot)
        if self._creator_name is None:
            value, _ = self.http.request("GET", f"/users/{self.owner}")
            user = _object(_object(value).get("user"))
            if not isinstance(user.get("username"), str) or user["username"].casefold() != self.owner:
                raise SyncError("Source creator profile does not match list owner")
            self._creator_name = user.get("name") or self.owner
            if not isinstance(self._creator_name, str):
                raise SyncError("Source creator display name is invalid")
        return self._creator_name

    def _metadata(self):
        value, _ = self.http.request("GET", self.path)
        # Actual public metadata is a singleton array, although schema says object.
        if isinstance(value, list):
            if len(value) != 1:
                raise SyncError("List metadata was missing or ambiguous")
            value = value[0]
        value = _object(value)
        self.list_id = _integer(value.get("id"), 1)
        _integer(value.get("items"))
        return value

    def _id_path(self):
        if self.list_id is None:
            self._metadata()
        return f"/lists/{_integer(self.list_id, 1)}"

    @staticmethod
    def _stable(metadata):
        return {key: metadata.get(key) for key in
                ("id", "name", "description", "items", "user_id", "user_name", "dynamic",
                 "updated_at", "updated", "last_updated", "sort", "sort_by", "sort_order", "private")}

    def _item(self, row, kind=None):
        row = _object(row)
        kind = row.get("mediatype", row.get("type", kind))
        if kind not in {"movie", "show"}:
            raise SyncError("Only movie/show items are supported; refusing partial sync")
        ids = dict(_object(row.get("ids", {})))
        for provider in ("imdb", "tmdb", "trakt", "tvdb"):
            if ids.get(provider) in (None, "") and row.get(provider + "_id") not in (None, ""):
                ids[provider] = row[provider + "_id"]
        if not ids.get("tmdb") and row.get("id"):
            ids["tmdb"] = row["id"]
        return Item(kind, ids, row.get("added_at"), _integer(row.get("rank", 0)))

    def _snapshot_once(self, order):
        _order(order)
        before = self._metadata()
        count = _integer(before["items"])
        items, seen, cursor = [], set(), None
        while True:
            params = {"limit": 1000, "unified": "true", "sort": "added" if order == "added" else "usort",
                      "order": "asc"}
            if cursor is not None:
                params["cursor"] = cursor
            value, headers = self.http.request("GET", self._id_path() + "/items", params=params)
            headers = _headers(headers)
            if isinstance(value, list):
                rows = [self._item(row) for row in value]
                total = _integer(headers.get("x-total-items"))
                more = str(headers.get("x-has-more", "")).lower()
                if more not in {"true", "false"}:
                    raise SyncError("MDBList pagination headers are incomplete")
                more = more == "true"
                next_cursor = headers.get("x-next-cursor")
            else:
                value = _object(value)
                pagination = _object(value.get("pagination"))
                total = _integer(pagination.get("total"))
                more = pagination.get("has_more")
                if not isinstance(more, bool):
                    raise SyncError("MDBList pagination is incomplete")
                next_cursor = pagination.get("next_cursor")
                if "items" in value:
                    if not isinstance(value["items"], list):
                        raise SyncError("MDBList returned invalid items")
                    rows = [self._item(row) for row in value["items"]]
                else:
                    rows, nonempty = [], 0
                    for bucket, kind in (("movies", "movie"), ("shows", "show"),
                                         ("seasons", "season"), ("episodes", "episode")):
                        bucket_items = value.get(bucket, [])
                        if not isinstance(bucket_items, list):
                            raise SyncError("MDBList returned invalid item buckets")
                        nonempty += bool(bucket_items)
                        rows.extend(self._item(row, kind) for row in bucket_items)
                    if nonempty > 1:
                        raise SyncError("Mixed media requires unified MDBList ordering")
            if not more:
                if next_cursor:
                    raise SyncError("MDBList pagination end is inconsistent")
            elif not rows or not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise SyncError("MDBList pagination cursor is missing or repeated")
            for item in rows:
                item.aliases()
            if len(rows) > total:
                raise SyncError("MDBList page contains more items than its declared total")
            if total != count:
                # Drift cannot turn invalid observed identities/cursors into a
                # retryable read. Validate collected data before abandoning it.
                identify(items + rows)
                raise _SnapshotChanged("MDBList pagination count changed during snapshot")
            if len(items) + len(rows) > count:
                raise SyncError("MDBList pagination contains excess items")
            items.extend(rows)
            if not more:
                break
            seen.add(next_cursor)
            cursor = next_cursor
        if len(items) != count:
            raise SyncError("List snapshot count mismatch; refusing incomplete snapshot")
        identify(items)
        after = self._metadata()
        if self._stable(before) != self._stable(after):
            raise _SnapshotChanged("List metadata changed during snapshot; retry next run")
        # MDBList's natural order=asc for added is NEWEST first, matching its
        # visible Date Added view. Preserve server ties/cursors even with null dates.
        return self._snapshot(before, items, count)

    def preflight(self, snapshot):
        metadata = self._check_snapshot(snapshot)
        if metadata.get("dynamic") is not False:
            raise SyncError("MDBList destination must be explicitly static")
        account, _ = self.http.request("GET", "/user")
        account = _object(account)
        owner_id = _integer(metadata.get("user_id"), 1)
        if _integer(account.get("user_id"), 1) != owner_id:
            raise SyncError("MDBList destination is not owned by authenticated account")
        if account.get("username") and metadata.get("user_name") and str(account["username"]).casefold() != str(metadata["user_name"]).casefold():
            raise SyncError("MDBList destination ownership metadata is inconsistent")
        self.list_id = _integer(metadata.get("id"), 1)
        snapshot.writable = True

    def reorder(self, items, snapshot):
        items = list(items)
        if not items:
            return
        _same_membership(items, snapshot.items)
        encoded = [{"mediatype": item.media_type, "ids": _ids(item)} for item in items]
        for offset in range(0, len(encoded), 1000):
            batch = encoded[offset:offset + 1000]
            body = {"items": batch}
            if offset:
                body["after"] = encoded[offset - 1]
            else:
                body["position"] = "top"
            result, _ = self.http.request("POST", self._id_path() + "/items/reorder", body=body)
            result = _object(result)
            if _has_value(result.get("not_found")) or _has_value(result.get("error")):
                raise SyncError("MDBList could not reorder every requested item")
            # 'updated' counts changed positions, not accepted items. The runner
            # verifies the final custom order; zero is valid for unchanged rows.
            if _integer(result.get("updated")) > len(batch):
                raise SyncError("MDBList returned an invalid reorder count")


class Trakt(_Provider):
    provider = "trakt"
    supports_description = True
    description_format = "markdown"

    def creator(self, snapshot):
        metadata = self._check_snapshot(snapshot)
        user = _object(metadata.get("user", {}))
        # Owner identity comes from the validated source URL, never authenticated
        # destination credentials or arbitrary profile URLs supplied by metadata.
        names = [user.get("username"), _object(user.get("ids", {})).get("slug")]
        if user and not any(isinstance(name, str) and name.casefold() == self.owner for name in names):
            raise SyncError("Source creator metadata does not match list owner")
        return user.get("name") or self.owner

    def ensure_description(self, snapshot, description):
        """Description-only, ownership checked and reread verified; no other fields."""
        self.preflight(snapshot)
        if not isinstance(description, str) or len(description) > 10000:
            raise SyncError("Invalid or oversized Trakt description")
        if snapshot.description == description:
            return False
        current = self._metadata()
        if (current.get("description") or "") != snapshot.description:
            raise SyncError("Destination description changed concurrently; refusing overwrite")
        if current.get("privacy") != snapshot.metadata.get("privacy"):
            raise SyncError("Destination visibility changed concurrently; refusing source disclosure")
        self.http.request("PUT", self.path, body={"description": description})
        current = self._metadata()
        if current.get("description") != description:
            raise SyncError("Destination description verification failed; baseline not advanced")
        snapshot.description = description
        snapshot.metadata = current
        snapshot.version = str(current.get("updated_at") or "")
        return True

    def __init__(self, url, http, user):
        super().__init__(url, http, user)
        self.path = f"/users/{self.owner}/lists/{self.slug}"
        self._pending_membership = _IdentityIndex()
        self._pending_order = None
        self._last_snapshot = None
        self._write_base = None
        self._write_summary = None
        self._confirmed_write = None

    def _record_write_summary(self, result):
        summary = result.get("list") if result is not None else None
        self._write_summary = None
        if summary is not None:
            summary = _object(summary)
            count = _integer(summary.get("item_count"))
            updated = summary.get("updated_at")
            _timestamp(updated)
            self._write_summary = {"item_count": count, "updated_at": updated}

    def _acknowledge_membership(self, items, action, result=None):
        if not self._pending_membership.records:
            self._write_base = self._last_snapshot
        self._record_write_summary(result)
        self._confirmed_write = None
        for item in items:
            self._pending_membership.record(item, action)
        self._pending_order = None

    def snapshot(self, order="added"):
        pending = bool(self._pending_membership.records or self._pending_order is not None)
        if not pending:
            result = super().snapshot(order)
        else:
            result = self._read_snapshot(order, (2, 5, 10, 20), settling=True)
            if result.metadata["item_count"] != len(result.items):
                self._confirmed_write = (result.items, dict(self._write_summary))
            self._pending_membership = _IdentityIndex()
            self._pending_order = None
            self._write_base = None
            self._write_summary = None
        if result.metadata["item_count"] == len(result.items):
            self._confirmed_write = None
        self._last_snapshot = result
        return result

    def _verify_pending_write(self, snapshot):
        sequences, _ = identify(snapshot.items, self._pending_membership.items("add"),
                                self._pending_membership.items("remove"))
        present, added, removed = map(set, sequences)
        if not added <= present or removed & present:
            raise _SnapshotChanged("Trakt acknowledged membership write is not yet visible")
        if self._write_base is not None:
            (base, current, additions, removals), _ = identify(
                self._write_base.items, snapshot.items,
                self._pending_membership.items("add"), self._pending_membership.items("remove"))
            if set(current) != (set(base) | set(additions)) - set(removals):
                raise _SnapshotChanged("Trakt membership differs from acknowledged write expectation")
        if self._write_summary is not None and self._write_summary["item_count"] != len(snapshot.items):
            raise _SnapshotChanged("Trakt acknowledged count is not yet visible")
        if self._pending_order is not None:
            ranked = sorted(snapshot.items, key=lambda item: item.rank)
            if [item.entry_id for item in ranked] != self._pending_order:
                raise _SnapshotChanged("Trakt acknowledged rank order is not yet visible")

    def _check_write_metadata(self, metadata, baseline):
        # Membership writes legitimately change count/update time, but do not
        # authorize unrelated visibility, description, owner or sort edits.
        keys = set(self._stable(metadata)) - {"item_count", "updated_at"}
        if any(metadata.get(key) != baseline.metadata.get(key) for key in keys):
            raise _SnapshotChanged("Trakt metadata changed outside the acknowledged write")

    def _metadata(self):
        metadata, _ = self.http.request("GET", self.path)
        metadata = _object(metadata)
        _integer(metadata.get("item_count"))
        return metadata

    @staticmethod
    def _stable(metadata):
        return {key: metadata.get(key) for key in
                ("ids", "name", "description", "item_count", "updated_at", "user", "privacy",
                 "sort_by", "sort_how")}

    def _item(self, row):
        row = _object(row)
        kind = row.get("type")
        if kind not in {"movie", "show"}:
            raise SyncError("Only movie/show items are supported; refusing partial sync")
        ids = _object(_object(row.get(kind)).get("ids"))
        return Item(kind, dict(ids), row.get("listed_at"), _integer(row.get("rank")),
                    _integer(row.get("id"), 1))

    @staticmethod
    def _sequence(items):
        return [(item.entry_id, item.rank, tuple(sorted(item.aliases())), item.added_at)
                for item in items]

    def _read_items(self, requested_limit, order):
        """Validate one complete explicit-rank traversal using effective headers."""
        items, page, page_count = [], 1, None
        count, effective_limit = None, None
        ranks, entries = set(), set()
        identities = _IdentityIndex()
        dates_present = None
        while True:
            value, headers = self.http.request("GET", self.path + "/items/movie,show,season,episode/rank/asc",
                params={"page": page, "limit": requested_limit})
            if not isinstance(value, list):
                raise SyncError("Trakt returned invalid list items")
            headers = _headers(headers)
            returned_page = _integer(headers.get("x-pagination-page"), 1)
            pages = _integer(headers.get("x-pagination-page-count"))
            total = _integer(headers.get("x-pagination-item-count"))
            limit = _integer(headers.get("x-pagination-limit"), 1)
            if returned_page != page:
                raise SyncError("Trakt returned an unexpected page number")
            if limit > requested_limit:
                raise SyncError("Trakt returned an unexpected effective page limit")
            if (headers.get("x-sort-by", "rank") != "rank"
                    or headers.get("x-sort-how", "asc") != "asc"):
                raise SyncError("Trakt did not return the requested rank order")
            if (total and pages == 0) or (not total and pages > 1) or pages > max(1, total):
                raise SyncError("Trakt returned inconsistent pagination")
            rows = [self._item(row) for row in value]
            if len(rows) > total:
                raise SyncError("Trakt page contains more items than its declared total")
            if not rows and page < pages:
                raise SyncError("Trakt pagination ended before the final page")
            for item in rows:
                identities.record(item)
                if item.added_at is not None:
                    _timestamp(item.added_at)
                if order == "added":
                    has_date = item.added_at is not None
                    if dates_present is not None and dates_present != has_date:
                        raise SyncError("Incomplete addition dates; refusing uncertain ordering")
                    dates_present = has_date
            for item in rows:
                if item.rank in ranks or item.entry_id in entries:
                    raise _SnapshotOverlap("Trakt returned duplicate ranks or list-entry IDs")
                ranks.add(item.rank)
                entries.add(item.entry_id)
            if count is not None and (total != count or pages != page_count or limit != effective_limit):
                identify(items + rows)
                if order == "added":
                    _newest_first(items + rows)
                raise _SnapshotChanged(f"Trakt pagination changed during snapshot "
                    f"(page={page}, expected_total={count}, observed_total={total}, "
                    f"expected_pages={page_count}, observed_pages={pages})")
            count, effective_limit = total, limit
            page_count = pages
            if pages != (count + limit - 1) // limit and not (count == 0 and pages == 1):
                raise SyncError("Trakt returned inconsistent effective pagination")
            expected_rows = min(limit, max(0, count - (page - 1) * limit))
            if len(rows) != expected_rows:
                raise SyncError("Trakt page row count does not match effective pagination")
            if any(item.rank != len(items) + offset for offset, item in enumerate(rows, 1)):
                raise _SnapshotOverlap("Trakt rank sequence has gaps or is not ascending")
            items.extend(rows)
            if len(items) > count:
                raise SyncError("Trakt pagination contains excess items")
            if page >= page_count:
                break
            if not rows:
                raise SyncError("Trakt pagination ended before the final page")
            page += 1
        if len(items) != count:
            raise SyncError("List snapshot count mismatch; refusing incomplete snapshot")
        identify(items)
        return items, count, effective_limit, page_count

    def _snapshot_once(self, order):
        _order(order)
        before = self._metadata()
        items, count, limit, pages = self._read_items(250, order)
        # Different page boundaries expose a stable page-keyed hybrid that a
        # repeated identical request could reproduce indefinitely. Still not an
        # isolation guarantee; the API provides no revision-pinned pagination.
        pending = bool(self._pending_membership.records or self._pending_order is not None)
        summary_mismatch = count != before["item_count"]
        if (summary_mismatch and self._confirmed_write is None
                and not (pending and self._write_base is not None and self._write_summary is not None)):
            raise _SnapshotChanged(f"Trakt metadata/items count disagreement "
                f"(metadata_total={before['item_count']}, items_total={count})")
        confirm = pages > 1 or summary_mismatch
        if confirm:
            requested = min(100, max(1, limit - 1))
            checked, checked_count, checked_limit, _ = self._read_items(requested, order)
            if checked_count != count or self._sequence(items) != self._sequence(checked):
                raise _SnapshotChanged("Trakt independent partition confirmation changed")
            if pages > 1 and checked_limit == limit:
                raise SyncError("Trakt cannot independently confirm the paginated partition")
        after = self._metadata()
        if self._stable(before) != self._stable(after):
            raise _SnapshotChanged("List metadata changed during snapshot; retry next run")
        result = self._snapshot(before, items if order == "rank" else _newest_first(items), count)
        if summary_mismatch:
            if pending and self._write_base is not None and self._write_summary is not None:
                self._check_write_metadata(before, self._write_base)
                self._verify_pending_write(result)
            elif self._confirmed_write is not None:
                previous, summary = self._confirmed_write
                self._check_write_metadata(before, self._last_snapshot)
                if (summary["item_count"] != count
                        or self._sequence(items) != self._sequence(sorted(previous, key=lambda item: item.rank))):
                    raise _SnapshotChanged("Trakt confirmed write evidence no longer matches")
            else:
                raise _SnapshotChanged(f"Trakt metadata/items count disagreement "
                    f"(metadata_total={before['item_count']}, items_total={count})")
        return result

    def preflight(self, snapshot):
        metadata = self._check_snapshot(snapshot)
        if not isinstance(self.user, str) or self.user.casefold() != self.owner.casefold():
            raise SyncError("Trakt destination is not owned by configured authenticated user")
        owner = metadata.get("user")
        if owner is not None:
            owner = _object(owner)
            names = [owner.get("username"), _object(owner.get("ids", {})).get("slug")]
            if not any(isinstance(name, str) and name.casefold() == self.user.casefold() for name in names):
                raise SyncError("Trakt destination ownership metadata is inconsistent")
        snapshot.writable = True

    def ensure_rank_sort(self, snapshot):
        """Keep rank 1 at the top of Trakt's default view without touching members."""
        self.preflight(snapshot)
        desired = {"sort_by": "rank", "sort_how": "asc"}
        if all(snapshot.metadata.get(key) == value for key, value in desired.items()):
            return False
        self.http.request("PUT", self.path, body=desired)
        current = self._metadata()
        if not all(current.get(key) == value for key, value in desired.items()):
            raise SyncError("Trakt default rank sort verification failed; baseline not advanced")
        snapshot.metadata = current
        snapshot.version = str(current.get("updated_at") or "")
        return True

    def reorder(self, items, snapshot):
        items = list(items)
        if not items:
            return
        identify(items)
        self._check_snapshot(snapshot)
        current = snapshot
        wanted, found = _same_membership(items, current.items)
        entries = {key: _integer(item.entry_id, 1) for key, item in zip(found, current.items)}
        ranks = [entries[key] for key in wanted]
        if len(set(ranks)) != len(ranks):
            raise SyncError("Trakt list-entry IDs are ambiguous")
        result, _ = self.http.request("POST", self.path + "/items/reorder", body={"rank": ranks})
        result = _object(result)
        if (_has_value(result.get("not_found")) or _has_value(result.get("error"))
                or _has_value(result.get("skipped_ids")) or _integer(result.get("updated")) != len(items)):
            raise SyncError("Trakt reported a partial reorder; verify before retrying")
        self._record_write_summary(result)
        self._confirmed_write = None
        self._write_base = current
        self._pending_order = list(ranks)


def make_provider(url, http, user):
    provider, _, _ = parse_url(url)
    return MDBList(url, http, user) if provider == "mdblist" else Trakt(url, http, user)