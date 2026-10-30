"""Source credits use destination-native formatting without description churn."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer.attribution import compose_description, CREDIT_URL, PROMO
from syncer.model import Snapshot, SyncError
from syncer.providers import MDBList, Trakt
from test_providers import FakeHttp, MDB_URL, TRAKT_URL, mdb_meta, trakt_meta


class DescriptionTests(unittest.TestCase):
    def setUp(self):
        self.source = Snapshot(MDB_URL, "Latest films", "Source description", [],
                               provider="mdblist", metadata={"user_name": "owner", "private": False})

    def compose(self, description="", **kwargs):
        return compose_description(description, self.source, "Gary", **kwargs)

    def test_compact_markdown_links_credit_and_approved_promo(self):
        text = self.compose()
        self.assertIn(f"Synced from [Latest films]({MDB_URL}) by [Gary (@owner)](https://mdblist.com/@owner).", text)
        self.assertIn(PROMO, text)
        self.assertIn(CREDIT_URL, text)
        self.assertNotIn("<a ", text)
        self.assertNotIn("[url", text)

    def test_existing_description_preserved_exactly_and_footer_idempotent(self):
        original = "My **own** description.\n\nKeep this wording.  "
        first = self.compose(original)
        self.assertTrue(first.startswith(original + "\n\n"))
        self.assertEqual(self.compose(first), first)
        self.assertEqual(first.count(PROMO), 1)

    def test_source_renames_replace_only_our_footer_and_preserve_later_text(self):
        first = self.compose("Before") + "\n\nAfter my credit."
        self.source.name = "Renamed list"
        second = self.compose(first)
        self.assertIn("Before", second)
        self.assertIn("After my credit.", second)
        self.assertIn("[Renamed list]", second)
        self.assertNotIn("[Latest films]", second)
        self.assertEqual(second.count(PROMO), 1)
        self.assertEqual(self.compose(second), second)

    def test_markdown_html_and_control_characters_in_names_are_escaped(self):
        self.source.name = "[click](https://evil.test) <script>\nBad"
        text = compose_description("", self.source, "A ](bad) <tag>\nB")
        self.assertIn(r"\[click\]\(https://evil.test\)", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn("<tag>", text)
        self.assertNotIn("\nBad", text)

    def test_private_or_unknown_source_not_disclosed_to_public_destination(self):
        for metadata in ({"private": True}, {}, {"privacy": "private"}):
            self.source.metadata = metadata
            with self.subTest(metadata=metadata), self.assertRaises(SyncError):
                self.compose(destination_public=True)
        self.source.metadata = {"private": True}
        self.assertIn(MDB_URL, self.compose(destination_public=False))

    def test_oversize_never_truncates_existing_description_or_credit(self):
        with self.assertRaises(SyncError):
            self.compose("x" * 1000, limit=1000)
        with self.assertRaises(SyncError):
            self.compose(limit=10)

    def test_lookalike_unmanaged_text_not_removed(self):
        original = "Synced from someone else.\n\nPowered by something else."
        self.assertTrue(self.compose(original).startswith(original))

    def test_plain_text_format_uses_visible_urls_not_markdown(self):
        text = self.compose(formatting="plain")
        self.assertIn(MDB_URL, text)
        self.assertIn("Gary (@owner)", text)
        self.assertIn(CREDIT_URL, text)
        self.assertNotIn("](", text)
        self.assertEqual(self.compose(text, formatting="plain"), text)

    def test_unsafe_source_url_and_duplicate_generated_blocks_fail_closed(self):
        first = self.compose()
        with self.assertRaises(SyncError):
            self.compose(first + "\n\n" + first)
        self.source.url = MDB_URL + "?apikey=do-not-disclose"
        with self.assertRaises(SyncError):
            self.compose()

    def test_trakt_original_list_and_creator_profile_links(self):
        self.source.url = TRAKT_URL
        self.source.provider = "trakt"
        self.source.metadata = {"privacy": "public"}
        text = self.compose(destination_public=True)
        self.assertIn(f"[Latest films]({TRAKT_URL})", text)
        self.assertIn("[Gary (@owner)](https://trakt.tv/users/owner)", text)


class MetadataProviderTests(unittest.TestCase):
    def test_mdblist_description_capability_is_explicitly_unsupported(self):
        provider = MDBList(MDB_URL, FakeHttp())
        self.assertFalse(provider.supports_description)
        self.assertEqual(provider.description_format, "plain")

    def test_mdblist_creator_profile_cached_and_owner_validated(self):
        http = FakeHttp({"user": {"username": "owner", "name": "Gary"}})
        provider = MDBList(MDB_URL, http)
        snapshot = Snapshot(MDB_URL, "Films", "", [], metadata=mdb_meta())
        self.assertEqual(provider.creator(snapshot), "Gary")
        self.assertEqual(provider.creator(snapshot), "Gary")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][1], "/users/owner")
        with self.assertRaises(SyncError):
            MDBList(MDB_URL, FakeHttp({"user": {"username": "someone-else", "name": "Wrong"}})).creator(snapshot)

    def test_trakt_creator_uses_source_metadata_not_destination_account(self):
        snapshot = Snapshot(TRAKT_URL, "Films", "", [], metadata={"user": {
            "name": "Curator", "username": "owner", "ids": {"slug": "owner"}}})
        self.assertEqual(Trakt(TRAKT_URL, FakeHttp(), "different-account").creator(snapshot), "Curator")

    def test_description_only_put_verified_and_repeat_noop(self):
        before = trakt_meta(); before["description"] = "My original text"
        after = deepcopy(before); after["description"] = "New description"
        provider = Trakt(TRAKT_URL, FakeHttp(before, after, after), "owner")
        snapshot = Snapshot(TRAKT_URL, "Films", before["description"], [], metadata=before)
        self.assertTrue(provider.ensure_description(snapshot, "New description"))
        self.assertEqual(provider.http.calls[1], ("PUT", "/users/owner/lists/films", None,
                                                {"description": "New description"}))
        self.assertFalse(provider.ensure_description(snapshot, "New description"))
        self.assertEqual(len(provider.http.calls), 3)

    def test_concurrent_description_edit_or_failed_verification_blocks_write_checkpoint(self):
        before = trakt_meta()
        snapshot = Snapshot(TRAKT_URL, "Films", before["description"], [], metadata=before)
        changed = deepcopy(before); changed["description"] = "Someone edited this"
        provider = Trakt(TRAKT_URL, FakeHttp(changed), "owner")
        with self.assertRaises(SyncError):
            provider.ensure_description(snapshot, "New")
        self.assertFalse(any(call[0] == "PUT" for call in provider.http.calls))
        provider = Trakt(TRAKT_URL, FakeHttp(before, before, before), "owner")
        with self.assertRaises(SyncError):
            provider.ensure_description(snapshot, "New")

    def test_nonowner_cannot_update_description(self):
        snapshot = Snapshot(TRAKT_URL, "Films", "Description", [], metadata=trakt_meta())
        provider = Trakt(TRAKT_URL, FakeHttp(), "someone-else")
        with self.assertRaises(SyncError):
            provider.ensure_description(snapshot, "New")
        self.assertEqual(provider.http.calls, [])


if __name__ == "__main__":
    unittest.main()