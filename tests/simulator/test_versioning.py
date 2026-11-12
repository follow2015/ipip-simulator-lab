"""版本声明与发布 tag 的一致性契约。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_release_tag.py"

from idc_simulator import __version__  # noqa: E402  (需在 ROOT 计算后导入以便定位)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ROOT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(ROOT),
    )


def test_version_is_a_plain_semver_string():
    parts = __version__.split(".")
    assert len(parts) == 3, f"版本应是 X.Y.Z：{__version__!r}"
    assert all(p.isdigit() for p in parts), f"版本段必须是数字：{__version__!r}"


def test_pyproject_declares_version_as_dynamic():
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert "version" not in project, "pyproject 的 [project] 里不应再有静态 version"
    assert "version" in project["dynamic"]
    assert data["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "idc_simulator.__version__"


def test_tag_matching_version_passes():
    result = run("--tag", f"v{__version__}")
    assert result.returncode == 0, result.stderr
    assert "版本一致" in result.stdout


def test_tag_without_v_prefix_is_accepted():
    assert run("--tag", __version__).returncode == 0


def test_mismatched_tag_fails():
    result = run("--tag", "v9.9.9")
    assert result.returncode == 1
    assert "不一致" in result.stderr


def test_no_tag_flag_only_checks_internals():
    result = run("--no-tag")
    assert result.returncode == 0, result.stderr
    assert "单一源" in result.stdout


def test_missing_tag_is_usage_error(monkeypatch):
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    result = run()
    assert result.returncode == 2
    assert "没有 tag" in result.stderr


def test_hardcoded_version_in_source_is_rejected(tmp_path):
    shutil_root = tmp_path
    (shutil_root / "idc_simulator").mkdir()
    (shutil_root / "idc_simulator" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8"
    )
    (shutil_root / "idc_simulator" / "runtime.py").write_text(
        'VERSION = "1.2.3"\n', encoding="utf-8"
    )
    (shutil_root / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndynamic = ["version"]\n'
        '[tool.setuptools.dynamic]\nversion = {attr = "idc_simulator.__version__"}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(shutil_root), "--no-tag"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "硬编码版本" in result.stderr


def test_static_version_in_pyproject_is_rejected(tmp_path):
    (tmp_path / "idc_simulator").mkdir()
    (tmp_path / "idc_simulator" / "__init__.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "1.2.3"\ndynamic = ["version"]\n'
        '[tool.setuptools.dynamic]\nversion = {attr = "idc_simulator.__version__"}\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path), "--no-tag"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1
    assert "写死了 version" in result.stderr


@pytest.mark.parametrize("tag", ["V1.2.3", "v1.2", "1.2.3.4", ""])
def test_non_conforming_tags_are_not_silently_equal(tag):
    result = run("--tag", tag)
    if tag == "":
        assert result.returncode == 2
    else:
        assert result.returncode == 1
