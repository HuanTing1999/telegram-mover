"""Copy installed dependency license texts, without copying application/user data."""
from __future__ import annotations

import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("Telethon", "cryptg", "regex", "tomli-w", "pyaes", "rsa", "pyasn1",
            "PySide6", "PySide6_Essentials", "shiboken6", "PyInstaller")


def main():
    destination = ROOT / "licenses" / "dependencies"
    destination.mkdir(parents=True, exist_ok=True)
    inventory_path = ROOT / "licenses" / "inventory.json"
    old_files = []
    if inventory_path.is_file():
        old_files = [ROOT / file["path"] for package in json.loads(inventory_path.read_text(encoding="utf-8"))
                     for file in package.get("files", [])]
    inventory = []
    for name in PACKAGES:
        package = distribution(name)
        copied = []
        for entry in package.files or ():
            value = str(entry).replace("\\", "/")
            if ".dist-info/" not in value or not any(word in value.lower() for word in ("license", "copying", "notice")):
                continue
            source = Path(package.locate_file(entry))
            if not source.is_file():
                continue
            target = destination / f"{name}-{package.version}-{source.name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            copied.append({"path": target.relative_to(ROOT).as_posix(),
                           "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
        if not copied:
            raise RuntimeError(f"No installed license text found for {name}")
        inventory.append({"package": name, "version": package.version,
                          "license": package.metadata.get("License-Expression") or package.metadata.get("License"),
                          "files": copied})
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise RuntimeError("CPython LICENSE.txt was not found in the selected interpreter")
    (destination / "Python-LICENSE.txt").write_bytes(python_license.read_bytes())
    inventory_path.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    current = {ROOT / item["path"] for package in inventory for item in package["files"]}
    # Only superseded files explicitly listed in our generated inventory.
    for path in old_files:
        if (path not in current and path.is_file() and not path.is_symlink()
                and path.resolve().is_relative_to(destination.resolve())):
            path.unlink()
    print(f"Copied license notices for {len(inventory)} installed packages and CPython.")


if __name__ == "__main__":
    main()
