"""Setup contracts: synthetic credentials, mocked HTTP/GitHub, no remote writes."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from syncer import setup
from syncer.http import ApiError
from syncer.state import StateStore


TOKEN = {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
         "expires_in": 864000, "created_at": 1900000000, "token_type": "bearer"}
KEY = base64.urlsafe_b64encode(b"k" * 32).decode()
ENV = {"TRAKT_CLIENT_ID": "fixture-client", "MDBLIST_API_KEY": "fixture-mdb",
       "TRAKT_OAUTH_JSON": json.dumps(TOKEN), "SYNC_STATE_KEY": KEY}
DEVICE = {"device_code": "fixture-device-secret", "user_code": "ABCD1234",
          "verification_url": "https://trakt.tv/activate", "expires_in": 60, "interval": 2}


class Clock:
    def __init__(self):
        self.now = 0
        self.delays = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.now += seconds


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, ENV, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def cli(self, *args):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = setup.main(list(args))
        return result, out.getvalue(), err.getvalue()

    def config(self):
        path = self.root / "config.toml"
        path.write_text('''version = 1
[[rules]]
id = "example"
source = "https://mdblist.com/lists/example/films"
destination = "https://trakt.tv/users/example/lists/films"
direction = "source_to_destination"
deletion_policy = "mirror"
enabled = false
''', encoding="utf-8")
        return path

    def gh_process(self, args, **kwargs):
        """Minimal gh fixture. Missing ref has a distinguishable 404, never 403."""
        if args[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[1:3] == ["secret", "list"]:
            return subprocess.CompletedProcess(args, 0, "[]", "")
        if args[1] == "api":
            endpoint = args[2]
            if endpoint.endswith("git/ref/heads/cron-state"):
                return subprocess.CompletedProcess(args, 1, "", "gh: Not Found (HTTP 404)")
            return subprocess.CompletedProcess(args, 0, '{"sha":"' + "a" * 40 + '"}', "")
        return subprocess.CompletedProcess(args, 0, "", "")

    def auth(self, responses=None, *extra):
        clock = Clock()
        http = Mock()
        http.request.side_effect = responses or [(DEVICE, {}), (TOKEN, {})]
        with patch("syncer.setup.shutil.which", return_value="gh"), \
             patch("syncer.setup.subprocess.run", side_effect=self.gh_process) as gh, \
             patch("syncer.setup.Http", return_value=http) as factory, \
             patch("syncer.setup.time.monotonic", side_effect=clock.monotonic), \
             patch("syncer.setup.time.sleep", side_effect=clock.sleep), \
             patch("syncer.setup.time.time", return_value=1900000010), \
             patch("syncer.setup.sys.stdin.isatty", return_value=False):
            result = self.cli("auth", *extra)
        return result, gh, http, factory, clock

    def test_main_wrapper_exposes_same_main_without_running_on_import(self):
        with patch("syncer.setup.main") as main:
            wrapper = importlib.import_module("manage")
            main.assert_not_called()
            self.assertIs(wrapper.main, main)

    def test_validate_explicit_and_project_root_default_without_http(self):
        with patch("syncer.setup.Http") as http, patch("syncer.config.load_settings") as load:
            load.return_value.rules = []
            code, out, err = self.cli("validate")
        self.assertEqual(code, 0, err)
        self.assertEqual(load.call_args.args[0], Path(setup.__file__).resolve().parents[1] / "config.toml")
        self.assertIn("0", out)
        http.assert_not_called()
        self.assertEqual(self.cli("validate", "--config", str(self.config()))[0], 0)

    def test_invalid_config_error_does_not_echo_config_contents(self):
        path = self.root / "bad.toml"
        path.write_text('fixture-access = [', encoding="utf-8")
        code, out, err = self.cli("validate", "--config", str(path))
        self.assertEqual(code, 1)
        self.assertNotIn("fixture-access", out + err)

    def test_doctor_only_reports_presence_and_local_hints(self):
        with patch("syncer.setup.shutil.which", return_value=None), \
             patch("syncer.setup.subprocess.run") as gh, patch("syncer.setup.Http") as http:
            code, out, err = self.cli("doctor", "--config", str(self.config()))
        self.assertEqual(code, 0, err)
        self.assertIn("gh", out)
        self.assertIn("TRAKT_CLIENT_ID", out)
        self.assertIn("optional", out.lower())
        self.assertNotIn("fixture-", out + err)
        self.assertNotIn(KEY, out + err)
        gh.assert_not_called()
        http.assert_not_called()

    def test_renderer_writes_checks_removes_only_schedule_yml_files(self):
        folder = self.root / "workflows"
        folder.mkdir()
        (folder / "sync.yml").write_text("user workflow", encoding="utf-8")
        (folder / "schedule-old.yml").write_text("stale", encoding="utf-8")
        (folder / "schedule-custom.yaml").write_text("not owned", encoding="utf-8")
        args = ("render-workflows", "--config", str(self.config()), "--output-dir", str(folder))
        with patch("syncer.scheduling.render_workflows", return_value={"schedule-main.yml": "generated\n"}):
            self.assertEqual(self.cli(*args, "--check")[0], 1)
            self.assertTrue((folder / "schedule-old.yml").exists())
            self.assertFalse((folder / "schedule-main.yml").exists())
            self.assertEqual(self.cli(*args)[0], 0)
            self.assertEqual(self.cli(*args, "--check")[0], 0)
            (folder / "schedule-main.yml").write_text("changed", encoding="utf-8")
            self.assertEqual(self.cli(*args, "--check")[0], 1)
        self.assertFalse((folder / "schedule-old.yml").exists())
        self.assertEqual((folder / "sync.yml").read_text(), "user workflow")
        self.assertTrue((folder / "schedule-custom.yaml").exists())

    def test_renderer_rejects_unsafe_filename_before_any_writes(self):
        args = ("render-workflows", "--config", str(self.config()), "--output-dir", str(self.root / "out"))
        with patch("syncer.scheduling.render_workflows", return_value={"../sync.yml": "bad"}):
            self.assertEqual(self.cli(*args)[0], 1)
        self.assertFalse((self.root / "sync.yml").exists())

    def test_auth_requires_gh_repo_or_explicit_plaintext_opt_in(self):
        with patch("syncer.setup.Http") as http, patch("syncer.setup.getpass.getpass") as prompt:
            code, out, err = self.cli("auth")
        self.assertEqual(code, 1)
        self.assertIn("--repo", err)
        self.assertIn("--output-secret-file", err)
        http.assert_not_called()
        prompt.assert_not_called()

    def test_auth_repo_installs_secrets_only_via_stdin_no_token_output(self):
        (code, out, err), gh, http, factory, clock = self.auth(None, "--repo", "owner/reelign")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["Code: ABCD1234", "Verification URL: https://trakt.tv/activate"])
        factory.assert_called_once_with("https://auth.trakt.tv")
        self.assertEqual(clock.delays, [2])
        self.assertEqual([call.args[:2] for call in http.request.call_args_list],
                         [("POST", "/oauth/device/code"), ("POST", "/oauth/device/token")])
        self.assertNotIn("client_secret", http.request.call_args.kwargs["body"])
        installs = {call.args[0][3]: call for call in gh.call_args_list
                    if call.args[0][1:3] == ["secret", "set"]}
        self.assertEqual(set(installs), {"TRAKT_CLIENT_ID", "MDBLIST_API_KEY", "TRAKT_OAUTH_JSON", "SYNC_STATE_KEY"})
        self.assertEqual(json.loads(installs["TRAKT_OAUTH_JSON"].kwargs["input"]), TOKEN)
        self.assertEqual(len(base64.urlsafe_b64decode(installs["SYNC_STATE_KEY"].kwargs["input"])), 32)
        for call in gh.call_args_list:
            command = " ".join(call.args[0])
            self.assertNotIn("fixture-", command)
            self.assertNotIn("--body", command)
        for value in ["fixture-access", "fixture-refresh", "fixture-device-secret", "fixture-client", "fixture-mdb", KEY]:
            self.assertNotIn(value, out + err)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_auth_optional_secret_prompt_and_environment(self):
        os.environ["TRAKT_CLIENT_SECRET"] = "fixture-client-secret"
        (code, out, err), gh, http, _, _ = self.auth(None, "--repo", "owner/reelign")
        self.assertEqual(code, 0, err)
        self.assertEqual(http.request.call_args.kwargs["body"]["client_secret"], "fixture-client-secret")
        self.assertTrue(any(call.args[0][1:4] == ["secret", "set", "TRAKT_CLIENT_SECRET"] for call in gh.call_args_list))
        self.assertNotIn("fixture-client-secret", out + err)

    def test_auth_pending_and_slowdown_increase_interval(self):
        responses = [(DEVICE, {}), ApiError("pending", 400), ApiError("slow", 429), (TOKEN, {})]
        (code, _, err), _, http, _, clock = self.auth(responses, "--repo", "owner/reelign")
        self.assertEqual(code, 0, err)
        self.assertEqual(clock.delays, [2, 2, 7])
        self.assertEqual(http.request.call_count, 4)

    def test_auth_accepts_documented_auth_verification_host_and_rejects_other_urls(self):
        device = dict(DEVICE, verification_url="https://auth.trakt.tv/activate")
        (code, out, err), _, _, _, _ = self.auth([(device, {}), (TOKEN, {})], "--repo", "owner/reelign")
        self.assertEqual(code, 0, err)
        self.assertIn("https://auth.trakt.tv/activate", out)
        for url in ("https://trakt.tv.evil.test/activate", "https://auth.trakt.tv/activate?token=fixture-access",
                    "https://fixture-access@trakt.tv/activate"):
            device = dict(DEVICE, verification_url=url)
            (code, out, err), gh, _, _, _ = self.auth([(device, {})], "--repo", "owner/reelign")
            self.assertEqual(code, 1)
            self.assertNotIn("fixture-access", out + err)
            self.assertFalse(any(call.args[0][1:3] == ["secret", "set"] for call in gh.call_args_list))

    def test_auth_expiry_stops_polling_without_installing_secrets(self):
        device = dict(DEVICE, expires_in=5)
        result, gh, http, _, clock = self.auth([(device, {}), ApiError("pending", 400), ApiError("pending", 400)], "--repo", "owner/reelign")
        self.assertEqual(result[0], 1)
        self.assertIn("expired", result[2].lower())
        self.assertEqual(clock.delays, [2, 2, 1])
        self.assertEqual(http.request.call_count, 3)
        self.assertFalse(any(call.args[0][1:3] == ["secret", "set"] for call in gh.call_args_list))

    def test_auth_other_errors_never_retry_or_expose_transport_message(self):
        for status in (401, 403, 409, 410, 418, 503, None):
            with self.subTest(status=status):
                (code, out, err), gh, http, _, _ = self.auth([(DEVICE, {}), ApiError("fixture-access", status)], "--repo", "owner/reelign")
                self.assertEqual(code, 1)
                self.assertEqual(http.request.call_count, 2)
                self.assertNotIn("fixture-access", out + err)
                self.assertFalse(any(call.args[0][1:3] == ["secret", "set"] for call in gh.call_args_list))

    def test_auth_refuses_existing_key_existing_branch_and_ambiguous_lookup(self):
        for condition in ("key", "branch", "forbidden"):
            def process(args, **kwargs):
                if condition == "key" and args[1:3] == ["secret", "list"]:
                    return subprocess.CompletedProcess(args, 0, '[{"name":"SYNC_STATE_KEY"}]', "")
                if args[1] == "api" and args[2].endswith("git/ref/heads/cron-state"):
                    if condition == "branch":
                        return subprocess.CompletedProcess(args, 0, '{"ref":"refs/heads/cron-state"}', "")
                    if condition == "forbidden":
                        return subprocess.CompletedProcess(args, 1, "", "fixture-access (HTTP 403)")
                return self.gh_process(args, **kwargs)
            with self.subTest(condition=condition), patch("syncer.setup.shutil.which", return_value="gh"), \
                 patch("syncer.setup.subprocess.run", side_effect=process), patch("syncer.setup.Http") as http:
                code, out, err = self.cli("auth", "--repo", "owner/reelign")
            self.assertEqual(code, 1)
            self.assertNotIn("fixture-access", out + err)
            http.assert_not_called()

    def test_auth_missing_gh_fails_before_authorization(self):
        with patch("syncer.setup.shutil.which", return_value=None), patch("syncer.setup.Http") as http:
            code, _, err = self.cli("auth", "--repo", "owner/reelign")
        self.assertEqual(code, 1)
        self.assertIn("gh", err)
        http.assert_not_called()

    def test_manual_file_is_explicit_create_exclusive_and_contains_named_secrets(self):
        output = self.root / "secrets.json"
        (code, out, err), gh, _, _, _ = self.auth(None, "--output-secret-file", str(output))
        self.assertEqual(code, 0, err)
        data = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(data["TRAKT_OAUTH_JSON"]), TOKEN)
        self.assertIn("SYNC_STATE_KEY", err)
        self.assertIn("plaintext", err.lower())
        self.assertNotIn("fixture-access", out + err)
        gh.assert_not_called()
        with patch("syncer.setup.Http") as http:
            self.assertEqual(self.cli("auth", "--output-secret-file", str(output))[0], 1)
        http.assert_not_called()
        self.assertEqual(json.loads(output.read_text()), data)

    def test_optional_backup_written_before_secret_install_and_matches_installed_key(self):
        output = self.root / "backup.json"
        clock = Clock()
        http = Mock()
        http.request.side_effect = [(DEVICE, {}), (TOKEN, {})]
        installed = {}
        def process(args, **kwargs):
            if args[1:3] == ["secret", "set"]:
                self.assertTrue(output.exists(), "backup must be durable before remote secrets change")
                installed[args[3]] = kwargs["input"]
            return self.gh_process(args, **kwargs)
        with patch("syncer.setup.shutil.which", return_value="gh"), \
             patch("syncer.setup.subprocess.run", side_effect=process), \
             patch("syncer.setup.Http", return_value=http), \
             patch("syncer.setup.time.monotonic", side_effect=clock.monotonic), \
             patch("syncer.setup.time.sleep", side_effect=clock.sleep), \
             patch("syncer.setup.sys.stdin.isatty", return_value=False):
            code, out, err = self.cli("auth", "--repo", "owner/reelign", "--output-secret-file", str(output))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output.read_text()), installed)
        self.assertNotIn(installed["SYNC_STATE_KEY"], out + err)
        self.assertNotIn("fixture-access", out + err)

    def test_repository_argument_rejects_injection_urls_and_traversal(self):
        for repo in ("https://github.com/owner/repo", "../repo", "owner/repo/extra", "owner/repo?token=fixture-access", "-owner/repo", "owner/..", "owner/repo\nX"):
            for command in ("auth", "init-state"):
                with self.subTest(repo=repo, command=command), patch("syncer.setup.subprocess.run") as gh, patch("syncer.setup.Http") as http:
                    code, out, err = self.cli(command, f"--repo={repo}")
                    self.assertEqual(code, 1)
                    self.assertNotIn("fixture-access", out + err)
                    gh.assert_not_called()
                    http.assert_not_called()

    def test_init_encrypts_state_and_creates_ref_last_without_git_or_plaintext(self):
        with patch("syncer.setup.shutil.which", return_value="gh"), \
             patch("syncer.setup.subprocess.run", side_effect=self.gh_process) as gh:
            code, out, err = self.cli("init-state", "--repo", "owner/reelign")
        self.assertEqual(code, 0, err)
        posts = [call for call in gh.call_args_list if "--method" in call.args[0] and "POST" in call.args[0]]
        self.assertEqual([call.args[0][2].split("/git/")[1] for call in posts], ["blobs", "trees", "commits", "refs"])
        payloads = [json.loads(call.kwargs["input"]) for call in posts]
        self.assertEqual(payloads[0]["encoding"], "base64")
        encrypted = base64.b64decode(payloads[0]["content"])
        path = self.root / "state.enc"
        path.write_bytes(encrypted)
        self.assertEqual(StateStore(path, KEY).load(), {"version": 1, "rules": {}, "oauth": TOKEN})
        self.assertEqual(payloads[1]["tree"][0]["path"], "jobs/list-sync/state.enc")
        self.assertEqual(payloads[2]["parents"], [])
        self.assertEqual(payloads[3]["ref"], "refs/heads/cron-state")
        self.assertIn("--bootstrap-state", out)
        for call in gh.call_args_list:
            self.assertEqual(call.args[0][0], "gh")
            self.assertNotIn("fixture-", " ".join(call.args[0]) + (call.kwargs.get("input") or ""))
            self.assertNotIn(KEY, call.kwargs.get("input") or "")

    def test_init_preview_never_posts(self):
        with patch("syncer.setup.shutil.which", return_value="gh"), \
             patch("syncer.setup.subprocess.run", side_effect=self.gh_process) as gh:
            code, out, err = self.cli("init-state", "--repo", "owner/reelign", "--preview")
        self.assertEqual(code, 0, err)
        self.assertIn("preview", out.lower())
        self.assertFalse(any("POST" in call.args[0] for call in gh.call_args_list))

    def test_init_missing_or_invalid_environment_does_not_contact_github(self):
        for name, value in (("TRAKT_OAUTH_JSON", "fixture-access"), ("SYNC_STATE_KEY", "bad"), ("TRAKT_OAUTH_JSON", '{}')):
            with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}), \
                 patch("syncer.setup.subprocess.run") as gh:
                code, out, err = self.cli("init-state", "--repo", "owner/reelign")
                self.assertEqual(code, 1)
                self.assertNotIn("fixture-access", out + err)
                gh.assert_not_called()

    def test_init_existing_branch_fails_without_any_mutation(self):
        def process(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, '{}', '')
        with patch("syncer.setup.shutil.which", return_value="gh"), \
             patch("syncer.setup.subprocess.run", side_effect=process) as gh:
            code, _, err = self.cli("init-state", "--repo", "owner/reelign")
        self.assertEqual(code, 1)
        self.assertIn("existing", err.lower())
        self.assertFalse(any("POST" in call.args[0] for call in gh.call_args_list))

    def test_init_failed_object_creation_never_creates_or_resets_ref(self):
        def process(args, **kwargs):
            if args[1] == "api" and args[2].endswith("/git/commits"):
                return subprocess.CompletedProcess(args, 1, "fixture-access", "fixture-refresh")
            return self.gh_process(args, **kwargs)
        with patch("syncer.setup.shutil.which", return_value="gh"), \
             patch("syncer.setup.subprocess.run", side_effect=process) as gh:
            code, out, err = self.cli("init-state", "--repo", "owner/reelign")
        self.assertEqual(code, 1)
        self.assertNotIn("fixture-", out + err)
        self.assertFalse(any(call.args[0][2].endswith("/git/refs") for call in gh.call_args_list if call.args[0][1] == "api"))

    def test_destination_creates_only_empty_private_list_owned_by_authenticated_user(self):
        trakt = Mock()
        trakt.request.side_effect = [({"user": {"ids": {"slug": "my-account"}}}, {}),
                                     ([], {}), ({"privacy": "private", "ids": {"slug": "source-title"}}, {})]
        mdb = Mock()
        mdb.request.return_value = ([{"name": "Source title", "description": "Source description"}], {})
        with patch("syncer.setup.Http", side_effect=[trakt, mdb]), patch("syncer.setup.time.time", return_value=1900000010):
            code, out, err = self.cli("create-destination", "--source", "https://mdblist.com/lists/curator/films", "--provider", "trakt")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "https://trakt.tv/users/my-account/lists/source-title")
        call = trakt.request.call_args
        self.assertEqual(call.args, ("POST", "/users/my-account/lists"))
        self.assertEqual(call.kwargs["body"]["privacy"], "private")
        self.assertEqual(call.kwargs["body"]["name"], "Source title")
        self.assertEqual(call.kwargs["body"]["description"], "Source description")
        self.assertNotIn("fixture-", out + err)

    def test_destination_name_collision_refuses_adoption_or_mutation(self):
        trakt = Mock()
        trakt.request.side_effect = [({"user": {"ids": {"slug": "my-account"}}}, {}),
                                     ([{"name": " source TITLE "}], {})]
        mdb = Mock()
        mdb.request.return_value = ({"name": "Source title", "description": ""}, {})
        with patch("syncer.setup.Http", side_effect=[trakt, mdb]), patch("syncer.setup.time.time", return_value=1900000010):
            code, _, err = self.cli("create-destination", "--source", "https://mdblist.com/lists/curator/films", "--provider", "trakt")
        self.assertEqual(code, 1)
        self.assertIn("exists", err.lower())
        self.assertTrue(all(call.args[0] == "GET" for call in trakt.request.call_args_list))

    def test_reauthorization_export_omits_key_and_mdblist_secret(self):
        output = self.root / "reauthorize.json"
        (code, out, err), gh, _, _, _ = self.auth(None, "--reauthorize", "--output-secret-file", str(output))
        self.assertEqual(code, 0, err)
        values = json.loads(output.read_text())
        self.assertEqual(set(values), {"TRAKT_CLIENT_ID", "TRAKT_OAUTH_JSON"})
        self.assertEqual(json.loads(values["TRAKT_OAUTH_JSON"]), TOKEN)
        self.assertNotIn("fixture-access", out + err)
        gh.assert_not_called()

    def test_reauthorization_refuses_repo_installation_or_missing_explicit_export(self):
        for args in (("--repo", "owner/reelign"), ()):
            with self.subTest(args=args), patch("syncer.setup.Http") as http, patch("syncer.setup.subprocess.run") as gh:
                self.assertEqual(self.cli("auth", "--reauthorize", *args)[0], 1)
            http.assert_not_called()
            gh.assert_not_called()

    def test_update_auth_preserves_baselines_and_original_key(self):
        path = self.root / "state.enc"
        original = {"version": 1, "rules": {"keep": {"fingerprint": "baseline", "items": ["movie:1"]}},
                    "oauth": dict(TOKEN, access_token="old-access")}
        StateStore(path, KEY).save(original)
        self.assertEqual(self.cli("update-auth", "--state", str(path))[0], 0)
        current = StateStore(path, KEY).load()
        self.assertEqual(current["rules"], original["rules"])
        self.assertEqual(current["oauth"], TOKEN)

    def test_update_auth_preview_and_wrong_key_never_overwrite_state(self):
        path = self.root / "state.enc"
        StateStore(path, KEY).save({"version": 1, "rules": {}, "oauth": TOKEN})
        before = path.read_bytes()
        self.assertEqual(self.cli("update-auth", "--state", str(path), "--preview")[0], 0)
        with patch.dict(os.environ, {"SYNC_STATE_KEY": base64.urlsafe_b64encode(b"z" * 32).decode()}):
            self.assertEqual(self.cli("update-auth", "--state", str(path))[0], 1)
        self.assertEqual(path.read_bytes(), before)

    def test_update_auth_missing_state_never_bootstraps(self):
        path = self.root / "absent.enc"
        code, _, err = self.cli("update-auth", "--state", str(path))
        self.assertEqual(code, 1)
        self.assertIn("missing", err.lower())
        self.assertFalse(path.exists())

    def test_update_auth_preflights_git_before_replacing_only_oauth(self):
        store = Mock()
        original = {"version": 1, "rules": {"keep": {"baseline": "unchanged"}}, "oauth": {}}
        store.load.return_value = original
        with patch("syncer.setup.StateStore", return_value=store) as factory:
            code, _, err = self.cli("update-auth", "--state", "checkout/state.enc", "--state-repo", "checkout")
        self.assertEqual(code, 0, err)
        self.assertEqual([call[0] for call in store.mock_calls], ["load", "preflight", "save"])
        self.assertEqual(store.save.call_args.args[0], {"version": 1, "rules": original["rules"], "oauth": TOKEN})
        self.assertEqual(factory.call_args.args, (Path("checkout/state.enc"), KEY, Path("checkout")))


if __name__ == "__main__":
    unittest.main()