#!/usr/bin/env python3
"""scan-secrets.py — 机密信息扫描器（移植自 codewiki / ipip 同名脚本，按本仓裁剪）"""

from __future__ import annotations

import re
import socket
import sys
from pathlib import Path

SAFE_HOME_USERS = {
    "you",
    "yourname",
    "user",
    "username",
    "me",
    "example",
    "runner",
    "shared",
}

SAFE_HOSTNAMES = {
    "localhost.localdomain",
    "example.local",
    "host.local",
    "myhost.local",
    "yourhost.local",
    "sim.local",
}

CODE_LOCAL_PREFIXES = {
    "args",
    "config",
    "env",
    "flags",
    "item",
    "kwargs",
    "node",
    "obj",
    "opts",
    "options",
    "request",
    "response",
    "self",
    "settings",
}

_LOCAL_HOST_LABEL = socket.gethostname().split(".")[0].strip().lower()

RULES: list[tuple[str, re.Pattern, str]] = [
    (
        "IP_ADDR",
        re.compile(
            r"(?<![\w.])(?!0\.0\.0\.0|127\.0\.0\.1|255\.255\.255\.255|localhost)"
            r"(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(?![\d.])"
        ),
        "IP 地址（可能是内网/公网 IP）",
    ),
    (
        "PRIVATE_KEY",
        re.compile(r"-----BEGIN\s+(RSA\s+|EC\s+|OPENSSH\s+|PGP\s+)?PRIVATE KEY-----"),
        "私钥",
    ),
    (
        "GITHUB_TOKEN",
        re.compile(r"\b(gho_|ghp_|ghu_|ghs_|ghr_)[A-Za-z0-9]{36,}\b"),
        "GitHub Token",
    ),
    (
        "AWS_KEY",
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "AWS Access Key ID",
    ),
    (
        "SLACK_TOKEN",
        re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),
        "Slack Token",
    ),
    (
        "SK_PREFIX",
        re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        "sk- 前缀密钥（OpenAI/Stripe 等）",
    ),
    (
        "PASSWORD_ASSIGN",
        re.compile(
            r"(?i)['\"]?\w*(passwd|password|pwd|secret|api[_-]?key|apikey)['\"]?\s*[=:]\s*"
            r"(?:['\"]([^'\"]{4,})['\"]|(\S{4,}))"
        ),
        "明文密码赋值",
    ),
    (
        "CONN_STRING",
        re.compile(r"(?i)(mysql|redis|postgresql|mongodb)://\w+:[^@\s]+@"),
        "连接字符串含密码",
    ),
    (
        "HOME_PATH",
        re.compile(r"(?<![\w.:/-])/(?:Users|home)/[A-Za-z0-9._-]+(?=/|[\s\"'<>,;)\]}]|$)"),
        "本机家目录绝对路径（含用户名）",
    ),
    (
        "MDNS_HOSTNAME",
        re.compile(r"(?i)(?<![\w.-])[a-z0-9][a-z0-9-]{2,}\.local(?![\w-])"),
        "局域网/mDNS 主机名",
    ),
]

if len(_LOCAL_HOST_LABEL) >= 4:
    RULES.append(
        (
            "LOCAL_HOSTNAME",
            re.compile(rf"(?i)(?<![\w-]){re.escape(_LOCAL_HOST_LABEL)}(?![\w-])"),
            "本机主机名",
        )
    )

_VALUE_TAIL = re.compile(r"['\"]([^'\"]*)['\"]\s*$")
_SOURCE_TAIL = re.compile(r"""(?:\\[nrt])*["')\]},;]*$""")
_PLACEHOLDER_VALUE = re.compile(
    r"^(?:<.*|\$\{.*|\$\(.*|\{.*|os\..*|getenv.*|environ.*|%.*|None|null|\*+|x+|\.+)$",
    re.IGNORECASE,
)
_INTERP_SEGMENT = (
    r"(?:\$\{[^{}]*\}"
    r"|\$[A-Za-z_][A-Za-z0-9_]*"
    r"|\{[A-Za-z_][A-Za-z0-9_.\[\]'\"]*\})"
)
_INTERP_ONLY = re.compile(rf"^{_INTERP_SEGMENT}+$")
_BRACE_SEGMENT = re.compile(r"(?<!\$)\{[A-Za-z_]")
_FSTRING_PREFIX = re.compile(r"(?<![\w])[rb]{0,2}f[rb]{0,2}['\"]", re.IGNORECASE)
_NAMESPACED_KEY_ONLY = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)+(?:passwd|password|pwd|secret|api[_-]?key)$",
    re.IGNORECASE,
)

