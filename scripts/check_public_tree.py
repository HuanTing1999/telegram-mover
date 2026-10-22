"""Check only the intended public tree; never print suspected secret values."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    ".gitignore", ".gitattributes", "LICENSE", "README.md", "CONTRIBUTING.md",
    "SECURITY.md", "CHANGELOG.md", "THIRD_PARTY_NOTICES.md", "ACCOUNTS.md",
    "AD_FILTERS.md", "LIMITS.md", "VALIDATION.md", "WINDOWS-README.txt",
    "config.example.toml", "app_credentials.example.toml", "requirements.txt",
    "requirements-build.txt", "build_windows.ps1", "main.py", "gui_main.py", "launcher.py",
}
PUBLIC_DIRS = {"tgmover", "tests", "docs", "scripts", "licenses", ".github"}
TEXT_SUFFIXES = {".py", ".md", ".txt", ".toml", ".ps1", ".yml", ".yaml", ".json", ".rst"}
SECRET_PATTERNS = {
    "GitHub token": re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "Telegram bot token": re.compile(r"(?<!\w)\d{6,14}:[A-Za-z0-9_-]{35}(?!\w)"),
    "literal API hash": re.compile(r'''(?i)["']?api_hash["']?\s*[:=]\s*["']([a-f0-9]{32})["']'''),
}


def intended_files() -> list[Path]:
    paths = [ROOT / name for name in ROOT_FILES if (ROOT / name).is_file()]
    paths += list(ROOT.glob("*-PREVIEW.png"))
    for directory in PUBLIC_DIRS:
        base = ROOT / directory
        if base.is_dir():
            paths.extend(path for path in base.rglob("*") if path.is_file()
                         and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
                         and not any(part.startswith("tmp") for part in path.relative_to(base).parts))
    return sorted(set(paths))


def public_files(*, use_git=True) -> list[Path]:
    if use_git and (ROOT / ".git").exists():
        result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
        paths = [ROOT / name.decode("utf-8") for name in result.stdout.split(b"\0") if name]
        if paths:
            return paths
    return intended_files()


def issues_for(path: Path) -> list[str]:
    relative = path.relative_to(ROOT)
    name = path.name.lower()
    problems = []
    forbidden = {".git", ".build", ".venv", "data", "downloads", "__pycache__"}
    if (any(part.lower() in forbidden for part in relative.parts)
            or name in {"config.toml", "app_credentials.toml", "accounts.json"}
            or name == ".env" or (name.startswith(".env.") and name != ".env.example")
            or re.search(r"\.(?:session|sqlite3?|db)(?:[-.]|$)", name)
            or path.suffix.lower() in {".exe", ".dll", ".pyd", ".zip", ".log", ".lock", ".part", ".pyc"}):
        problems.append(f"{relative.as_posix()}: runtime/private or build file")
        return problems
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT.resolve()):
        return [f"{relative.as_posix()}: missing, linked, or outside repository"]
    if path.suffix.lower() in TEXT_SUFFIXES or name in {"license", ".gitignore", ".gitattributes"}:
        content = path.read_text(encoding="utf-8-sig")
        for number, line in enumerate(content.splitlines(), 1):
            for label, pattern in SECRET_PATTERNS.items():
                match = pattern.search(line)
                # Repeated a/b/etc hashes are intentional offline fixtures.
                if match and not (label == "literal API hash" and len(set(match.group(1).lower())) == 1):
                    problems.append(f"{relative.as_posix()}:{number}: possible {label} (value hidden)")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intended", action="store_true", help="check the explicit source allowlist before staging")
    args = parser.parse_args()
    paths = public_files(use_git=not args.intended)
    problems = [problem for path in paths for problem in issues_for(path)]
    for required in ("LICENSE", "README.md", "docs/USER_GUIDE.md", "THIRD_PARTY_NOTICES.md"):
        if ROOT / required not in paths:
            problems.append(f"{required}: required public document missing")
    if problems:
        print("Public-tree check failed; inspect locally before publishing:")
        print("\n".join(problems))
        return 1
    print(f"Public-tree check passed: {len(paths)} files; no runtime data or matching credential literals.")
    print("Review screenshots and staged changes separately; this is a bounded check, not a guarantee.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
