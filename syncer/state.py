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

from .model import SyncError

MAGIC = b"REELIGN-STATE-v1\n"
LEGACY_MAGIC = b"PERSONAL-CRON-STATE-v1\n"


class StateStore:
    def __init__(self, path, key, repo=None):
        self.path = Path(path)
        self.repo = Path(repo).resolve() if repo else None
        try:
            raw = base64.b64decode(key, altchars=b"-_", validate=True)
            if len(raw) != 32:
                raise ValueError()
        except (ValueError, TypeError):
            raise SyncError("SYNC_STATE_KEY must be a base64-encoded 32-byte key") from None
        self.cipher = AESGCM(raw)
        self.previous = None

    def load(self):
        if not self.path.exists():
            raise SyncError("Durable state missing; explicitly bootstrap once, never silently reset")
        try:
            raw = self.path.read_bytes()
            magic = next((value for value in (MAGIC, LEGACY_MAGIC) if raw.startswith(value)), None)
            if magic is None:
                raise ValueError()
            payload = raw[len(magic):]
            data = json.loads(self.cipher.decrypt(payload[:12], payload[12:], magic))
            if data.get("version") != 1 or not isinstance(data.get("rules"), dict):
                raise ValueError()
            if not isinstance(data.get("oauth"), dict):
                raise ValueError()
        except Exception:
            raise SyncError("State corrupt, incompatible or wrong encryption key; refusing reset") from None
        self.previous = copy.deepcopy(data)
        return data

    def _git(self, *args):
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo), *args], capture_output=True, text=True, timeout=45
            )
        except (OSError, subprocess.TimeoutExpired):
            raise SyncError("Durable state Git connection failed; encrypted recovery file retained") from None
        if result.returncode:
            raise SyncError("Durable state Git persistence failed; check state branch permissions")
        return result.stdout

    def preflight(self):
        """Reject stale/unwritable checkouts before consuming rotating credentials."""
        if self.repo:
            self._git("fetch", "--quiet", "origin", "cron-state")
            if self._git("rev-parse", "HEAD").strip() != self._git("rev-parse", "origin/cron-state").strip():
                raise SyncError("State checkout stale; reload before OAuth rotation")
            if self._git("status", "--porcelain").strip():
                raise SyncError("State checkout dirty; resolve before OAuth rotation")
            self._git("push", "--dry-run", "origin", "HEAD:cron-state")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not os.access(self.path.parent, os.W_OK):
                raise SyncError("Local state directory is not writable")

    def save(self, data):
        if data == self.previous:
            return False
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        encrypted = MAGIC + nonce + self.cipher.encrypt(nonce, raw, MAGIC)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, prefix=".state-")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
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
        self.previous = copy.deepcopy(data)
        return True