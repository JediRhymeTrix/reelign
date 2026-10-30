"""Public template invariants and documentation references (no credentials)."""

from pathlib import Path
import re
import sys
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class TemplateTests(unittest.TestCase):
    def test_examples_are_valid_disabled_configurations(self):
        from syncer.config import load_settings
        folder = ROOT / "examples"
        if not folder.exists():
            self.skipTest("Examples not part of engine integration")
        for path in folder.glob("*.toml"):
            settings = load_settings(path)
            self.assertTrue(settings.rules, path.name)
            self.assertFalse(any(rule.enabled for rule in settings.rules), path.name)

    def test_default_rules_disabled_and_safe(self):
        from syncer.config import load_config
        path = ROOT / "config.toml"
        if not path.exists():
            self.skipTest("Backported engine uses installation-owned configuration")
        rules = load_config(path)
        # Private integrations deliberately own enabled rules and have UPSTREAM.json.
        if (ROOT / "UPSTREAM.json").exists():
            return
        self.assertTrue(rules)
        self.assertFalse(any(rule.enabled for rule in rules))
        self.assertTrue(all(rule.deletion_policy == "additions_only" for rule in rules))

    def test_workflow_actions_are_sha_pinned_and_no_pull_request_target(self):
        workflow_dir = ROOT / ".github/workflows"
        if not workflow_dir.exists():
            self.skipTest("Workflow layout belongs to integration")
        for path in workflow_dir.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("pull_request_target", text)
            for action in re.findall(r"uses:\s*([^\s#]+)", text):
                if action.startswith("./"):
                    self.assertTrue((ROOT / action[2:]).is_file())
                else:
                    self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

    def test_readme_brand_and_balanced_use_cases(self):
        if not (ROOT / ".github/workflows").exists():
            self.skipTest("Product copy not part of engine backport")
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Your lists. Perfectly aligned.", text)
        self.assertIn("Your watchlist didn’t ask to be part of the breakup.", text)
        self.assertIn("public", text.lower())
        self.assertIn("own", text.lower())
        self.assertIn("not endorsed or certified by Trakt", text)

    def test_ci_does_not_validate_site_with_installation_secrets(self):
        path = ROOT / ".github/workflows/ci.yml"
        if not path.exists():
            self.skipTest("CI belongs to integration")
        self.assertNotIn("secrets.", path.read_text(encoding="utf-8"))

    def test_internal_markdown_links_resolve(self):
        if not (ROOT / ".github/workflows").exists():
            self.skipTest("Site not part of engine backport")
        documents = [ROOT / "README.md", *list((ROOT / "docs").rglob("*.md"))]
        for path in documents:
            text = path.read_text(encoding="utf-8")
            for href in re.findall(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
                if "{{" in href or urlsplit(href).scheme or href.startswith("#"):
                    continue
                target = unquote(href.split("#", 1)[0].split("?", 1)[0])
                if not target:
                    continue
                base = ROOT if target.startswith("/") else path.parent
                resolved = base / target.lstrip("/")
                candidates = [resolved]
                if resolved.suffix == ".html":
                    candidates.append(resolved.with_suffix(".md"))
                if not resolved.suffix:
                    candidates.extend([resolved.with_suffix(".md"), resolved / "index.md"])
                self.assertTrue(any(p.exists() for p in candidates),
                                f"Broken link in {path.relative_to(ROOT)}: {href}")


if __name__ == "__main__":
    unittest.main()