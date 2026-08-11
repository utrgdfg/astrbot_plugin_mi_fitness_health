"""Build a deterministic AstrBot file-install archive from runtime sources."""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path, PurePosixPath

PLUGIN_NAME = "astrbot_plugin_mi_fitness_health"
ROOT_FILES = (
    "_conf_schema.json",
    "CHANGELOG.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "logo.png",
    "main.py",
    "metadata.yaml",
    "requirements.txt",
)
RUNTIME_PACKAGES = (
    "adapters",
    "compat",
    "features",
    "models",
    "services",
    "storage",
    "utils",
)
TEXT_SUFFIXES = {".json", ".md", ".py", ".txt", ".yaml", ".yml"}
VERSION_PATTERN = re.compile(r"^version:\s*(v\d+\.\d+\.\d+)\s*$", re.MULTILINE)
FORBIDDEN_SUFFIXES = {
    "-journal",
    "-shm",
    "-wal",
    ".db",
    ".journal",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".wal",
}


def metadata_version(repository_root: Path) -> str:
    """Return the semantic version declared by metadata.yaml."""
    text = (repository_root / "metadata.yaml").read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(text)
    if match is None:
        raise ValueError("metadata.yaml 缺少有效的 vX.Y.Z 版本号")
    return match.group(1)


def release_files(repository_root: Path) -> list[Path]:
    """Return the explicit runtime manifest in stable archive order."""
    files = [repository_root / relative for relative in ROOT_FILES]
    for package in RUNTIME_PACKAGES:
        package_root = repository_root / package
        if not (package_root / "__init__.py").is_file():
            raise FileNotFoundError(f"运行包缺少 {package}/__init__.py")
        files.extend(sorted(package_root.rglob("*.py")))
    missing = [
        str(path.relative_to(repository_root)) for path in files if not path.is_file()
    ]
    if missing:
        raise FileNotFoundError("发布文件缺失：" + "、".join(missing))
    return sorted(
        set(files), key=lambda path: path.relative_to(repository_root).as_posix()
    )


def _archive_payload(path: Path) -> bytes:
    payload = path.read_bytes()
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in {
        "LICENSE",
        "NOTICE",
        "requirements.txt",
    }:
        payload = payload.replace(b"\r\n", b"\n")
    return payload


def validate_archive(archive_path: Path) -> None:
    """Reject nested roots, missing entrypoints, and private build artefacts."""
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
    required = {"main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt"}
    missing = required.difference(names)
    if missing:
        raise ValueError("安装包缺少：" + "、".join(sorted(missing)))
    for name in names:
        path = PurePosixPath(name)
        lowered = name.lower()
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"安装包路径不安全：{name}")
        if "__pycache__" in path.parts or lowered.endswith(tuple(FORBIDDEN_SUFFIXES)):
            raise ValueError(f"安装包含有禁止文件：{name}")


def build_release(repository_root: Path, output_directory: Path) -> Path:
    """Create and validate one direct-root AstrBot installation ZIP."""
    repository_root = repository_root.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    version = metadata_version(repository_root)
    output_path = output_directory / f"{PLUGIN_NAME}-{version}.zip"
    with zipfile.ZipFile(
        output_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for source in release_files(repository_root):
            relative = source.relative_to(repository_root).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, _archive_payload(source), compresslevel=9)
    validate_archive(output_path)
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist"),
        help="directory for the generated installation ZIP",
    )
    arguments = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    archive = build_release(repository_root, arguments.output_dir)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
