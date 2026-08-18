from __future__ import annotations

import os
from pathlib import Path

import pytest

from nodriver_mcp.config import ConfigurationError, Settings


@pytest.fixture(autouse=True)
def clean_nodriver_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("NODRIVER_MCP_"):
            monkeypatch.delenv(name)


def test_settings_from_env_parses_paths_browser_and_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NODRIVER_MCP_DATA_DIR", str(tmp_path / "data home"))
    monkeypatch.setenv("NODRIVER_MCP_USER_DATA_DIR", "relative-profile")
    monkeypatch.setenv("NODRIVER_MCP_SCRIPTS_DIR", "relative-scrapers")
    monkeypatch.setenv("NODRIVER_MCP_ARTIFACTS_DIR", "relative-artifacts")
    monkeypatch.setenv("NODRIVER_MCP_BROWSER_EXECUTABLE", "bin/chrome.exe")
    monkeypatch.setenv(
        "NODRIVER_MCP_BROWSER_ARGS",
        '["--disable-extensions", "--window-size=1200,800"]',
    )
    monkeypatch.setenv("NODRIVER_MCP_HEADLESS", " YeS ")
    monkeypatch.setenv("NODRIVER_MCP_LANGUAGE", "en-GB")
    monkeypatch.setenv("NODRIVER_MCP_CONNECT_HOST", "127.0.0.1")
    monkeypatch.setenv("NODRIVER_MCP_CONNECT_PORT", "9222")
    monkeypatch.setenv("NODRIVER_MCP_PROFILE_DIRECTORY", "Automation")
    monkeypatch.setenv("NODRIVER_MCP_SCRAPER_TIMEOUT_SECONDS", "37")
    monkeypatch.setenv("NODRIVER_MCP_MAX_INLINE_RESULT_BYTES", "4096")
    monkeypatch.setenv("NODRIVER_MCP_MAX_SCRIPT_BYTES", "8192")
    monkeypatch.setenv("NODRIVER_MCP_MAX_TABS_PER_RUN", "7")

    settings = Settings.from_env()

    assert settings.user_data_dir == (tmp_path / "relative-profile").resolve()
    assert settings.scripts_dir == (tmp_path / "relative-scrapers").resolve()
    assert settings.artifacts_dir == (tmp_path / "relative-artifacts").resolve()
    assert settings.browser_executable_path == (tmp_path / "bin/chrome.exe").resolve()
    assert settings.browser_args == (
        "--disable-extensions",
        "--window-size=1200,800",
    )
    assert settings.headless is True
    assert settings.language == "en-GB"
    assert settings.attach_mode is True
    assert settings.connect_host == "127.0.0.1"
    assert settings.connect_port == 9222
    assert settings.profile_directory == "Automation"
    assert settings.scraper_timeout_seconds == 37
    assert settings.operation_timeout_seconds == 117
    assert settings.max_inline_result_bytes == 4096
    assert settings.max_script_bytes == 8192
    assert settings.max_tabs_per_run == 7


def test_settings_default_paths_are_stable_under_configured_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NODRIVER_MCP_DATA_DIR", str(tmp_path / "private-state"))

    settings = Settings.from_env()

    assert settings.user_data_dir == (tmp_path / "private-state/browser-profile").resolve()
    assert settings.scripts_dir == (tmp_path / "scrapers").resolve()
    assert settings.artifacts_dir == (tmp_path / "artifacts").resolve()
    assert settings.browser_args == ()
    assert settings.headless is False
    assert settings.attach_mode is False
    assert settings.operation_timeout_seconds == 200


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({"NODRIVER_MCP_HEADLESS": "sometimes"}, "must be true or false"),
        ({"NODRIVER_MCP_BROWSER_ARGS": "not-json"}, "must be a JSON array"),
        ({"NODRIVER_MCP_BROWSER_ARGS": '["ok", 1]'}, "array of strings"),
        ({"NODRIVER_MCP_CONNECT_HOST": "127.0.0.1"}, "must be set together"),
        ({"NODRIVER_MCP_CONNECT_PORT": "9222"}, "must be set together"),
        (
            {
                "NODRIVER_MCP_CONNECT_HOST": "192.0.2.1",
                "NODRIVER_MCP_CONNECT_PORT": "9222",
            },
            "loopback host",
        ),
        (
            {
                "NODRIVER_MCP_CONNECT_HOST": "127.0.0.1",
                "NODRIVER_MCP_CONNECT_PORT": "70000",
            },
            "between 1 and 65535",
        ),
        ({"NODRIVER_MCP_PROFILE_DIRECTORY": "../Default"}, "profile basename"),
        ({"NODRIVER_MCP_MAX_TABS_PER_RUN": "0"}, "between 1 and 50"),
        (
            {
                "NODRIVER_MCP_CONNECT_WEBSOCKET_URL": ("ws://127.0.0.1:9222/devtools/browser/id"),
                "NODRIVER_MCP_CONNECT_HOST": "127.0.0.1",
                "NODRIVER_MCP_CONNECT_PORT": "9222",
            },
            "either NODRIVER_MCP_CONNECT_WEBSOCKET_URL",
        ),
    ],
)
def test_settings_reject_invalid_environment(
    environment: dict[str, str], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError, match=message):
        Settings.from_env()


def test_public_browser_config_omits_credentials_and_debugger_endpoint(
    settings_factory,
) -> None:
    settings = settings_factory(
        connect_host="127.0.0.1",
        connect_port=9222,
        browser_executable_path=Path("C:/private/chrome.exe"),
    )

    public = settings.public_browser_config()

    assert public["mode"] == "attach-http"
    assert "connect_host" not in public
    assert "connect_port" not in public
    assert "browser_executable_path" not in public
    assert "user_data_dir" not in public


def test_settings_supports_private_live_browser_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket_url = "ws://127.0.0.1:9222/devtools/browser/nodriver-mcp"
    monkeypatch.setenv("NODRIVER_MCP_CONNECT_WEBSOCKET_URL", websocket_url)

    settings = Settings.from_env()
    public = settings.public_browser_config()

    assert settings.attach_mode is True
    assert settings.connect_websocket_url == websocket_url
    assert public["mode"] == "attach-websocket"
    assert websocket_url not in str(public)


@pytest.mark.parametrize(
    "websocket_url",
    [
        "http://127.0.0.1:9222/devtools/browser/id",
        "ws://192.0.2.1:9222/devtools/browser/id",
        "ws://127.0.0.1:9222/devtools/page/id",
        "ws://user:secret@127.0.0.1:9222/devtools/browser/id",
        "ws://127.0.0.1:not-a-port/devtools/browser/id",
    ],
)
def test_settings_rejects_unsafe_live_browser_websocket(
    websocket_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NODRIVER_MCP_CONNECT_WEBSOCKET_URL", websocket_url)

    with pytest.raises(ConfigurationError, match="WebSocket URL|loopback"):
        Settings.from_env()


def test_operation_timeout_covers_all_configured_stage_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NODRIVER_MCP_SCRAPER_TIMEOUT_SECONDS", "600")
    monkeypatch.setenv("NODRIVER_MCP_BROWSER_START_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("NODRIVER_MCP_NAVIGATION_TIMEOUT_SECONDS", "120")

    settings = Settings.from_env()

    assert settings.operation_timeout_seconds == 860

    monkeypatch.setenv("NODRIVER_MCP_OPERATION_TIMEOUT_SECONDS", "700")
    with pytest.raises(ConfigurationError, match="must be at least the sum"):
        Settings.from_env()