SAFE_IPS = {
    "0.0.0.0",
    "127.0.0.1",
    "255.255.255.255",
    "1.1.1.1",
    "8.8.8.8",
    "8.8.4.4",
    "1.2.3.4",
    "239.0.0.1",
    "255.255.255.0",
    "255.255.0.0",
    "255.0.0.0",
    "255.255.252.0",
    "255.255.248.0",
    "255.255.240.0",
}

SAFE_NETWORKS: dict[str, str] = {
    "192.0.2.": "RFC 5737 文档网段",
    "198.51.100.": "RFC 5737 文档网段",
    "203.0.113.": "RFC 5737 文档网段",
    "192.0.3.": "本仓历史回归用例的示例网段（非真实资产）",
}

OID_ROOT_PREFIXES = {"1.3.6.1"}
_OID_CHAIN = re.compile(r"(?<![\w.])\d+(?:\.\d+){4,}(?![\d.])")

SAFE_PASSWORDS = {
    "sim-password",
    "live-password",
    "wrong-password",
    "unit-test-password",
}


def is_private_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    o = [int(p) for p in parts]
    if o[0] in (255, 10, 127):
        return True
    if o[0] == 172 and 16 <= o[1] <= 31:
        return True
    if o[0] == 192 and o[1] == 168:
        return True
    if o[0] == 169 and o[1] == 254:
        return True
    if o[0] == 100 and 64 <= o[1] <= 127:
        return True
    if o[0] == 192 and o[1] == 0 and o[2] == 2:
        return True
    if o[0] == 198 and o[1] == 51 and o[2] == 100:
        return True
    return bool(o[0] == 203 and o[1] == 0 and o[2] == 113)


SAFE_PATH_PREFIXES = (".example", "README", "examples/", "docs/")

FIXTURE_MARKER = "scan-secrets: fixtures-allowed"
_MARKER_HEAD_LINES = 40
_FIXTURE_DIRECTIVE = re.compile(
    r"^\s*(?:#|//|/\*|\*|<!--)\s*scan-secrets:\s*fixtures-allowed\b", re.IGNORECASE
)


def _has_fixture_marker(text: str) -> bool:
    return any(_FIXTURE_DIRECTIVE.match(line) for line in text.splitlines()[:_MARKER_HEAD_LINES])


def _in_tests_tree(path: Path) -> bool:
    return "tests" in Path(path).parts


def _is_safe_ip(matched: str) -> bool:
    if matched in SAFE_IPS or is_private_ip(matched):
        return True
    return any(matched.startswith(prefix) for prefix in SAFE_NETWORKS)


PUBLIC_MESSAGE_RULES: list[tuple[str, re.Pattern, str]] = [
    (
        "INTERNAL_DOC",
        re.compile(r"(?i)docs/(review|design|acceptance|archive|implementation\.md)"),
        "内部文档路径",
    ),
    (
        "INTERNAL_REVIEW",
        re.compile(r"(?i)(评审|复核|review[-_ ]?v?\d|r2-fixes|v4-v5-fixes)"),
        "内部评审流程/编号",
    ),
    (
        "INTERNAL_ISSUE",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])(?:I-\d|F\d|N\d|OPT-\d|WP-[\d.]+|IMP-\d|R\d|B-\d|E\d)"
            r"(?![A-Za-z0-9])"
        ),
        "内部缺陷编号",
    ),
    (
        "INTERNAL_TOOL",
        re.compile(
            r"(?i)\b(?:[a-z_]+_report\.py|sanitize_[a-z_]+\.py|sync_to_[a-z_]+\.py|"
            r"backup-[a-z-]+|internal_[a-z_]+\.py)\b"
        ),
        "内部工具/脚本名",
    ),
    (
        "INTERNAL_INFRA",
        re.compile(r"(?i)(gitea|gitlab|jenkins|内网|私有部署|/root/)"),
        "内部部署语境",
    ),
    (
        "INTERNAL_WORD",
        re.compile(r"(内部|内测|私有仓|开发仓|工作日志|验收台账|归档|整改)"),
        "内部过程用语",
    ),
    (
        "EMAIL_ADDR",
        re.compile(
            r"(?<![\w.+-])[\w.+-]+@"
            r"(?!(?:example\.(?:com|org|net)|localhost|users\.noreply\.github\.com))"
            r"[\w-]+(?:\.[\w-]+)+"
        ),
        "邮箱地址（可能暴露个人/企业身份）",
    ),
]


