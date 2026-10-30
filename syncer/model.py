"""Provider-neutral models and unambiguous cross-provider identity resolution."""

from dataclasses import dataclass, field


class SyncError(Exception):
    """An actionable failure safe to display without secrets."""


@dataclass
class Item:
    media_type: str
    ids: dict
    added_at: str | None = None
    rank: int = 0
    entry_id: int | None = None

    def aliases(self):
        if self.media_type not in {"movie", "show"}:
            raise SyncError("Only movie/show items are supported; refusing partial sync")
        aliases = set()
        for provider in ("imdb", "tmdb", "trakt", "tvdb"):
            value = self.ids.get(provider)
            if value is not None and value != "":
                text = str(value).strip().lower()
                if provider == "imdb":
                    if not text.startswith("tt") or not text[2:].isdigit():
                        raise SyncError("Invalid IMDb identity")
                elif not text.isdigit() or int(text) <= 0:
                    raise SyncError("Invalid numeric provider identity")
                else:
                    text = str(int(text))
                aliases.add(f"{self.media_type}:{provider}:{text}")
        if not aliases:
            raise SyncError("Media has no usable provider identity")
        return aliases


@dataclass
class Snapshot:
    url: str
    name: str
    description: str
    items: list[Item]
    version: str = ""
    writable: bool = False
    provider: str = ""
    metadata: dict = field(default_factory=dict)


def identify(*collections):
    """Union all shared IDs; reject contradictory IDs and duplicate list entries.

    Return parallel key sequences and a merged representative per identity.
    Union-find is essentially linear and never does title-based/per-item lookups.
    """
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for collection in collections:
        for item in collection:
            aliases = sorted(item.aliases())
            first = find(aliases[0])
            for alias in aliases[1:]:
                other = find(alias)
                if first != other:
                    parent[other] = first
    groups = {}
    for alias in parent:
        groups.setdefault(find(alias), set()).add(alias)
    representatives = {}
    for aliases in groups.values():
        identities = {}
        for alias in aliases:
            kind, provider, value = alias.split(":", 2)
            if provider in identities and identities[provider] != value:
                raise SyncError("Conflicting provider IDs; refusing ambiguous match")
            identities[provider] = value
        key = min(aliases)
        representatives[key] = Item(kind, identities)
    keys = []
    roots = {find(alias): min(aliases) for aliases in groups.values() for alias in aliases}
    for collection in collections:
        seq = [roots[find(next(iter(item.aliases()))) ] for item in collection]
        if len(seq) != len(set(seq)):
            raise SyncError("Duplicate media identities within list")
        keys.append(seq)
    return keys, representatives