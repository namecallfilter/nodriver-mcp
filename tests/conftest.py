from __future__ import annotations

from pathlib import Path

import pytest

from nodriver_mcp.config import Settings


@pytest.fixture
def settings_factory(tmp_path: Path):
    def build(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "user_data_dir": tmp_path / "profile",
            "profile_directory": "Default",
            "scripts_dir": tmp_path / "scrapers",
            "artifacts_dir": tmp_path / "artifacts",
            "scraper_timeout_seconds": 2,
            "max_inline_result_bytes": 512 * 1024,
            "max_script_bytes": 256 * 1024,
            "max_tabs_per_run": 4,
        }
        values.update(overrides)
        return Settings(**values)  # type: ignore[arg-type]

    return build
