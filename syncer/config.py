"""Strict, credential-free TOML configuration for ordered list reconciliation."""

from dataclasses import asdict, dataclass, field, fields
from graphlib import CycleError, TopologicalSorter
import hashlib
import json
import math
from pathlib import Path
import re
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import SyncError


DIRECTIONS = {"source_to_destination", "destination_to_source", "bidirectional"}
POLICIES = {"mirror", "additions_only"}
AUTHORITIES = {"source", "destination"}
_PROFILE_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
_MONTH_NAMES = {name: index for index, name in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_DAY_NAMES = {name: index for index, name in enumerate(
    ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}
_URL_PATTERNS = (
    ("mdblist", re.compile(r"https://mdblist\.com/lists/([A-Za-z0-9_-]+)/([A-Za-z0-9_-]+)/?")),
    ("trakt", re.compile(r"https://trakt\.tv/users/([A-Za-z0-9_-]+)/lists/([A-Za-z0-9_-]+)/?")),
)


def parse_url(url: str) -> tuple[str, str, str]:
    """Accept only direct HTTPS provider list URLs, never credential-bearing URLs.

    Matching the whole raw URL also rejects empty query/fragment markers, ports,
    escaped path separators, whitespace and URL-parser normalization surprises.
    A single optional trailing slash refers to the same endpoint.
    """
    if isinstance(url, str):
        for provider, pattern in _URL_PATTERNS:
            match = pattern.fullmatch(url)
            if match:
                return provider, match[1].casefold(), match[2].casefold()
    raise SyncError("Expected an HTTPS mdblist.com/lists/user/slug or trakt.tv/users/user/lists/slug URL")


def _validate_profile(name: str) -> None:
    if not isinstance(name, str) or _PROFILE_NAME.fullmatch(name) is None:
        raise SyncError("Schedule profile names must match ^[a-z][a-z0-9-]{0,39}$")


def _cron_field(value: str, low: int, high: int, names: dict[str, int]) -> set[int]:
    """Expand one standard cron field; do not accept provider-specific extensions."""
    def number(token):
        if token.upper() in names:
            return names[token.upper()]
        if re.fullmatch(r"[0-9]+", token) is None:
            raise SyncError("Invalid standard five-field cron expression")
        try:
            result = int(token)
        except ValueError:
            raise SyncError("Invalid standard five-field cron expression") from None
        if not low <= result <= high:
            raise SyncError("Cron field value is out of range")
        return result

    result = set()
    for term in value.split(","):
        parts = term.split("/")
        if len(parts) > 2:
            raise SyncError("Invalid cron step")
        step = 1
        if len(parts) == 2:
            if re.fullmatch(r"[0-9]+", parts[1]) is None:
                raise SyncError("Cron steps must be positive integers")
            try:
                step = int(parts[1])
            except ValueError:
                raise SyncError("Invalid cron step") from None
            if step < 1:
                raise SyncError("Cron steps must be positive integers")
        base = parts[0]
        if base == "*":
            start, end = low, high
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2:
                raise SyncError("Invalid cron range")
            start, end = number(bounds[0]), number(bounds[1])
            if start > end:
                raise SyncError("Cron ranges must be ascending")
        else:
            start = number(base)
            end = high if len(parts) == 2 else start
        result.update(range(start, end + 1, step))
    return result


def _validate_cron(expression: str) -> None:
    if not isinstance(expression, str) or any(ord(char) < 32 or ord(char) > 126
                                              for char in expression):
        raise SyncError("Cron must be a standard five-field expression or manual")
    tokens = expression.split()
    if len(tokens) != 5:
        raise SyncError("Cron must contain exactly five fields")
    ranges = ((0, 59, {}), (0, 23, {}), (1, 31, {}),
              (1, 12, _MONTH_NAMES), (0, 6, _DAY_NAMES))
    expanded = [_cron_field(token, low, high, names)
                for token, (low, high, names) in zip(tokens, ranges)]
    # Bound minute granularity conservatively, including the next-hour boundary.
    # This avoids schedules GitHub cannot honor, even with sparse hour/day fields.
    minutes = sorted(expanded[0])
    gaps = [right - left for left, right in zip(minutes, minutes[1:])]
    gaps.append(60 + minutes[0] - minutes[-1])
    if min(gaps) < 5:
        raise SyncError("Cron minute selections must be at least 5 minutes apart")


@dataclass(frozen=True)
class Schedule:
    cron: str | None = "17 * * * *"
    timezone: str = "UTC"

    def __post_init__(self):
        if self.cron == "manual":
            object.__setattr__(self, "cron", None)
        elif self.cron is not None:
            _validate_cron(self.cron)
        if (not isinstance(self.timezone, str)
                or re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", self.timezone) is None):
            raise SyncError("Schedule timezone must be an IANA timezone name")
        # UTC is always available, including Windows without installed tzdata.
        if self.timezone != "UTC":
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                raise SyncError("Unknown schedule timezone; install tzdata for IANA timezones") from None


@dataclass(frozen=True)
class Rule:
    id: str
    source: str
    destination: str
    direction: str
    deletion_policy: str
    order_authority: str = "source"
    allow_empty: bool = False
    max_delete_fraction: float = 0.5
    max_delete_count: int = 100
    enabled: bool = True
    profile: str = "default"
    attribution: bool = True

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id.strip():
            raise SyncError("Rule id must be a nonempty string")
        _validate_profile(self.profile)
        source = parse_url(self.source)
        destination = parse_url(self.destination)
        if source == destination:
            raise SyncError("Rule endpoints must be distinct lists")
        for name, allowed in (
            ("direction", DIRECTIONS), ("deletion_policy", POLICIES),
            ("order_authority", AUTHORITIES),
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or value not in allowed:
                raise SyncError(f"Invalid {name}; expected one of {', '.join(sorted(allowed))}")
        for name in ("allow_empty", "enabled", "attribution"):
            if type(getattr(self, name)) is not bool:
                raise SyncError(f"{name} must be a boolean")
        fraction = self.max_delete_fraction
        if (type(fraction) not in (int, float) or not 0 <= fraction <= 1
                or not math.isfinite(fraction)):
            raise SyncError("max_delete_fraction must be a finite number between 0 and 1")
        if type(self.max_delete_count) is not int or self.max_delete_count < 0:
            raise SyncError("max_delete_count must be a nonnegative integer")

    @property
    def fingerprint(self) -> str:
        """Hash original reconciliation fields, excluding enabled and scheduling."""
        values = self._legacy_values()
        del values["enabled"]
        return self._hash_values(values)

    @property
    def legacy_fingerprint(self) -> str:
        """Original dataclass hash, including enabled but never the new profile."""
        return self._hash_values(self._legacy_values())

    def matches_fingerprint(self, value: object) -> bool:
        """Accept current hash or exact legacy semantics with either enabled value."""
        if not isinstance(value, str):
            return False
        if value == self.fingerprint:
            return True
        values = self._legacy_values()
        for enabled in (True, False):
            values["enabled"] = enabled
            if value == self._hash_values(values):
                return True
        return False

    def _legacy_values(self) -> dict:
        values = asdict(self)
        del values["profile"]
        del values["attribution"]
        # TOML integers 0/1 are valid fractions with identical numeric semantics.
        values["max_delete_fraction"] = float(self.max_delete_fraction)
        return values

    @staticmethod
    def _hash_values(values: dict) -> str:
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Settings:
    rules: list[Rule] = field(default_factory=list)
    schedules: dict[str, Schedule] = field(default_factory=lambda: {"default": Schedule()})

    def __post_init__(self):
        if not isinstance(self.rules, list) or any(not isinstance(rule, Rule) for rule in self.rules):
            raise SyncError("Settings rules must be a list of rules")
        if not isinstance(self.schedules, dict):
            raise SyncError("Schedules must be named tables")
        for name, schedule in self.schedules.items():
            _validate_profile(name)
            if not isinstance(schedule, Schedule):
                raise SyncError("Schedules must contain valid schedule definitions")
        if any(rule.profile not in self.schedules for rule in self.rules):
            raise SyncError("Rule references an unknown schedule profile")


def _reject_rule_cycles(rules: list[Rule]) -> None:
    """Reject cross-rule feedback without treating an intentional pair as a cycle."""
    dependencies = {}
    for rule in rules:
        if not rule.enabled or rule.direction == "bidirectional":
            # Both endpoints of a bidirectional rule are exclusive writers, so
            # other rules can read them but cannot feed a path back into them.
            continue
        reader, writer = parse_url(rule.source), parse_url(rule.destination)
        if rule.direction == "destination_to_source":
            reader, writer = writer, reader
        dependencies.setdefault(writer, set()).add(reader)
    try:
        # Iterative O(vertices + edges); do not reorder the configured rules.
        TopologicalSorter(dependencies).prepare()
    except CycleError:
        raise SyncError("Enabled rules form a read/write cycle; use an explicit bidirectional rule") from None


def load_config(path: str | Path) -> list[Rule]:
    """Retain the original rule-list API while validating all settings."""
    return load_settings(path).rules


def load_settings(path: str | Path) -> Settings:
    """Read strict version 1 TOML with optional native schedule profiles."""
    try:
        with Path(path).open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, ValueError, TypeError):
        # Do not echo TOML lines, paths or URL input that could contain secrets.
        raise SyncError("Could not read valid TOML configuration") from None
    if set(config) - {"version", "rules", "schedules"} or {"version", "rules"} - set(config):
        raise SyncError("Configuration requires version and rules, with optional schedules only")
    if type(config["version"]) is not int or config["version"] != 1:
        raise SyncError("Configuration version must be integer 1")
    entries = config["rules"]
    if not isinstance(entries, list) or not entries:
        raise SyncError("Configuration rules must be a nonempty array of tables")
    schedule_entries = config.get("schedules", {})
    if not isinstance(schedule_entries, dict):
        raise SyncError("Schedules must be named tables")
    schedules = {"default": Schedule()}
    for name, entry in schedule_entries.items():
        _validate_profile(name)
        if not isinstance(entry, dict) or set(entry) - {"cron", "timezone"}:
            raise SyncError("Schedule has unknown fields or is not a table")
        schedules[name] = Schedule(**entry)
    allowed = {field.name for field in fields(Rule)}
    required = {"id", "source", "destination", "direction", "deletion_policy"}
    rules = []
    ids = set()
    writers = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - allowed or required - set(entry):
            raise SyncError("Rule has unknown or missing required keys")
        rule = Rule(**entry)
        if rule.id in ids:
            raise SyncError("Rule ids must be unique")
        ids.add(rule.id)
        if rule.enabled:
            endpoints = []
            if rule.direction != "source_to_destination":
                endpoints.append(parse_url(rule.source))
            if rule.direction != "destination_to_source":
                endpoints.append(parse_url(rule.destination))
            if writers.intersection(endpoints):
                raise SyncError("Multiple enabled rules cannot write the same endpoint")
            writers.update(endpoints)
        rules.append(rule)
    _reject_rule_cycles(rules)
    return Settings(rules=rules, schedules=schedules)
