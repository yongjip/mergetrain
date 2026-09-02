from __future__ import annotations

import unittest
from pathlib import Path


class MCPRegistryWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).resolve().parents[1]
        cls.workflow = (
            cls.repo / ".github" / "workflows" / "mcp-registry.yml"
        ).read_text()
        cls.release_workflow = (
            cls.repo / ".github" / "workflows" / "release.yml"
        ).read_text()

    def test_registry_workflow_uses_oidc_without_a_pat(self) -> None:
        self.assertIn("workflow_call:", self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertIn("id-token: write", self.workflow)
        self.assertIn("./mcp-publisher login github-oidc", self.workflow)
        self.assertNotIn("MCP_GITHUB_TOKEN", self.workflow)
        self.assertNotIn("--token", self.workflow)

    def test_manual_publication_is_serialized_and_main_only(self) -> None:
        self.assertIn("group: mcp-registry-publish", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)
        self.assertIn(
            "github.event_name != 'workflow_dispatch' "
            "|| github.ref == 'refs/heads/main'",
            self.workflow,
        )

    def test_registry_workflow_pins_and_verifies_the_publisher(self) -> None:
        self.assertIn('MCP_PUBLISHER_VERSION: "v1.8.0"', self.workflow)
        self.assertRegex(
            self.workflow,
            r'MCP_PUBLISHER_SHA256: "[0-9a-f]{64}"',
        )
        self.assertIn("sha256sum --check --strict", self.workflow)

    def test_registry_workflow_validates_before_authentication_and_publish(
        self,
    ) -> None:
        validate = self.workflow.index("./mcp-publisher validate server.json")
        launch = self.workflow.index("scripts/check_mcp_registry_launch.py")
        login = self.workflow.index("./mcp-publisher login github-oidc")
        publish = self.workflow.index("./mcp-publisher publish server.json")
        self.assertLess(validate, launch)
        self.assertLess(launch, login)
        self.assertLess(validate, login)
        self.assertLess(login, publish)

    def test_registry_launch_uses_the_pinned_uvx_runtime(self) -> None:
        self.assertIn("python -m pip install uv==0.12.7", self.workflow)
        self.assertIn("--attempts 6 --timeout 120", self.workflow)

    def test_release_waits_for_pypi_before_calling_registry_workflow(
        self,
    ) -> None:
        job = self.release_workflow.index("publish-mcp-registry:")
        dependency = self.release_workflow.index("needs: publish", job)
        workflow = self.release_workflow.index(
            "uses: ./.github/workflows/mcp-registry.yml",
            job,
        )
        self.assertLess(dependency, workflow)


if __name__ == "__main__":
    unittest.main()
