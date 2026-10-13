"""Automated Windows EXE, CLI entry point, and packaging verification tests (Section 18)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.main import main as app_main
from src.utils.paths import get_bundle_dir


@pytest.mark.packaging
def test_build_script_and_entry_points_exist() -> None:
    root = get_bundle_dir()
    build_script = root / "build_exe.ps1"
    run_py = root / "run.py"
    main_py = root / "src" / "main.py"

    assert build_script.exists() and build_script.stat().st_size > 100
    assert run_py.exists() and run_py.stat().st_size > 50
    assert main_py.exists() and main_py.stat().st_size > 100

    script_text = build_script.read_text(encoding="utf-8")
    assert "PyInstaller" in script_text or "pyinstaller" in script_text
    assert "--collect-all" in script_text
    assert "customtkinter" in script_text


@pytest.mark.packaging
def test_cli_entry_points_check_env_and_smoke_test_ui(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    isolated_dir = tmp_path / "cli_isolated_data"
    rc_env = app_main(["--check-env", "--data-dir", str(isolated_dir)])
    assert rc_env == 0
    captured_env = capsys.readouterr()
    assert "Diagnostics OK" in captured_env.out
    assert "yt-dlp" in captured_env.out
    assert (isolated_dir / "logs" / "app.log").exists()

    root = get_bundle_dir()
    res_ui = subprocess.run(
        [sys.executable, str(root / "run.py"), "--smoke-test-ui", "--data-dir", str(isolated_dir)],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res_ui.returncode == 0, f"STDERR: {res_ui.stderr}\nSTDOUT: {res_ui.stdout}"
    assert (isolated_dir / "history.db").exists()
    assert (isolated_dir / "logs" / "app.log").exists()


@pytest.mark.packaging
def test_packaged_windows_exe_bundle_and_isolated_execution(tmp_path: Path) -> None:
    root = get_bundle_dir()
    exe_path = root / "dist" / "YouTubeDownloaderStudio" / "YouTubeDownloaderStudio.exe"
    internal_dir = root / "dist" / "YouTubeDownloaderStudio" / "_internal"

    if not exe_path.exists():
        pytest.skip(f"Packaged executable not built yet at {exe_path}")

    # Verify Windows PE header ('MZ') and non-trivial size
    with exe_path.open("rb") as f:
        magic = f.read(2)
    assert magic == b"MZ"
    assert exe_path.stat().st_size > 100_000

    # Verify bundled assets and libraries in _internal
    assert internal_dir.exists() and internal_dir.is_dir()
    assert (internal_dir / "customtkinter").exists()
    assert (internal_dir / "assets" / "app_icon.ico").exists()

    # Run from an external working directory with spaces in --data-dir
    external_cwd = tmp_path / "External Launch Folder"
    external_cwd.mkdir(parents=True, exist_ok=True)
    isolated_data_dir = tmp_path / "Isolated App Data With Spaces"

    # 1. --check-env from packaged EXE
    res_env = subprocess.run(
        [str(exe_path), "--check-env", "--data-dir", str(isolated_data_dir)],
        cwd=str(external_cwd),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res_env.returncode == 0, f"STDERR: {res_env.stderr}\nSTDOUT: {res_env.stdout}"
    assert "Diagnostics OK" in res_env.stdout
    assert "yt-dlp" in res_env.stdout
    assert (isolated_data_dir / "logs" / "app.log").exists()

    # 2. --smoke-test-ui from packaged EXE (First launch creates SQLite DB and logs)
    res_ui_1 = subprocess.run(
        [str(exe_path), "--smoke-test-ui", "--data-dir", str(isolated_data_dir)],
        cwd=str(external_cwd),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert res_ui_1.returncode == 0, f"STDERR: {res_ui_1.stderr}\nSTDOUT: {res_ui_1.stdout}"
    assert "Startup dependency check:" in res_ui_1.stderr
    assert (isolated_data_dir / "history.db").exists()
    assert (isolated_data_dir / "logs" / "app.log").exists()

    # 3. Second launch verifies clean restart & SQLite/settings reload
    res_ui_2 = subprocess.run(
        [str(exe_path), "--smoke-test-ui", "--data-dir", str(isolated_data_dir)],
        cwd=str(external_cwd),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert res_ui_2.returncode == 0, f"STDERR: {res_ui_2.stderr}\nSTDOUT: {res_ui_2.stdout}"
    assert "Startup dependency check:" in res_ui_2.stderr
