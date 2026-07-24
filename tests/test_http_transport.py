"""Integration test for MCP over streamable HTTP: spawn the real server as a
subprocess, connect with the real MCP client, do a write -> read round-trip."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

EXPECTED_TOOLS = {"remember", "update", "recall", "get_memory_schema", "link", "forget", "export"}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def http_url(tmp_path):
    port = _free_port()
    env = {
        **os.environ,
        "OBS_DATA_DIR": str(tmp_path / "data"),
        "OBS_USER": "testuser",
        "OBS_BACKEND": "sqlite",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "openbrainstore.cli", "serve", "--http", "--port", str(port)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError("server process exited early")
                if time.monotonic() > deadline:
                    raise RuntimeError("server did not start listening in time")
                time.sleep(0.3)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _tool_payload(result) -> dict:
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


def test_http_roundtrip(http_url):
    async def run():
        async with streamablehttp_client(http_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                assert {t.name for t in tools.tools} == EXPECTED_TOOLS

                res = _tool_payload(await session.call_tool(
                    "remember",
                    {
                        "content": "The HTTP transport round-trip works.",
                        "type": "fact",
                        "tags": ["transport"],
                    },
                ))
                mem_id = res["id"]
                assert mem_id.startswith("mem_")

                res = _tool_payload(await session.call_tool(
                    "recall", {"query": "HTTP transport round-trip"}
                ))
                assert [r["id"] for r in res["results"]] == [mem_id]

                res = _tool_payload(await session.call_tool("get_memory_schema", {}))
                assert res["total_memories"] == 1

    asyncio.run(run())
