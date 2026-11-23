"""Per-account catalog visibility over a real MCP stdio session
(docs/specs/skill-catalog.md): opt-in hides the tools and refuses calls until the
account turns the catalog on; 'all' shows them to everyone."""

import asyncio
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CATALOG_TOOLS = {"agent_startup", "get_skill", "log_episode", "propose_skill"}


def _env(tmp_path, mode: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OBS_")}
    env.update({"OBS_DATA_DIR": str(tmp_path), "OBS_BACKEND": "sqlite", "OBS_USER": "fresh-user",
                "OBS_CATALOG": mode})
    return env


def _session(env: dict) -> tuple[set, str]:
    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-m", "openbrainstore.cli", "serve"],
                                       env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                names = {t.name for t in (await s.list_tools()).tools}
                res = await s.call_tool("agent_startup", {"task": "test"})
                return names, res.content[0].text
    return asyncio.run(run())


def test_optin_hides_until_enabled(tmp_path):
    env = _env(tmp_path, "1")
    names, text = _session(env)
    assert not (names & CATALOG_TOOLS)
    assert "off for this account" in text  # a client that calls anyway is refused
    subprocess.run([sys.executable, "-m", "openbrainstore.cli", "catalog", "enable"], env=env,
                   check=True, capture_output=True)
    names, text = _session(env)
    assert CATALOG_TOOLS <= names
    assert text.startswith("# Catalog session")


def test_all_mode_shows_everyone(tmp_path):
    names, text = _session(_env(tmp_path, "all"))
    assert CATALOG_TOOLS <= names and text.startswith("# Catalog session")
