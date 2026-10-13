"""Pytest configuration, custom markers, and opt-in live integration test handling."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register CLI options for opt-in live network download integration tests."""
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="Run opt-in live network download integration tests (requires YDS_TEST_*_URL env vars).",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register custom pytest markers."""
    config.addinivalue_line(
        "markers",
        "live: Opt-in real network download integration tests (disabled by default).",
    )
    config.addinivalue_line(
        "markers",
        "gui: Automated CustomTkinter GUI tests.",
    )
    config.addinivalue_line(
        "markers",
        "packaging: Packaged Windows executable verification tests.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip @pytest.mark.live tests unless --run-live or YDS_RUN_LIVE_TESTS=1 is explicitly set."""
    run_live = config.getoption("--run-live") or os.environ.get("YDS_RUN_LIVE_TESTS") == "1"
    if run_live:
        return

    skip_live = pytest.mark.skip(
        reason="BLOCKED/OPT-IN: Live network download tests are disabled by default. Pass --run-live with authorized YDS_TEST_*_URL environment variables to execute."
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _isolate_app_data_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure every test defaults YTD_STUDIO_DATA_DIR to an isolated temporary folder.

    This guarantees no test can accidentally modify the user's real %LOCALAPPDATA% state.
    """
    isolated_dir = tmp_path / "_isolated_appdata"
    monkeypatch.setenv("YTD_STUDIO_DATA_DIR", str(isolated_dir))
