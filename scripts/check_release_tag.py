#!/usr/bin/env python3
"""check_release_tag.py — 校验发布 tag 与包版本一致，并守住"版本只有一个源"。"""

from __future__ import annotations

import argparse
import ast
import os
import re
import sys
import tomllib
from pathlib import Path

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_USAGE = 2

TAG_ENV = "GITHUB_REF_NAME"
VERSION_ATTR = "idc_simulator.__version__"
INIT_PATH = Path("idc_simulator/__init__.py")


def read_dunder_version(path: Path) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"读不了 {path}：{exc}") from exc
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "__version__" in targets and isinstance(node.value, ast.Constant):
            value = node.value.value
            if isinstance(value, str) and value.strip():
                return value.strip()
    raise ValueError(f"{path} 里没有模块级 __version__ 字符串")


def check_pyproject(path: Path) -> list[str]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return [f"{path} 不是合法 TOML：{exc}"]

    problems: list[str] = []
    project = data.get("project") or {}
    if "version" in project:
        problems.append(
            f'{path} 的 [project] 里写死了 version = "{project["version"]}"；'
            "应改为 dynamic = [\"version\"] 并由 [tool.setuptools.dynamic] 指向 "
            f"{VERSION_ATTR}"
        )
    dynamic = project.get("dynamic") or []
    if "version" not in dynamic:
        problems.append(f'{path} 的 [project].dynamic 里没有 "version"')

    attr = ((data.get("tool") or {}).get("setuptools") or {}).get("dynamic") or {}
    declared = (attr.get("version") or {}).get("attr")
    if declared != VERSION_ATTR:
        problems.append(
            f"{path} 的 [tool.setuptools.dynamic].version.attr 应为 "
            f"{VERSION_ATTR!r}，实际是 {declared!r}"
        )
    return problems


def find_hardcoded_versions(root: Path, version: str) -> list[str]:
    literal = re.compile(rf"""["']{re.escape(version)}["']""")
    findings: list[str] = []
    for path in sorted((root / "idc_simulator").rglob("*.py")):
        if path == root / INIT_PATH:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if literal.search(line) and "__version__" not in line:
                findings.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    return findings


def normalize_tag(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="校验 tag 与包版本一致")
    parser.add_argument("--tag", default=None, help=f"默认取环境变量 {TAG_ENV}")
    parser.add_argument("--no-tag", action="store_true", help="只校验内部一致性，不比对 tag")
    parser.add_argument("--root", default=".", help="仓库根目录")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    init = root / INIT_PATH
    pyproject = root / "pyproject.toml"
    if not init.is_file():
        print(f"错误：找不到 {init}", file=sys.stderr)
        return EXIT_USAGE
    if not pyproject.is_file():
        print(f"错误：找不到 {pyproject}", file=sys.stderr)
        return EXIT_USAGE

    try:
        version = read_dunder_version(init)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    problems = check_pyproject(pyproject)
    problems += [f"源码里还有硬编码版本：{line}" for line in find_hardcoded_versions(root, version)]

    status = EXIT_OK
    if problems:
        status = EXIT_MISMATCH
        print("❌ 版本声明形态有问题：", file=sys.stderr)
        for problem in problems:
            print(f"   - {problem}", file=sys.stderr)

    if not args.no_tag:
        raw_tag = args.tag if args.tag is not None else os.environ.get(TAG_ENV, "")
        tag = raw_tag.strip()
        if not tag:
            print(
                f"错误：没有 tag —— --tag 未给出、也没加 --no-tag，环境变量 {TAG_ENV} 也是空的。",
                file=sys.stderr,
            )
            return EXIT_USAGE
        if normalize_tag(tag) != version:
            print(
                f"❌ tag 「{tag}」与包版本 「{version}」不一致。\n"
                f"   这样产出的是「名叫 {tag}、内容是 {version}」的包：装上去很难察觉，\n"
                f"   却让所有 issue 与回归都无法复现。\n"
                f"   修法：改 {INIT_PATH} 的 __version__ 为 {normalize_tag(tag)}，或重新打 tag。",
                file=sys.stderr,
            )
            return EXIT_MISMATCH
        print(f"✅ 版本一致：{version}（tag {tag}）")
    else:
        print(f"✅ 版本声明形态正确：{version}（单一源 {VERSION_ATTR}）")

    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
