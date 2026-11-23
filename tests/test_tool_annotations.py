"""Every tool declares a title and annotations (readOnlyHint, destructiveHint,
openWorldHint): MCP hosts use them to decide what needs the user's confirmation, and
the Claude and ChatGPT app directories require them. The server sends no
`instructions`; when to use the catalog is up to the user's own agent instructions."""

import json
import os
import subprocess
import sys

CODE = """
import asyncio, json, openbrainstore.server as s
tools = asyncio.run(s.mcp.list_tools())
print(json.dumps({
    "instructions": s.mcp.instructions,
    "tools": {t.name: {"title": t.title,
                       "annotations": t.annotations.model_dump() if t.annotations else None}
              for t in tools},
}))
"""


def _server_info(tmp_path, catalog: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("OBS_CATALOG", "OBS_VIEWS")}
    env.update(OBS_DATA_DIR=str(tmp_path), OBS_BACKEND="sqlite")
    if catalog:
        env["OBS_CATALOG"] = "all"
    out = subprocess.run([sys.executable, "-c", CODE], env=env, capture_output=True, text=True,
                         check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_every_tool_has_a_title_and_hints(tmp_path):
    info = _server_info(tmp_path, catalog=True)
    assert len(info["tools"]) == 13
    for name, tool in info["tools"].items():
        ann = tool["annotations"]
        assert tool["title"] and ann and ann["title"] == tool["title"], name
        assert ann["readOnlyHint"] is not None, name
        assert ann["destructiveHint"] is not None, name
        assert ann["openWorldHint"] is False, name
        if not ann["readOnlyHint"]:
            assert ann["idempotentHint"] is not None, name


def test_hints_match_what_each_tool_does(tmp_path):
    tools = _server_info(tmp_path, catalog=True)["tools"]
    effect = {}
    for name, tool in tools.items():
        ann = tool["annotations"]
        effect[name] = ("read" if ann["readOnlyHint"]
                        else "change" if ann["destructiveHint"] else "add")
    assert effect == {
        "remember": "add", "link": "add", "log_episode": "add", "propose_skill": "add",
        "update": "change", "forget": "change",
        "recall": "read", "list_memories": "read", "graph": "read", "get_memory_schema": "read",
        "export": "read", "agent_startup": "read", "get_skill": "read",
    }
    assert tools["link"]["annotations"]["idempotentHint"] is True
    assert tools["forget"]["annotations"]["idempotentHint"] is True
    assert tools["remember"]["annotations"]["idempotentHint"] is False


def test_server_sends_no_instructions(tmp_path):
    assert _server_info(tmp_path, catalog=False)["instructions"] is None
    assert _server_info(tmp_path, catalog=True)["instructions"] is None
