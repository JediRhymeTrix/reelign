"""Complete list snapshots and batched movie/show mutations, with no ID lookups.

Snapshots default to newest-first addition order. ``order='rank'`` instead
returns actual custom order for post-write verification. Mutation callers must
preflight destinations and reread after writes (the runner owns convergence).
"""

from datetime import datetime, timezone

from .config import parse_url
from .http import ApiError
from .model import Item, Snapshot, SyncError, identify


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
    provider = ""

    def __init__(self, url, http, user=None):
        provider, self.owner, self.slug = parse_url(url)
        if provider != self.provider:
            raise SyncError("List URL does not match provider")
        self.url = url.rstrip("/")
        self.http = http
        self.user = user

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

    def add(self, items):
        self._mutate(items, "add")

    def remove(self, items):
        self._mutate(items, "remove")

    def ensure_rank_sort(self, snapshot):
        """Provider has no separate default view to update (MDB reorder sets it)."""
        return False

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

    def snapshot(self, order="added"):
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
            if total != count or len(items) + len(rows) > count:
                raise SyncError("MDBList pagination count changed during snapshot")
            items.extend(rows)
            if not more:
                if next_cursor:
                    raise SyncError("MDBList pagination end is inconsistent")
                break
            if not rows or not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise SyncError("MDBList pagination cursor is missing or repeated")
            seen.add(next_cursor)
            cursor = next_cursor
        after = self._metadata()
        if self._stable(before) != self._stable(after):
            raise SyncError("List metadata changed during snapshot; retry next run")
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

    def __init__(self, url, http, user):
        super().__init__(url, http, user)
        self.path = f"/users/{self.owner}/lists/{self.slug}"

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

    def snapshot(self, order="added"):
        _order(order)
        before = self._metadata()
        count = _integer(before["item_count"])
        items, page, page_count = [], 1, None
        while True:
            value, headers = self.http.request("GET", self.path + "/items",
                                               params={"page": page, "limit": 100})
            if not isinstance(value, list):
                raise SyncError("Trakt returned invalid list items")
            headers = _headers(headers)
            returned_page = _integer(headers.get("x-pagination-page"), 1)
            pages = _integer(headers.get("x-pagination-page-count"))
            total = _integer(headers.get("x-pagination-item-count"))
            if returned_page != page or total != count or (page_count is not None and pages != page_count):
                raise SyncError("Trakt pagination changed during snapshot")
            page_count = pages
            if (count and pages == 0) or (not count and pages > 1) or pages > max(1, count):
                raise SyncError("Trakt returned inconsistent pagination")
            rows = [self._item(row) for row in value]
            items.extend(rows)
            if len(items) > count:
                raise SyncError("Trakt pagination contains excess items")
            if page >= page_count:
                break
            if not rows:
                raise SyncError("Trakt pagination ended before the final page")
            page += 1
        after = self._metadata()
        if self._stable(before) != self._stable(after):
            raise SyncError("List metadata changed during snapshot; retry next run")
        if len({item.rank for item in items}) != len(items) or len({item.entry_id for item in items}) != len(items):
            raise SyncError("Trakt returned duplicate ranks or list-entry IDs")
        items = sorted(items, key=lambda item: item.rank) if order == "rank" else _newest_first(items)
        return self._snapshot(before, items, count)

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
        current = self.snapshot(order="rank")
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


def make_provider(url, http, user):
    provider, _, _ = parse_url(url)
    return MDBList(url, http, user) if provider == "mdblist" else Trakt(url, http, user)