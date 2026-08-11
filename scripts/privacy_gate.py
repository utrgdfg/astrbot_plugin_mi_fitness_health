"""Fail closed when tracked files or release archives contain private material."""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import subprocess
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

APPROVED_LOGO_SHA256 = (
    "d608084f30124e9d4083786ee61a61157c18c10422037cd4176eaf1c2869393f"
)
MAX_ARCHIVE_ENTRY_BYTES = 10 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 50 * 1024 * 1024

_PLACEHOLDER_MARKERS = (
    b"synthetic",
    b"example",
    b"dummy",
    b"fake",
    b"placeholder",
    b"redacted",
    b"***",
    "示例".encode(),
)
_FORBIDDEN_SUFFIXES = (
    ".7z",
    ".bak",
    ".backup",
    ".bmp",
    ".db",
    ".db-journal",
    ".db-shm",
    ".db-wal",
    ".env",
    ".gif",
    ".jpeg",
    ".jpg",
    ".log",
    ".rar",
    ".sqlite",
    ".sqlite-journal",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-journal",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".webp",
    ".zip",
)
_PRIVATE_KEY_PATTERN = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_KNOWN_TOKEN_PATTERN = re.compile(
    rb"(?i)\b(?:"
    rb"gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"sk-(?:ant-)?[A-Za-z0-9_-]{16,}|"
    rb"AIza[A-Za-z0-9_-]{24,}|"
    rb"AKIA[A-Z0-9]{16}|"
    rb"xox[baprs]-[A-Za-z0-9-]{16,}|"
    rb"(?:sk|rk)_live_[A-Za-z0-9]{16,}"
    rb")\b"
)
_SENSITIVE_NAME_PATTERN = re.compile(
    r"(?i)^(?:"
    r"[a-z0-9_-]*token|[a-z0-9_-]*(?:password|passwd|pwd)|"
    r"[a-z0-9_-]*cookie[a-z0-9_-]*|[a-z0-9_-]*secret[a-z0-9_-]*|"
    r"(?:c?user|session|owner|provider|persona)[a-z0-9_-]*id|"
    r"uid|platform_id|bot_id|ssecurity|authorization|api[_-]?key|"
    r"x-api-key|private[_-]?key"
    r")$"
)
_SENSITIVE_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    rb"(?i)\b(?:"
    rb"[a-z0-9_-]*token|[a-z0-9_-]*(?:password|passwd|pwd)|"
    rb"[a-z0-9_-]*cookie[a-z0-9_-]*|[a-z0-9_-]*secret[a-z0-9_-]*|"
    rb"(?:c?user|session|owner|provider|persona)[a-z0-9_-]*id|"
    rb"uid|platform_id|bot_id|ssecurity|authorization|api[_-]?key|"
    rb"x-api-key|private[_-]?key"
    rb")[\"']?\s*(?:=|:)\s*(?:"
    rb'"(?P<double>[^"\r\n]*)"|\'(?P<single>[^\'\r\n]*)\'|'
    rb"(?P<unquoted>[^\s;,&\r\n]+))"
)
_BEARER_PATTERN = re.compile(
    rb"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+(?P<value>[^\s\r\n]+)"
)
_URL_USERINFO_PATTERN = re.compile(rb"(?i)https?://[^\s/:]+:[^\s/@]+@")
_WINDOWS_PATH_PATTERN = re.compile(
    rb"(?ix)(?<![\w])(?:"
    rb"[A-Z]:[\\/](?![\]\\(){}*+?.^$|])"
    rb"(?:[^\\/\r\n:*?\"<>|,;\)\]]+[\\/])*"
    rb"[^\\/\s\r\n:*?\"<>|,;\)\]]+|"
    rb"\\\\[A-Z0-9._-]+[\\/][A-Z0-9 $._-]+[\\/]"
    rb"(?:[^\\/\r\n:*?\"<>|,;\)\]]+[\\/])*"
    rb"[^\\/\s\r\n:*?\"<>|,;\)\]]+"
    rb")"
)
_PERSONAL_POSIX_PATH_PATTERN = re.compile(
    rb"(?ix)(?<![\w:/])(?:"
    rb"/(?:home|Users)/[^/\s]+/(?:[^/\r\n:;,]+/)*[^/\s\r\n:;,\)\]]+|"
    rb"/root/(?:[^/\r\n:;,]+/)*[^/\s\r\n:;,\)\]]+|"
    rb"/mnt/[a-z]/Users/[^/\s]+/(?:[^/\r\n:;,]+/)*[^/\s\r\n:;,\)\]]+"
    rb")"
)
_PLATFORM_LOG_PATTERN = re.compile(
    rb"(?m)^\[(?:\d{2}:\d{2}:\d{2}(?:\.\d+)?|"
    rb"\d{4}-\d{2}-\d{2}[^\]]*)\].*"
    rb"(?:event_bus|star_manager|plugin_service)"
)


