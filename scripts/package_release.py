"""Create a public source archive and a credential-free Windows release ZIP."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import struct
import zipfile

from check_public_tree import ROOT, intended_files, issues_for


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_exe(path):
    from PyInstaller.archive.readers import CArchiveReader
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError("Not a Windows executable")
        stream.seek(0x3C)
        pe_offset, = struct.unpack("<I", stream.read(4))
        stream.seek(pe_offset)
        if stream.read(4) != b"PE\0\0":
            raise ValueError("Missing PE header")
        machine, = struct.unpack("<H", stream.read(2))
        stream.seek(pe_offset + 24 + 68)
        subsystem, = struct.unpack("<H", stream.read(2))
        if machine != 0x8664 or subsystem != 2:
            raise ValueError("Expected a Windows x64 GUI executable")
    names = [name.replace("\\", "/") for name in CArchiveReader(str(path)).toc]
    if "config.example.toml" not in names or not any(n.startswith("cryptg/") and n.endswith(".pyd") for n in names):
        raise ValueError("Missing config template or cryptg")
    for name in names:
        filename = Path(name).name.lower()
        if (filename in {"config.toml", "app_credentials.toml", "accounts.json"}
                or ".session" in filename or filename.endswith((".db", ".sqlite3", ".lock"))
                or name.startswith(("data/", "downloads/"))):
            raise ValueError("Executable contains private/runtime data")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "release")
    parser.add_argument("--version", default="0.1.0")
    args = parser.parse_args()
    if not args.version or any(c not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-" for c in args.version):
        raise ValueError("Invalid release version")
    verify_exe(args.exe.resolve())
    sources = intended_files()
    problems = [problem for path in sources for problem in issues_for(path)]
    if problems:
        raise ValueError("Source tree check failed:\n" + "\n".join(problems))
    for required in ("LICENSE", "README.md", "docs/USER_GUIDE.md", "licenses/qt/LGPL-3.0-only.txt", "THIRD_PARTY_NOTICES.md"):
        if ROOT / required not in sources:
            raise ValueError(f"Missing public release document: {required}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_zip = output / f"TelegramMover-{args.version}-source.zip"
    windows_zip = output / f"TelegramMover-{args.version}-Windows-x64.zip"
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sources:
            bundle.write(path, f"telegram-mover-{args.version}/" + path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(windows_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.write(args.exe, "TelegramMover.exe")
        bundle.write(ROOT / "config.example.toml", "config.toml")
        bundle.write(ROOT / "config.example.toml", "config.example.toml")
        bundle.write(ROOT / "WINDOWS-README.txt", "使用说明.txt")
        for path in sources:
            if path.suffix.lower() in {".md", ".png", ".rst"} or path.name == "LICENSE" or "licenses" in path.relative_to(ROOT).parts:
                bundle.write(path, "telegram_mover/" + path.relative_to(ROOT).as_posix())
    for path in (source_zip, windows_zip):
        with zipfile.ZipFile(path) as bundle:
            if bundle.testzip() is not None:
                raise RuntimeError("Archive integrity check failed")
    with zipfile.ZipFile(windows_zip) as bundle:
        if bundle.read("config.toml") != (ROOT / "config.example.toml").read_bytes():
            raise RuntimeError("Published config is not the credential-free template")
    checksum = output / "SHA256SUMS.txt"
    checksum.write_text("\n".join(f"{digest(path)}  {path.name}" for path in (source_zip, windows_zip)) + "\n", encoding="utf-8")
    for path in (source_zip, windows_zip, checksum):
        print(f"{path.name}: {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
