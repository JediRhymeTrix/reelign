"""Authenticated encryption and atomic durable state; never use Actions cache."""

import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .model import StateError, SyncError

MAGIC = b"REELIGN-STATE-v1\n"
LEGACY_MAGIC = b"PERSONAL-CRON-STATE-v1\n"


def _validate_payload(data):
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        raise ValueError()
    if data["version"] not in (1, 2):
        raise ValueError()
    if not isinstance(data.get("rules"), dict) or not isinstance(data.get("oauth"), dict):
        raise ValueError()
    if data["version"] == 2 or "journals" in data:
        from .staged import validate_journals

        validate_journals(data)
        # A v1 payload with journals could be accepted by an older executable,
        # which would silently ignore pending writes. Never persist that shape.
        if data["version"] != 2 or not isinstance(data.get("journals"), dict):
            raise ValueError()


class StateStore:
    def __init__(self, path, key, repo=None):
        self.path = Path(path)
        self.repo = Path(repo).resolve() if repo else None
        try:
            raw = base64.b64decode(key, altchars=b"-_", validate=True)
            if len(raw) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            raise StateError("SYNC_STATE_KEY must be a base64-encoded 32-byte key") from None
        self.cipher = AESGCM(raw)
        self.previous = None

    def load(self):
        try:
            exists = self.path.exists()
        except Exception:
            raise StateError("Durable state could not be read; refusing reset") from None
        if not exists:
            raise StateError("Durable state missing; explicitly bootstrap once, never silently reset")
        try:
            raw = self.path.read_bytes()
            magic = next((value for value in (MAGIC, LEGACY_MAGIC) if raw.startswith(value)), None)
            if magic is None:
                raise ValueError()
            payload = raw[len(magic):]
            data = json.loads(self.cipher.decrypt(payload[:12], payload[12:], magic))
            _validate_payload(data)
            previous = copy.deepcopy(data)
        except Exception:
            raise StateError("State corrupt, incompatible or wrong encryption key; refusing reset") from None
        self.previous = previous
        return data

    def _git(self, *args):
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *args], capture_output=True, text=True, timeout=45
            )
            failed = result.returncode
            output = result.stdout
        except Exception:
            raise StateError("Durable state Git connection failed; encrypted recovery file retained") from None
        if failed:
            raise StateError("Durable state Git persistence failed; check state branch permissions")
        return output

    def preflight(self):
        """Reject stale/unwritable checkouts before consuming rotating credentials."""
        try:
            if self.repo:
                self._git("fetch", "--quiet", "origin", "cron-state")
                if self._git("rev-parse", "HEAD").strip() != self._git("rev-parse", "origin/cron-state").strip():
                    raise ValueError()
                if self._git("status", "--porcelain").strip():
                    raise ValueError()
                self._git("push", "--dry-run", "origin", "HEAD:cron-state")
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if not os.access(self.path.parent, os.W_OK):
                    raise ValueError()
        except Exception:
            raise StateError(
                "Durable state preflight failed; require a current, clean, writable checkout before mutations"
            ) from None

    def save(self, data):
        """Validate every checkpoint and stop safely on any persistence failure."""
        try:
            _validate_payload(data)
            return self._save(data)
        except Exception:
            raise StateError(
                "Durable state persistence failed; encrypted recovery file retained if written; stop mutations"
            ) from None

    def _save(self, data):
        if data == self.previous:
            return False
        previous = copy.deepcopy(data)
        raw = json.dumps(previous, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        encrypted = MAGIC + nonce + self.cipher.encrypt(nonce, raw, MAGIC)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        try:
            stream = os.fdopen(fd, "wb")
            fd = None
            with stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if fd is not None:
                os.close(fd)
            if os.path.exists(name):
                os.unlink(name)
        if self.repo:
            relative = self.path.resolve().relative_to(self.repo).as_posix()
            self._git("add", "--", relative)
            self._git("-c", "user.name=reelign[bot]", "-c",
                      "user.email=reelign@users.noreply.github.com",
                      "commit", "-m", "Update encrypted list-sync state", "--", relative)
            for attempt in range(3):
                try:
                    self._git("push", "origin", "HEAD:cron-state")
                    break
                except SyncError:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
        self.previous = previous
        return True