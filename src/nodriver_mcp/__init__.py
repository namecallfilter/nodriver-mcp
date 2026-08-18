"""Authenticated, scriptable browser scraping for Codex via MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("nodriver-mcp")
except PackageNotFoundError:  # pragma: no cover - editable source without metadata
    __version__ = "0.0.0"

__all__ = ["__version__"]
