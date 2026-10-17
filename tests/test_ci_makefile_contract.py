import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = REPO_ROOT / "Makefile"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GITIGNORE = REPO_ROOT / ".gitignore"


class CiMakefileContractTests(unittest.TestCase):
    def test_makefile_has_python_test_target(self):
        makefile = MAKEFILE.read_text()

        self.assertRegex(makefile, r"(?m)^\.PHONY: .*\btest\b")
        self.assertRegex(makefile, r"(?m)^test:\n\tGATEWAY_TOKEN=test-token RUNTIME_FAKE_PROVIDER=true ")
        self.assertIn("python3 -m pytest tests -q", makefile)
        self.assertRegex(makefile, r"(?m)^check: lint validate test$")

    def test_makefile_validates_rendered_workspace_not_source_templates(self):
        makefile = MAKEFILE.read_text()

        self.assertNotIn("bash scripts/validate-placeholders.sh workspace", makefile)
        render_index = makefile.index("python3 scripts/render-workspace.py")
        validate_index = makefile.index(
            "bash scripts/validate-placeholders.sh $(RENDERED_WORKSPACE_DIR)"
        )
        self.assertLess(render_index, validate_index)
        self.assertIn("--config config/organization.example.yaml", makefile)
        self.assertIn("--src workspace", makefile)
        self.assertIn("--strict", makefile)
        self.assertIn("bash scripts/validate-placeholders.sh $(RENDERED_WORKSPACE_DIR)", makefile)
        self.assertIn("bash scripts/validate-placeholders.sh skills", makefile)
        self.assertRegex(makefile, r"(?m)^validate:$")

    def test_makefile_has_no_legacy_terraform_targets(self):
        makefile = MAKEFILE.read_text()

        self.assertNotIn("cd terraform", makefile)
        self.assertNotRegex(makefile, r"(?m)^(apply|destroy):")

    def test_ci_validates_rendered_workspace_not_source_templates(self):
        ci = CI_WORKFLOW.read_text()

        self.assertNotIn("run: bash scripts/validate-placeholders.sh workspace", ci)
        self.assertIn("Validate placeholders (rendered workspace)", ci)
        render_index = ci.index("python3 scripts/render-workspace.py")
        validate_index = ci.index(
            "bash scripts/validate-placeholders.sh /tmp/ts-harness-workspace-rendered"
        )
        self.assertLess(render_index, validate_index)
        self.assertIn("--config config/organization.example.yaml", ci)
        self.assertIn("--src workspace", ci)
        self.assertIn("--dst /tmp/ts-harness-workspace-rendered", ci)
        self.assertIn("bash scripts/validate-placeholders.sh /tmp/ts-harness-workspace-rendered", ci)
        self.assertIn("run: bash scripts/validate-placeholders.sh skills", ci)

    def test_project_local_worktrees_are_ignored(self):
        gitignore = GITIGNORE.read_text()

        self.assertRegex(gitignore, r"(?m)^\.worktrees/$")


if __name__ == "__main__":
    unittest.main()
