"""Build an allowlisted, source-only GitHub archive; never publish or connect hardware."""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
FIXTURE_MACS = {"00:00:00:00:00:00", "00:11:22:33:44:55", "02:00:00:00:00:01",
                "02:00:00:00:00:02", "02:01:04:03:06:05", "02:AA:BB:CC:DD:EE",
                "34:12:78:56:BC:9A"}
MAC = re.compile(r"(?i)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
PRIVATE_PATH = re.compile(r"(?i)(?:\b[DG]:[\\/]|[A-Z]:[\\/]+Users[\\/])")
TOKEN = re.compile(r"(?:ghp_|github_pat_|sk-(?:proj-|svcacct-)?)[A-Za-z0-9_-]{20,}")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def content_files(project: Path = PROJECT) -> dict[str, bytes]:
    entries = {}
    def include(path: Path, name: str | None = None):
        if path.is_symlink() or not path.resolve().is_relative_to(project.resolve()):
            raise ValueError("Source file escapes the project: " + path.name)
        key = name or path.relative_to(project).as_posix()
        if key in entries or key.startswith("/") or ".." in Path(key).parts:
            raise ValueError("Invalid or duplicate archive path: " + key)
        raw = path.read_bytes()
        if len(raw) > 1_000_000:
            raise ValueError("Unexpectedly large source file: " + key)
        source = raw.decode("utf-8-sig")
        if PRIVATE_PATH.search(source) or TOKEN.search(source) or PRIVATE_KEY.search(source):
            raise ValueError("Local path or credential marker in: " + key)
        unexpected = {value.upper() for value in MAC.findall(source)} - FIXTURE_MACS
        if unexpected:
            raise ValueError("Non-fixture MAC in: " + key)
        entries[key] = raw
    for name in (".gitignore", ".gitattributes", "run_server.py", "pyproject.toml",
                 "requirements.lock", "requirements-analysis.txt", "codex.config.example.toml", "LICENSE", "LICENSE_NOTICE.md"):
        include(project / name)
    for folder, suffixes in (("src", {".py"}), ("scripts", {".py", ".ps1", ".cs"}), ("tests", {".py", ".cs"})):
        for path in sorted((project / folder).rglob("*")):
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts:
                include(path)
    templates = project / "docs" / "github"
    if templates.is_dir():
        for path in sorted(templates.glob("*.md")):
            include(path, "README.md" if path.name == "README.md" else "docs/" + path.name)
    else:
        # Extracted source distributions keep the public documentation in place.
        include(project / "README.md")
        for name in ("DISTRIBUTION.md", "GETTING_STARTED.md", "VALIDATION.md", "MOTION_CONTROL.md", "TOOLS.md"):
            include(project / "docs" / name)
    include(project / "data" / "README.md")
    return entries


def main() -> None:
    version = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Unexpected release version")
    entries = content_files()
    manifest = {"format": "source-tree", "version": version,
        "vendor_binaries_included": False, "firmware_images_included": False,
        "device_records_included": False, "reference_databases_included": False,
        "license_selected": True, "license": "MIT", "configuration": "regenerate absolute paths on the destination machine",
        "files": [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()} for name, raw in sorted(entries.items())]}
    entries["RELEASE_MANIFEST.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    destination = PROJECT / "dist"
    destination.mkdir(exist_ok=True)
    archive = destination / ("boneng-drivesoft-mcp-v" + version + "-github.zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as output:
        for name, raw in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            info.create_system = 3
            output.writestr(info, raw)
    with zipfile.ZipFile(archive) as output:
        assert output.testzip() is None
        assert set(output.namelist()) == set(entries)
        for name, raw in entries.items():
            assert output.read(name) == raw, name
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".zip.sha256").write_text(digest + "  " + archive.name + "\n", encoding="ascii")
    report = {"status": "passed", "archive": archive.name, "version": version, "file_count": len(entries),
              "bytes": archive.stat().st_size, "sha256": digest, "source_only": True,
              "content_scan_passed": True, "manifest_verified": True, "uploaded_to_github": False}
    (destination / "github-package-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
