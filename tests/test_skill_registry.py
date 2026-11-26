from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from hai_mcp import skill_registry
from hai_mcp.server import hai_health, mcp


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_is_offline_and_does_not_claim_installation(self):
        with patch("hai_mcp.server.get_control_plane") as cp, patch(
            "hai_mcp.skill_registry._read", side_effect=AssertionError("network used")
        ):
            cp.return_value.health.return_value = {"ok": True, "server": "hai-mcp"}
            result = json.loads(hai_health())
        self.assertEqual(result["server"], "hai-mcp")
        info = result["skill_registry"]
        self.assertEqual(info["index_resource"], "hai://skills/index")
        for state in ("accessibility", "local_installation", "activation"):
            self.assertEqual(info[state], "not_checked")

    async def test_mcp_resources_return_canonical_index_and_instructions(self):
        resources = await mcp.list_resources()
        templates = await mcp.list_resource_templates()
        self.assertIn("hai://skills/index", [str(r.uri) for r in resources])
        self.assertIn("hai://skills/{category}/{name}", [str(t.uriTemplate) for t in templates])
        def fake_read(path):
            return {"README.md": "| [prior-art-search](./skills/engineering/prior-art-search/SKILL.md) | Search before building |", "skills/engineering/prior-art-search/SKILL.md": "# Original instructions"}[path]
        with patch("hai_mcp.skill_registry._read", side_effect=fake_read):
            index = await mcp.read_resource("hai://skills/index")
            skill = await mcp.read_resource("hai://skills/engineering/prior-art-search")
        self.assertIn("Search before building", list(index)[0].content)
        self.assertEqual(list(skill)[0].content, "# Original instructions")

    async def test_failure_is_confined_and_invalid_paths_never_fetch(self):
        with patch("hai_mcp.skill_registry._read", side_effect=ValueError("offline")):
            with self.assertRaises(Exception):
                await mcp.read_resource("hai://skills/index")
        with patch("hai_mcp.skill_registry._read") as read:
            with self.assertRaises(ValueError):
                skill_registry.read_skill("..", "secret")
            read.assert_not_called()

    async def test_health_error_shape_is_unchanged(self):
        with patch("hai_mcp.server.get_control_plane") as cp:
            cp.return_value.health.return_value = {"ok": False, "error": "missing_project"}
            self.assertEqual(json.loads(hai_health("bad")), {"ok": False, "error": "missing_project"})


if __name__ == "__main__":
    unittest.main()
