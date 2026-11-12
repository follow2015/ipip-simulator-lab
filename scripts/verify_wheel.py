#!/usr/bin/env python3
"""verify_wheel.py — 校验构建出的 wheel 与源码树一致，且没有夹带非交付物。"""

from __future__ import annotations

import ast
import sys
import zipfile
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

PACKAGE = "idc_simulator"

REQUIRED_ASSETS = (
    f"{PACKAGE}/web/index.html",
    f"{PACKAGE}/web/app.js",
    f"{PACKAGE}/web/styles.css",
)

FORBIDDEN_PREFIXES = (
    "tests/",
    "docs/",
    "scripts/",
    "deploy/",
    "examples/",
    ".github/",
    f"{PACKAGE}-deploy/",
)


def source_modules(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in (root / PACKAGE).rglob("*.py")
        if "__pycache__" not in path.parts
    }


def wheel_members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def metadata_version(wheel: Path) -> str | None:
    with zipfile.ZipFile(wheel) as archive:
        for name in archive.namelist():
            if name.endswith(".dist-info/METADATA"):
                for line in archive.read(name).decode("utf-8", "replace").splitlines():
                    if line.startswith("Version: "):
                        return line.split(": ", 1)[1].strip()
    return None


def expected_version(root: Path) -> str:
    tree = ast.parse((root / PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets):
                return str(node.value.value)
    raise SystemExit("无法从 __init__.py 读取 __version__")


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("用法: verify_wheel.py <wheel 路径>", file=sys.stderr)
        return EXIT_USAGE

    wheel = Path(argv[0])
    if not wheel.is_file():
        print(f"错误：找不到 {wheel}", file=sys.stderr)
        return EXIT_USAGE

    root = Path.cwd()
    problems: list[str] = []
    members = wheel_members(wheel)

    packaged = {m for m in members if m.startswith(f"{PACKAGE}/") and m.endswith(".py")}
    expected = source_modules(root)
    missing = sorted(expected - packaged)
    extra = sorted(packaged - expected)
    if missing:
        problems.append("wheel 缺少源码树中的模块：\n    " + "\n    ".join(missing))
    if extra:
        problems.append("wheel 含源码树里不存在的模块：\n    " + "\n    ".join(extra))

    for asset in REQUIRED_ASSETS:
        if asset not in members:
            problems.append(f"缺少运行期资产：{asset}")
    if not any(m.startswith(f"{PACKAGE}/profiles/") and m.endswith(".yaml") for m in members):
        problems.append("缺少 profile 资产：idc_simulator/profiles/**/*.yaml 一个都没有")

    strays = [m for m in members if m.startswith(FORBIDDEN_PREFIXES)]
    if strays:
        problems.append("wheel 夹带了非交付物：\n    " + "\n    ".join(strays[:20]))

    version = expected_version(root)
    declared = metadata_version(wheel)
    if declared != version:
        problems.append(f"METADATA 版本 {declared!r} 与 __version__ {version!r} 不一致")

    if problems:
        print("❌ wheel 校验失败：", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return EXIT_FAIL

    print(
        f"✅ wheel 校验通过：{wheel.name}（{len(packaged)} 个模块，"
        f"{sum(1 for m in members if m.startswith(f'{PACKAGE}/profiles/'))} 个 profile 资产，版本 {version}）"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