@dataclass(frozen=True, slots=True)
class Violation:
    """One safe-to-print privacy gate failure without the matched content."""

    scope: str
    path: str
    rule: str

    def render(self) -> str:
        return f"{self.scope}:{self.path} [{self.rule}]"


def _is_placeholder(value: bytes, path: str) -> bool:
    normalized = value.strip(b"\"'").lower()
    if not normalized or normalized in {b"none", b"null", b"true", b"false"}:
        return True
    if any(marker in normalized for marker in _PLACEHOLDER_MARKERS):
        return True
    if any(marker in normalized for marker in (b"(?", b"[^", b"\\s")):
        return True
    if path.startswith("tests/") and (
        normalized in {b"user", b"token"}
        or normalized.strip(b"[]") == b"1234567890"
        or normalized.startswith(
            (
                b"test-",
                b"original-",
                b"replacement-",
                b"main-",
                b"turn-",
                b"different-",
                b"configured-",
                b"unknown",
            )
        )
    ):
        return True
    return False


def _python_sensitive_literals(text: str, path: str, scope: str) -> list[Violation]:
    """Find literal secrets in Python without mistaking variables for values."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError:
        return [Violation(scope, path, "invalid-python-source")]

    candidates: list[tuple[str, object]] = []

    def target_name(target: ast.expr) -> str | None:
        if isinstance(target, ast.Name):
            return target.id
        if isinstance(target, ast.Attribute):
            return target.attr
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = target_name(target)
                if name:
                    candidates.append((name, node.value))
        elif isinstance(node, ast.AnnAssign):
            name = target_name(node.target)
            if name and node.value is not None:
                candidates.append((name, node.value))
        elif isinstance(node, ast.keyword) and node.arg:
            candidates.append((node.arg, node.value))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    candidates.append((key.value, value))

    for name, value_node in candidates:
        if not _SENSITIVE_NAME_PATTERN.fullmatch(name):
            continue
        if name.isupper() or name.startswith("_RUNNER_"):
            continue
        if not isinstance(value_node, ast.Constant) or not isinstance(
            value_node.value, (str, bytes)
        ):
            continue
        value = (
            value_node.value.encode("utf-8")
            if isinstance(value_node.value, str)
            else value_node.value
        )
        if len(value) >= 8 and not _is_placeholder(value, path):
            return [Violation(scope, path, "sensitive-value-literal")]
    return []


def _forbidden_path_reason(relative_path: str) -> str | None:
    path = PurePosixPath(relative_path.replace("\\", "/"))
    lowered_parts = tuple(part.lower() for part in path.parts)
    lowered_name = path.name.lower()
    if any(
        part in {"logs", "log", "screenshots", "screenshot"} for part in lowered_parts
    ):
        return "private-artifact-path"
    if lowered_name == ".env" or lowered_name.startswith(".env."):
        if lowered_name not in {".env.example", ".env.template"}:
            return "environment-file"
    if lowered_name.startswith(("screenshot", "截图")):
        return "screenshot-file"
    if lowered_name != "logo.png" and lowered_name.endswith(".png"):
        return "unapproved-image"
    if lowered_name.endswith(_FORBIDDEN_SUFFIXES):
        return "private-artifact-file"
    return None


def inspect_payload(relative_path: str, payload: bytes, scope: str) -> list[Violation]:
    """Inspect one repository/archive entry without ever returning matched values."""
    path = relative_path.replace("\\", "/")
    violations: list[Violation] = []
    path_reason = _forbidden_path_reason(path)
    if path_reason:
        violations.append(Violation(scope, path, path_reason))
        return violations
    if path == "logo.png":
        if hashlib.sha256(payload).hexdigest() != APPROVED_LOGO_SHA256:
            violations.append(Violation(scope, path, "unapproved-logo-content"))
        return violations
    if b"\x00" in payload:
        return [Violation(scope, path, "unexpected-binary-content")]
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return [Violation(scope, path, "non-utf8-content")]

    if _PRIVATE_KEY_PATTERN.search(payload):
        violations.append(Violation(scope, path, "private-key-literal"))
    if _URL_USERINFO_PATTERN.search(payload):
        violations.append(Violation(scope, path, "credential-in-url"))
    for match in _KNOWN_TOKEN_PATTERN.finditer(payload):
        if not _is_placeholder(match.group(0), path):
            violations.append(Violation(scope, path, "known-token-literal"))
            break
    for match in _BEARER_PATTERN.finditer(payload):
        if not _is_placeholder(match.group("value"), path):
            violations.append(Violation(scope, path, "authorization-literal"))
            break
    if path.lower().endswith(".py"):
        violations.extend(_python_sensitive_literals(text, path, scope))
    else:
        for match in _SENSITIVE_QUOTED_ASSIGNMENT_PATTERN.finditer(payload):
            value = (
                match.group("double")
                or match.group("single")
                or match.group("unquoted")
                or b""
            )
            if len(value) >= 8 and not _is_placeholder(value, path):
                violations.append(Violation(scope, path, "sensitive-value-literal"))
                break
    for pattern, rule in (
        (_WINDOWS_PATH_PATTERN, "local-windows-path"),
        (_PERSONAL_POSIX_PATH_PATTERN, "local-posix-path"),
    ):
        for match in pattern.finditer(payload):
            if not _is_placeholder(match.group(0), path):
                violations.append(Violation(scope, path, rule))
                break
    if _PLATFORM_LOG_PATTERN.search(payload):
        violations.append(Violation(scope, path, "platform-log-content"))
    return violations


def scan_tracked_tree(repository_root: Path) -> list[Violation]:
    """Scan both staged blobs and tracked worktree files, failing closed on Git errors."""
    repository_root = repository_root.resolve()
    command = ["git", "-C", str(repository_root), "ls-files", "--stage", "-z"]
    try:
        listing = subprocess.check_output(command)
    except (OSError, subprocess.CalledProcessError) as error:
        raise RuntimeError("could not enumerate tracked files") from error
    violations: list[Violation] = []
    seen_paths: set[str] = set()
    for record in listing.split(b"\0"):
        if not record:
            continue
        metadata, encoded_path = record.split(b"\t", 1)
        mode, blob_sha, stage = metadata.decode("ascii").split()
        if stage != "0":
            violations.append(Violation("index", encoded_path.decode(), "merge-stage"))
            continue
        path = encoded_path.decode("utf-8", "surrogateescape").replace("\\", "/")
        seen_paths.add(path)
        if mode == "120000":
            violations.append(Violation("index", path, "symlink"))
            continue
        try:
            staged = subprocess.check_output(
                ["git", "-C", str(repository_root), "cat-file", "blob", blob_sha]
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(f"could not read staged blob for {path}") from error
        violations.extend(inspect_payload(path, staged, "index"))
    for path in sorted(seen_paths):
        worktree_path = repository_root / PurePosixPath(path)
        if not worktree_path.is_file():
            violations.append(Violation("worktree", path, "tracked-file-missing"))
            continue
        if worktree_path.is_symlink():
            violations.append(Violation("worktree", path, "symlink"))
            continue
        violations.extend(inspect_payload(path, worktree_path.read_bytes(), "worktree"))
    return violations


def scan_archive(archive_path: Path) -> list[Violation]:
    """Scan one ZIP in memory without extracting private material to disk."""
    violations: list[Violation] = []
    with zipfile.ZipFile(archive_path) as archive:
        names = [entry.filename for entry in archive.infolist()]
        for name, count in Counter(names).items():
            if count > 1:
                violations.append(Violation("archive", name, "duplicate-entry"))
        total_size = 0
        for entry in archive.infolist():
            if entry.is_dir():
                continue
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts:
                violations.append(Violation("archive", entry.filename, "unsafe-path"))
                continue
            if entry.flag_bits & 0x1:
                violations.append(
                    Violation("archive", entry.filename, "encrypted-entry")
                )
                continue
            mode = (entry.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                violations.append(Violation("archive", entry.filename, "symlink"))
                continue
            total_size += entry.file_size
            if entry.file_size > MAX_ARCHIVE_ENTRY_BYTES:
                violations.append(
                    Violation("archive", entry.filename, "entry-too-large")
                )
                continue
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                violations.append(
                    Violation("archive", entry.filename, "archive-too-large")
                )
                break
            violations.extend(
                inspect_payload(entry.filename, archive.read(entry), "archive")
            )
    return violations


def assert_clean(violations: list[Violation]) -> None:
    """Raise with only safe path/rule diagnostics when any violation exists."""
    if violations:
        rendered = "\n".join(violation.render() for violation in violations)
        raise ValueError("privacy gate rejected content:\n" + rendered)


def assert_archive_clean(archive_path: Path) -> None:
    assert_clean(scan_archive(archive_path))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repository", type=Path)
    group.add_argument("--archive", type=Path, nargs="+")
    arguments = parser.parse_args()
    if arguments.repository is not None:
        assert_clean(scan_tracked_tree(arguments.repository))
        print("Tracked repository privacy gate: clean")
    else:
        for archive_path in arguments.archive:
            assert_archive_clean(archive_path)
            print(f"Release privacy gate: clean ({archive_path.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