def _message_exempt(rule_id: str, m: re.Match, line: str) -> bool:
    matched = m.group(0)

    if rule_id == "IP_ADDR":
        if _is_safe_ip(matched):
            return True
        if matched in OID_ROOT_PREFIXES and _OID_CHAIN.search(line):
            return True
        parts = matched.split(".")
        return (
            len(parts) == 4
            and all(p.isdigit() and int(p) < 100 for p in parts)
            and ("version" in line.lower() or "==" in line)
        )

    if rule_id == "PASSWORD_ASSIGN":
        lowered = matched.lower()
        if any(p in lowered for p in ("your", "xxx", "change", "example", "replace", "<", "placeholder")):
            return True
        value = (m.group(2) or m.group(3) or "").strip().rstrip(",;)]}")
        if not value or value in SAFE_PASSWORDS:
            return True
        return bool(
            _PLACEHOLDER_VALUE.match(value)
            or _INTERP_ONLY.match(value)
            or _NAMESPACED_KEY_ONLY.match(value)
            or re.fullmatch(r"[A-Z][A-Z0-9_]*", value)
        )

    if rule_id == "HOME_PATH":
        return matched.rsplit("/", 1)[-1].lower() in SAFE_HOME_USERS

    if rule_id in {"MDNS_HOSTNAME", "LOCAL_HOSTNAME"}:
        return matched.lower() in SAFE_HOSTNAMES

    if rule_id == "CONN_STRING":
        return any(p in matched.lower() for p in ("password", "secret", "your", "xxx", "<"))

    return False


def scan_message(text: str) -> list[tuple[int, str, str, str]]:
    findings: list[tuple[int, str, str, str]] = []
    for lineno, line in enumerate(text.splitlines() or [""], 1):
        for rule_id, pattern, desc in PUBLIC_MESSAGE_RULES:
            for m in pattern.finditer(line):
                findings.append((lineno, rule_id, m.group(0), desc))
        for rule_id, pattern, desc in RULES:
            for m in pattern.finditer(line):
                if not _message_exempt(rule_id, m, line):
                    findings.append((lineno, rule_id, m.group(0), desc))
    return findings


def _message_mode(argv: list[str]) -> int | None:
    if not argv or argv[0] not in ("--stdin", "--message"):
        return None
    if argv[0] == "--stdin":
        if len(argv) != 1:
            print("用法: scan-secrets.py --stdin", file=sys.stderr)
            return 2
        text = sys.stdin.read()
    else:
        if len(argv) < 2:
            print("用法: scan-secrets.py --message <text>", file=sys.stderr)
            return 2
        text = " ".join(argv[1:])

    findings = scan_message(text)
    if not findings:
        print("✅ 未发现敏感内容（公开提交/标签信息）")
        return 0
    print(f"❌ 公开提交/标签信息里有 {len(findings)} 处敏感内容：\n", file=sys.stderr)
    for lineno, rule_id, matched, desc in findings:
        shown = matched[:80] + "..." if len(matched) > 80 else matched
        print(f"  [{rule_id}] 第 {lineno} 行: {desc}", file=sys.stderr)
        print(f"    匹配: {shown}\n", file=sys.stderr)
    print("⚠️  公开仓历史不可改写：请改用中性发布说明，变更细节写进 CHANGELOG.md。", file=sys.stderr)
    return 1


