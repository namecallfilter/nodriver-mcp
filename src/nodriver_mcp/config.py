"""Environment-backed configuration for the local MCP server."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    """Raised when an environment setting is invalid."""


def _data_home() -> Path:
    override = os.getenv("NODRIVER_MCP_DATA_DIR")
    if override:
        return _path(override)
    if sys.platform == "win32":
        root = os.getenv("LOCALAPPDATA")
        if root:
            return Path(root).resolve() / "nodriver-mcp"
    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / "nodriver-mcp").resolve()
    root = os.getenv("XDG_DATA_HOME")
    if root:
        return Path(root).expanduser().resolve() / "nodriver-mcp"
    return (Path.home() / ".local" / "share" / "nodriver-mcp").resolve()


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve()


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false, got {raw!r}")


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _browser_args() -> tuple[str, ...]:
    raw = os.getenv("NODRIVER_MCP_BROWSER_ARGS")
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigurationError("NODRIVER_MCP_BROWSER_ARGS must be a JSON array") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError("NODRIVER_MCP_BROWSER_ARGS must be a JSON array of strings")
    return tuple(value)


def _websocket_url() -> str | None:
    raw = os.getenv("NODRIVER_MCP_CONNECT_WEBSOCKET_URL")
    if not raw:
        return None
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError(
            "NODRIVER_MCP_CONNECT_WEBSOCKET_URL must be a valid WebSocket URL"
        ) from exc
    if (
        parsed.scheme != "ws"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or not (parsed.path == "/devtools/browser" or parsed.path.startswith("/devtools/browser/"))
    ):
        raise ConfigurationError(
            "NODRIVER_MCP_CONNECT_WEBSOCKET_URL must be a loopback "
            "ws:// URL with a /devtools/browser path"
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """All server settings, loaded once when the MCP process starts."""

    user_data_dir: Path
    profile_directory: str
    scripts_dir: Path
    artifacts_dir: Path
    browser_executable_path: Path | None = None
    browser_args: tuple[str, ...] = ()
    headless: bool = False
    language: str = "en-US"
    connect_host: str | None = None
    connect_port: int | None = None
    connect_websocket_url: str | None = None
    scraper_timeout_seconds: int = 120
    max_inline_result_bytes: int = 512 * 1024
    max_artifact_result_bytes: int = 50 * 1024 * 1024
    max_script_bytes: int = 256 * 1024
    max_tabs_per_run: int = 12
    browser_start_timeout_seconds: int = 30
    navigation_timeout_seconds: int = 30
    operation_timeout_seconds: int = 200
    operation_queue_timeout_seconds: int = 5

    @classmethod
    def from_env(cls) -> Settings:
        data_home = _data_home()
        working_dir = Path.cwd().resolve()
        executable = os.getenv("NODRIVER_MCP_BROWSER_EXECUTABLE")
        raw_host = os.getenv("NODRIVER_MCP_CONNECT_HOST")
        host = raw_host.strip() if raw_host else None
        websocket_url = _websocket_url()
        raw_port = os.getenv("NODRIVER_MCP_CONNECT_PORT")
        port = None
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError as exc:
                raise ConfigurationError("NODRIVER_MCP_CONNECT_PORT must be an integer") from exc
            if not 1 <= port <= 65535:
                raise ConfigurationError("NODRIVER_MCP_CONNECT_PORT must be between 1 and 65535")
        if bool(host) != bool(port):
            raise ConfigurationError(
                "NODRIVER_MCP_CONNECT_HOST and NODRIVER_MCP_CONNECT_PORT must be set together"
            )
        if host and host not in {"127.0.0.1", "localhost", "::1"}:
            raise ConfigurationError(
                "NODRIVER_MCP_CONNECT_HOST must be a loopback host: 127.0.0.1, localhost, or ::1"
            )
        if websocket_url and host:
            raise ConfigurationError(
                "Set either NODRIVER_MCP_CONNECT_WEBSOCKET_URL or "
                "CONNECT_HOST/CONNECT_PORT, not both"
            )

        profile_directory = os.getenv("NODRIVER_MCP_PROFILE_DIRECTORY", "Default").strip()
        if not profile_directory or any(char in profile_directory for char in "/\\\0"):
            raise ConfigurationError(
                "NODRIVER_MCP_PROFILE_DIRECTORY must be a profile basename such as 'Default'"
            )

        scraper_timeout_seconds = _integer("NODRIVER_MCP_SCRAPER_TIMEOUT_SECONDS", 120, 1, 600)
        browser_start_timeout_seconds = _integer(
            "NODRIVER_MCP_BROWSER_START_TIMEOUT_SECONDS", 30, 1, 120
        )
        navigation_timeout_seconds = _integer("NODRIVER_MCP_NAVIGATION_TIMEOUT_SECONDS", 30, 1, 120)
        minimum_operation_timeout = (
            scraper_timeout_seconds + browser_start_timeout_seconds + navigation_timeout_seconds
        )
        operation_timeout_default = minimum_operation_timeout + 20
        operation_timeout_seconds = _integer(
            "NODRIVER_MCP_OPERATION_TIMEOUT_SECONDS",
            operation_timeout_default,
            10,
            1000,
        )
        if operation_timeout_seconds < minimum_operation_timeout:
            raise ConfigurationError(
                "NODRIVER_MCP_OPERATION_TIMEOUT_SECONDS must be at least the sum of "
                "the browser startup, navigation, and scraper timeouts "
                f"({minimum_operation_timeout} seconds)"
            )

        return cls(
            user_data_dir=_path(
                os.getenv("NODRIVER_MCP_USER_DATA_DIR", str(data_home / "browser-profile"))
            ),
            profile_directory=profile_directory,
            scripts_dir=_path(os.getenv("NODRIVER_MCP_SCRIPTS_DIR", str(working_dir / "scrapers"))),
            artifacts_dir=_path(
                os.getenv("NODRIVER_MCP_ARTIFACTS_DIR", str(working_dir / "artifacts"))
            ),
            browser_executable_path=_path(executable) if executable else None,
            browser_args=_browser_args(),
            headless=_bool("NODRIVER_MCP_HEADLESS", False),
            language=os.getenv("NODRIVER_MCP_LANGUAGE", "en-US"),
            connect_host=host,
            connect_port=port,
            connect_websocket_url=websocket_url,
            scraper_timeout_seconds=scraper_timeout_seconds,
            max_inline_result_bytes=_integer(
                "NODRIVER_MCP_MAX_INLINE_RESULT_BYTES", 512 * 1024, 1024, 10 * 1024 * 1024
            ),
            max_artifact_result_bytes=_integer(
                "NODRIVER_MCP_MAX_ARTIFACT_RESULT_BYTES",
                50 * 1024 * 1024,
                1024,
                500 * 1024 * 1024,
            ),
            max_script_bytes=_integer(
                "NODRIVER_MCP_MAX_SCRIPT_BYTES", 256 * 1024, 1024, 1024 * 1024
            ),
            max_tabs_per_run=_integer("NODRIVER_MCP_MAX_TABS_PER_RUN", 12, 1, 50),
            browser_start_timeout_seconds=browser_start_timeout_seconds,
            navigation_timeout_seconds=navigation_timeout_seconds,
            operation_timeout_seconds=operation_timeout_seconds,
            operation_queue_timeout_seconds=_integer(
                "NODRIVER_MCP_OPERATION_QUEUE_TIMEOUT_SECONDS", 5, 1, 120
            ),
        )

    @property
    def attach_mode(self) -> bool:
        return self.connect_websocket_url is not None or (
            self.connect_host is not None and self.connect_port is not None
        )

    def public_browser_config(self) -> dict[str, object]:
        """Return troubleshooting details without debugger endpoints or cookie data."""
        return {
            "mode": (
                "attach-websocket"
                if self.connect_websocket_url
                else "attach-http"
                if self.attach_mode
                else "managed"
            ),
            "headless": self.headless,
            "profile_directory": self.profile_directory,
            "persistent_profile_exists": self.user_data_dir.exists(),
            "scripts_dir": str(self.scripts_dir),
            "artifacts_dir": str(self.artifacts_dir),
        }
