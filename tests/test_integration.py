"""Fresh clone bootstrap through safe preview/apply using offline provider fixtures."""

import base64
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from syncer.model import Item, Snapshot
from syncer.runner import main
from syncer.state import StateStore


class FreshInstallationTests(unittest.TestCase):
    def test_config_to_bootstrap_preview_apply_noop_and_legacy_key_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, state_path = root / "config.toml", root / "state.enc"
            config.write_text('''version = 1
[[rules]]
id = "own-and-public"
enabled = true
source = "https://mdblist.com/lists/public-curator/films"
destination = "https://trakt.tv/users/my-account/lists/films"
direction = "source_to_destination"
deletion_policy = "mirror"
''', encoding="utf-8")
            key = base64.urlsafe_b64encode(b"k" * 32).decode()
            token = {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
                     "expires_in": 604800, "created_at": int(time.time())}
            source_items = [Item("movie", {"tmdb": 2}), Item("movie", {"tmdb": 1})]

            class Provider:
                supports_description = False
                def __init__(self, items):
                    self.items = list(items)
                    self.writes = []

                def snapshot(self, order="added"):
                    return Snapshot("fixture", "Films", "", list(self.items), writable=True)

                def preflight(self, snapshot):
                    pass

                def ensure_rank_sort(self, snapshot):
                    return False

                def add(self, items):
                    self.writes.append("add")
                    self.items.extend(items)

                def remove(self, items):
                    self.writes.append("remove")
                    self.items = [item for item in self.items if item not in items]

                def reorder(self, items, snapshot):
                    self.writes.append("reorder")
                    self.items = list(items)

            source, target = Provider(source_items), Provider([])
            env = {"MDBLIST_API_KEY": "fixture", "TRAKT_CLIENT_ID": "fixture",
                   "TRAKT_OAUTH_JSON": json.dumps(token), "SYNC_STATE_KEY": key}
            args = ["--config", str(config), "--state", str(state_path)]
            with patch.dict(os.environ, env), patch("syncer.http.Http") as http, \
                    patch("syncer.providers.MDBList", return_value=source), \
                    patch("syncer.providers.Trakt", return_value=target), \
                    redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                http.return_value.request.return_value = ({"user": {"ids": {"slug": "my-account"}}}, {})
                self.assertEqual(main([*args, "--bootstrap-state", "--dry-run"]), 0)
                self.assertFalse(state_path.exists())
                self.assertEqual(target.writes, [])
                self.assertEqual(main([*args, "--bootstrap-state"]), 0)
                self.assertEqual(len(target.items), 2)
                saved = state_path.read_bytes()
                self.assertNotIn(b"fixture-access", saved)
                target.writes.clear()
                self.assertEqual(main(args), 0)
                self.assertEqual(target.writes, [])
                self.assertEqual(state_path.read_bytes(), saved)
                state = StateStore(state_path, key).load()
                self.assertIn("own-and-public", state["rules"])
                self.assertEqual(state["oauth"], token)


if __name__ == "__main__":
    unittest.main()