def scan_file(path: Path, text: str | None = None) -> list[tuple[int, str, str, str]]:
    findings: list[tuple[int, str, str, str]] = []

    if path.name == "scan-secrets.py" and path.parent.name == "scripts":
        return findings

    if text is None:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return findings

    if "\x00" in text[:8192]:
        return findings

    if _has_fixture_marker(text) and _in_tests_tree(path):
        return findings

    full = str(path).replace("\\", "/")
    is_safe_path = any(path.name.startswith(p) or full.startswith(p) or f"/{p}" in full for p in SAFE_PATH_PREFIXES)

    for lineno, line in enumerate(text.splitlines(), 1):
        for rule_id, pattern, desc in RULES:
            for m in pattern.finditer(line):
                matched = m.group(0)

                if rule_id == "IP_ADDR":
                    if _is_safe_ip(matched):
                        continue
                    if is_safe_path:
                        continue
                    if matched in OID_ROOT_PREFIXES and _OID_CHAIN.search(line):
                        continue
                    if any(tok in line for tok in ("_O_", ".snmprec", "ObjectIdentity", "ObjectType", "OidProvider")):
                        continue
                    parts = matched.split(".")
                    if (
                        len(parts) == 4
                        and all(p.isdigit() and int(p) < 100 for p in parts)
                        and ("version" in line.lower() or "==" in line)
                    ):
                        continue

                if rule_id == "PASSWORD_ASSIGN":
                    lowered = m.group(0).lower()
                    if any(
                        p in lowered for p in ("your", "xxx", "change", "example", "replace", "<", "placeholder")
                    ):
                        continue
                    tail = _VALUE_TAIL.search(m.group(0))
                    value = tail.group(1).strip() if tail else ""
                    bare = (m.group(3) or "").strip().rstrip(",")
                    if bare and m.group(2) is None:
                        bare = _SOURCE_TAIL.sub("", bare)
                        if not bare:
                            continue
                        if _PLACEHOLDER_VALUE.match(bare) or re.fullmatch(r"[A-Z][A-Z0-9_]*", bare):
                            continue
                        if bare == m.group(1):
                            continue
                        if any(ch in bare for ch in "()[]{}"):
                            continue
                        if re.fullmatch(r"[a-z_][a-z0-9_]*", bare):
                            continue
                        value, tail = bare, None
                    if value in SAFE_PASSWORDS:
                        continue
                    if (
                        tail
                        and _INTERP_ONLY.match(value)
                        and (not _BRACE_SEGMENT.search(value) or _FSTRING_PREFIX.search(line))
                    ):
                        continue
                    if tail and value.startswith("$("):
                        continue
                    if tail and _NAMESPACED_KEY_ONLY.match(value):
                        continue
                    if tail and re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
                        continue

                if rule_id == "HOME_PATH":
                    user = matched.rsplit("/", 1)[-1].lower()
                    if user in SAFE_HOME_USERS:
                        continue

                if rule_id in {"MDNS_HOSTNAME", "LOCAL_HOSTNAME"}:
                    if matched.lower() in SAFE_HOSTNAMES:
                        continue
                    if rule_id == "MDNS_HOSTNAME" and matched.rsplit(".", 1)[0].lower() in CODE_LOCAL_PREFIXES:
                        continue

                if rule_id == "CONN_STRING" and any(
                    p in m.group(0).lower() for p in ("password", "secret", "your", "xxx", "<")
                ):
                    continue

                findings.append((lineno, rule_id, matched, desc))

    return findings


def main() -> int:
    message_result = _message_mode(sys.argv[1:])
    if message_result is not None:
        return message_result

    if len(sys.argv) < 2:
        print("用法: scan-secrets.py <file1> [file2 ...] | --stdin | --message <text>", file=sys.stderr)
        return 2

    all_findings: list[tuple[str, int, str, str, str]] = []
    skipped: list[str] = []
    misused: list[str] = []
    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists() or path.is_dir():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        if _has_fixture_marker(text):
            if _in_tests_tree(path):
                skipped.append(arg)
                continue
            misused.append(arg)
        for lineno, rule_id, matched, desc in scan_file(path, text):
            all_findings.append((arg, lineno, rule_id, matched, desc))

    if skipped:
        print(f"ℹ️  {len(skipped)} 个测试夹具文件按标记跳过（含故意写出的假密钥）：", file=sys.stderr)
        for s in skipped:
            print(f"     - {s}", file=sys.stderr)

    if misused:
        print("❌ 以下文件在 tests/ 之外使用了 fixture 豁免标记 —— 不生效，已按普通文件扫描：", file=sys.stderr)
        for s in misused:
            print(f"     - {s}", file=sys.stderr)

    if not all_findings:
        print("✅ 未发现机密")
        return 1 if misused else 0

    print(f"❌ 发现 {len(all_findings)} 处疑似机密：\n", file=sys.stderr)
    for path, lineno, rule_id, matched, desc in all_findings:
        shown = matched[:80] + "..." if len(matched) > 80 else matched
        print(f"  [{rule_id}] {path}:{lineno}", file=sys.stderr)
        print(f"    {desc}", file=sys.stderr)
        print(f"    匹配: {shown}\n", file=sys.stderr)

    print("⚠️  请确认这些内容是否为机密。如是，请移除后再同步到公开仓。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
