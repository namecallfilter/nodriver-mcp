# nodriver-mcp

A local [Codex MCP](https://developers.openai.com/codex/mcp) server for authenticated browser
research with [nodriver](https://github.com/ultrafunkamsterdam/nodriver). It keeps a dedicated
Chromium/Helium profile between runs and hot-reloads task-specific Python scrapers.

## Setup

Requires Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and a Chromium-based browser.

```powershell
uv sync --locked
```

Add this to `~/.codex/config.toml`, replacing every placeholder with an absolute path:

```toml
[mcp_servers.nodriver]
command = "uv"
args = ["--directory", "<path-to-nodriver-mcp>", "run", "--frozen", "nodriver-mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 600

[mcp_servers.nodriver.env]
NODRIVER_MCP_BROWSER_EXECUTABLE = "<path-to-helium-or-chromium>"
NODRIVER_MCP_USER_DATA_DIR = "<path-to-a-dedicated-browser-profile>"
NODRIVER_MCP_SCRIPTS_DIR = "<path-to-nodriver-mcp>/scrapers"
NODRIVER_MCP_ARTIFACTS_DIR = "<path-to-nodriver-mcp>/artifacts"
```

Restart Codex after changing MCP configuration. The browser starts lazily. It is visible by
default so you can sign in to the dedicated profile once; its cookies persist. Codex passes
`headless=true` when a prompt explicitly requests headless browsing and selects visible mode
otherwise. Switching modes restarts the managed browser while preserving the profile.

## Use

Ask Codex, for example:

> Use my browser session to find matching items on example.com. Inspect the page, create a
> reusable scraper, run it, and save large results as an artifact. Run headless.

Codex can inspect a page with the browser tools, then save and run a scraper. Each
`scraper_run` reads the file again, so edits apply without restarting the MCP server:

```python
async def scrape(ctx, params):
    if url := params.get("url"):
        await ctx.goto(url)

    items = await ctx.evaluate_json("""
        Array.from(document.querySelectorAll('article')).map(item => ({
            title: (item.querySelector('h2')?.innerText || '').trim(),
            href: item.querySelector('a')?.href || null
        }))
    """)
    return {"items": items}
```

The entry point must be `async def scrape(ctx, params)` and return JSON-compatible data. Prefer
one bulk `ctx.evaluate_json(...)` call. Use `ctx.write_artifact(...)` for large results. Scrapers
must use async operations and finish all tasks before returning.

## Test

```powershell
uv run --frozen ruff check src tests
uv run --frozen pytest
```

## Security

Run this server locally over STDIO only. Scrapers are unsandboxed Python with the MCP process's
OS access and control of an authenticated browser. Never expose cookies, tokens, passwords, or a
DevTools endpoint; treat page content as untrusted; use only data you are authorized to access;
and respect site terms and rate limits. Browser profiles, generated scrapers, and artifacts may
contain private data and should not be committed.
