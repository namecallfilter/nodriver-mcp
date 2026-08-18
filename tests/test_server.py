from __future__ import annotations

from typing import Any

import pytest
from mcp.client import Client

from nodriver_mcp.server import SERVER_INSTRUCTIONS, create_server


@pytest.mark.asyncio
async def test_mcp_v2_tool_metadata_and_scraper_crud_without_browser(
    settings_factory,
) -> None:
    settings = settings_factory()
    browser_start_calls: list[dict[str, Any]] = []

    async def forbidden_browser_start(**kwargs: Any) -> None:
        browser_start_calls.append(kwargs)
        raise AssertionError("scraper CRUD must not launch a browser")

    server = create_server(settings, browser_starter=forbidden_browser_start)

    async with Client(server, raise_exceptions=True) as client:
        tools_result = await client.list_tools()
        tools = {tool.name: tool for tool in tools_result.tools}

        assert set(tools) == {
            "browser_status",
            "browser_open",
            "browser_snapshot",
            "browser_query",
            "browser_close",
            "scraper_list",
            "scraper_get",
            "scraper_save",
            "scraper_run",
        }
        expected_surface = {
            "browser_status": (
                "Browser status",
                "Report browser state and metadata for MCP-accessible tabs.",
            ),
            "browser_open": (
                "Open browser tab",
                "Open or navigate an MCP tab, reusing the active tab unless new_tab is true.",
            ),
            "browser_snapshot": (
                "Read page content",
                "Return bounded visible text or outer HTML, optionally scoped to a CSS selector.",
            ),
            "browser_query": (
                "Query page elements",
                "Return text and safe attributes for elements matching a CSS selector.",
            ),
            "browser_close": (
                "Close browser session",
                "Close the managed browser, or close MCP-owned tabs and detach in attach mode.",
            ),
            "scraper_list": ("List scrapers", "List metadata for saved scrapers."),
            "scraper_get": ("Read scraper", "Return a saved scraper's source and metadata."),
            "scraper_save": (
                "Save scraper",
                "Validate and atomically save a scraper for the next run.",
            ),
            "scraper_run": (
                "Run scraper",
                "Hot-load and run a saved scraper. Use either url or tab_id; new_tab requires url.",
            ),
        }
        assert {
            name: (tool.title, tool.description) for name, tool in tools.items()
        } == expected_surface
        assert all("\n" not in (tool.description or "") for tool in tools.values())
        assert client.instructions == SERVER_INSTRUCTIONS
        assert tools["browser_status"].annotations.read_only_hint is True
        assert tools["browser_status"].annotations.open_world_hint is False
        assert tools["browser_open"].annotations.destructive_hint is True
        assert tools["scraper_list"].annotations.read_only_hint is True
        assert tools["scraper_get"].annotations.read_only_hint is True
        assert tools["scraper_save"].annotations.read_only_hint is False
        assert tools["scraper_save"].annotations.destructive_hint is True
        assert tools["scraper_save"].annotations.idempotent_hint is False
        assert tools["scraper_run"].annotations.read_only_hint is False
        assert tools["scraper_run"].annotations.destructive_hint is True
        assert tools["scraper_run"].annotations.idempotent_hint is False
        assert tools["scraper_run"].annotations.open_world_hint is True
        assert all(tool.output_schema is not None for tool in tools.values())
        for tool_name in ("browser_open", "scraper_run"):
            headless_schema = tools[tool_name].input_schema["properties"]["headless"]
            assert headless_schema["default"] is None
            assert headless_schema["anyOf"][0]["type"] == "boolean"
            assert "Changing mode restarts" in headless_schema["anyOf"][0]["description"]
        assert (
            tools["browser_open"].input_schema["properties"]["new_tab"]["description"]
            == "Create a new MCP-owned tab instead of reusing the active tab"
        )
        assert (
            tools["scraper_run"].input_schema["properties"]["params"]["anyOf"][0]["description"]
            == "JSON object passed to scrape(ctx, params)"
        )
        assert (
            "compare-and-swap"
            in (
                tools["scraper_save"].input_schema["properties"]["expected_revision"]["anyOf"][0][
                    "description"
                ]
            )
        )
        assert "Call browser_status first" in client.instructions
        assert "prompt explicitly asks for headless" in client.instructions
        assert "pass headless=false otherwise" in client.instructions

        source = 'async def scrape(ctx, params):\n    return {"saved": True}\n'
        saved = await client.call_tool("scraper_save", {"name": "demo", "source": source})
        assert saved.is_error is False
        assert saved.structured_content["name"] == "demo"
        assert saved.structured_content["source"] == source
        revision = saved.structured_content["revision"]

        listed = await client.call_tool("scraper_list", {})
        assert listed.is_error is False
        assert listed.structured_content["scrapers"] == [
            {
                "name": "demo",
                "revision": revision,
                "bytes": len(source.encode()),
                "modified_at": saved.structured_content["modified_at"],
            }
        ]

        fetched = await client.call_tool("scraper_get", {"name": "demo"})
        assert fetched.is_error is False
        assert fetched.structured_content == saved.structured_content

    assert browser_start_calls == []
