"""Small JSON transport with secret-safe failures and bounded, selective retries."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .model import SyncError


class ApiError(SyncError):
    """A transport failure containing no response body, URL, or credentials."""

    def __init__(self, message="API request failed", status=None):
        super().__init__(message)
        self.status = status


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


def _retryable(method, path):
    if method in {"GET", "PUT"}:
        return True
    if method != "POST":
        return False
    # These operations set membership or rank. OAuth and list creation must
    # never be replayed after ambiguous failures.
    return bool(re.fullmatch(r"/lists/[1-9][0-9]*/items/(?:add|remove|reorder)", path)
                or re.fullmatch(r"/users/[^/]+/lists/[^/]+/items(?:/(?:remove|reorder))?", path))


def _delay(headers, attempt):
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
            seconds = 2 ** attempt
    # Never violate a server's long Retry-After by retrying before its reset.
    # The ceiling bounds this run, not the provider's requested quiet period.
    if seconds != seconds:
        seconds = 2 ** attempt
    if seconds > 120:
        raise ApiError("API retry delay exceeds 120 seconds; retry in a later run")
    return max(0, seconds)


class Http:
    """JSON HTTP client. At most three attempts, each with a 30-second timeout."""

    def __init__(self, base, headers=None, params=None):
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme == "https" and parsed.hostname
                     and parsed.username is None and parsed.password is None
                     and not parsed.query and not parsed.fragment
                     and not any(ord(c) <= 32 for c in base))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise SyncError("API base must be a credential-free HTTPS URL")
        self.base = base.rstrip("/")
        self.headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
        self.headers.update(headers or {})
        self.params = dict(params or {})

    def request(self, method, path, params=None, body=None):
        _safe_path(path)
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
        can_retry = _retryable(method, path)
        for attempt in range(3):
            try:
                with urlopen(req, timeout=30) as response:
                    raw = response.read()
                    response_headers = dict(response.headers.items())
                try:
                    result = json.loads(raw) if raw else None
                except (ValueError, UnicodeError):
                    raise ApiError("API returned invalid JSON") from None
                return result, response_headers
            except HTTPError as exc:
                status = exc.code
                response_headers = dict((exc.headers or {}).items())
                exc.close()
                if can_retry and attempt < 2 and (status == 429 or 500 <= status <= 599):
                    time.sleep(_delay(response_headers, attempt))
                    continue
                raise ApiError(f"API request failed (HTTP {status})", status) from None
            except (URLError, OSError, HTTPException, ValueError):
                if can_retry and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError("API connection failed or timed out") from None