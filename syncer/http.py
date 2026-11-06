"""Small JSON transport with secret-safe failures and bounded, selective retries."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import math
import random
import re
from threading import Lock
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model import SyncError


class ApiError(SyncError):
    """A transport failure containing no response body, URL, or credentials."""

    def __init__(self, message="API request failed", status=None, outcome=None):
        super().__init__(message)
        self.status = status
        self.outcome = outcome


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib would forward Authorization headers, including across origins.
        return None


# Module-level entry point makes offline tests straightforward without global
# urllib handlers or changes to TLS certificate verification.
urlopen = build_opener(_NoRedirect()).open


def _safe_path(path):
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise SyncError("API path must be relative to its configured HTTPS origin")
    if any(c in path for c in "?#%\\") or any(ord(c) <= 32 for c in path):
        raise SyncError("Invalid API path")
    if any(part in {".", ".."} for part in path.split("/")):
        raise SyncError("Invalid API path")


def _retryable_429(method, path):
    """Only reads and known list writes may replay an explicit rejection."""
    if method == "GET":
        return True
    if method == "PUT":
        return bool(re.fullmatch(r"/lists/[1-9][0-9]*", path)
                    or re.fullmatch(r"/users/[^/]+/lists/[^/]+", path))
    if method != "POST":
        return False
    # No mutation may replay an ambiguous failure, even a membership/rank write.
    # OAuth and list creation must not replay even a known rejection.
    return bool(re.fullmatch(r"/lists/[1-9][0-9]*/items/(?:add|remove|reorder)", path)
                or re.fullmatch(r"/users/[^/]+/lists/[^/]+/items(?:/(?:remove|reorder))?", path))


def _delay(headers, attempt, status=None):
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = (date - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = None
    # Never violate a server's long Retry-After by retrying before its reset.
    # The ceiling bounds this run, not the provider's requested quiet period.
    if seconds is not None and seconds > 120:
        error = ApiError("API retry delay exceeds 120 seconds; retry in a later run",
                         status, "rejected")
        error.retry_after = seconds
        raise error from None
    if seconds is None or not math.isfinite(seconds):
        seconds = random.uniform(2 ** attempt, 2 ** (attempt + 1))
    return max(0, seconds)


class Http:
    """JSON HTTP client; 30-second sends, at most three safe retry attempts.

    Share one authenticated Trakt instance across rules for that user: write
    pacing belongs to this instance, never to global process state. Ambiguous
    mutations abort so a later run can reconcile fresh provider state first.
    """

    def __init__(self, base, headers=None, params=None):
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme == "https" and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment
                     and not any(ord(c) <= 32 for c in base))
            trakt_origin = parsed.hostname == "api.trakt.tv" and parsed.port in {None, 443}
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise SyncError("API base must be a credential-free HTTPS URL")
        self.base = base.rstrip("/")
        self.headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
        self.headers.update(headers or {})
        self.params = dict(params or {})
        self._trakt_origin = trakt_origin
        self._write_lock = Lock()
        self._last_write_started = None
        self._retry_not_before = 0

    def _pace_write(self, method, headers):
        if (not self._trakt_origin or method not in {"POST", "PUT", "DELETE"}
                or not any(key.lower() == "authorization" and value
                           for key, value in headers.items())):
            return
        # Reserve every send attempt, including rejected or unknown writes.
        # Reads never acquire this lock or inherit a write's quiet period.
        with self._write_lock:
            now = time.monotonic()
            if self._last_write_started is not None:
                remaining = 1 - (now - self._last_write_started)
                while remaining > 0:
                    time.sleep(remaining)
                    now = time.monotonic()
                    remaining = 1 - (now - self._last_write_started)
            self._last_write_started = now

    def request(self, method, path, params=None, body=None):
        _safe_path(path)
        if time.monotonic() < self._retry_not_before:
            raise ApiError("API account quiet period is active; retry in a later run", 429, "rejected")
        method = method.upper()
        query = dict(self.params)
        query.update(params or {})
        headers = dict(self.headers)
        try:
            data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
            if data is not None:
                headers["Content-Type"] = "application/json"
            url = self.base + path
            if query:
                url += "?" + urlencode(query, doseq=True)
            req = Request(url, data=data, headers=headers, method=method)
        except (TypeError, ValueError):
            raise ApiError("Invalid API request") from None
        mutation = method in {"POST", "PUT", "DELETE"}
        for attempt in range(3):
            self._pace_write(method, headers)
            try:
                with urlopen(req, timeout=30) as response:
                    raw = response.read()
                    response_headers = dict(response.headers.items())
                try:
                    result = json.loads(raw) if raw else None
                except (ValueError, UnicodeError):
                    if mutation:
                        raise ApiError("API mutation outcome unknown after invalid JSON; "
                                       "reconcile before retry", outcome="unknown") from None
                    raise ApiError("API returned invalid JSON") from None
                return result, response_headers
            except HTTPError as exc:
                status = exc.code
                response_headers = dict((exc.headers or {}).items())
                exc.close()
                if status == 429 and (attempt == 2 or not _retryable_429(method, path)):
                    try:
                        delay = _delay(response_headers, attempt, status)
                    except ApiError as error:
                        # Do not lose an unbounded server quiet period between
                        # independent rules sharing this authenticated client.
                        self._retry_not_before = time.monotonic() + error.retry_after
                        raise
                    self._retry_not_before = max(self._retry_not_before, time.monotonic() + delay)
                if mutation and 500 <= status <= 599:
                    raise ApiError(f"API mutation outcome unknown (HTTP {status}); "
                                   "reconcile before retry", status, "unknown") from None
                retry = ((status == 429 and _retryable_429(method, path))
                         or (method == "GET" and 500 <= status <= 599))
                if retry and attempt < 2:
                    try:
                        delay = _delay(response_headers, attempt, status)
                    except ApiError as error:
                        if status == 429:
                            self._retry_not_before = time.monotonic() + error.retry_after
                        raise
                    time.sleep(delay)
                    continue
                raise ApiError(f"API request failed (HTTP {status})", status,
                               "rejected" if 400 <= status <= 499 else None) from None
            except (URLError, OSError, HTTPException, ValueError):
                if mutation:
                    raise ApiError("API mutation outcome unknown after connection or response failure; "
                                   "reconcile before retry", outcome="unknown") from None
                if method == "GET" and attempt < 2:
                    time.sleep(_delay({}, attempt))
                    continue
                raise ApiError("API connection failed or timed out") from None