"""PyInstaller build used by release.yml to produce a single-file binary.

CHANGELOG.md rides along as data because the runtime `changelog` command reads
it from disk (REPO-SPEC: one human-maintained change source, everything else
derived). A binary that builds is not a binary that runs - smoke the artifact
itself, never only `python -m auto_cad_cli.main`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SEP = ";" if sys.platform == "win32" else ":"


def data(source: Path, dest: str) -> list[str]:
    return ["--add-data", f"{source}{SEP}{dest}"]


def main() -> int:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "auto-cad-cli",
        # The entry script imports the package rather than being a module of it,
        # so the frozen binary keeps its package context.
        "--paths",
        str(ROOT),
        *data(ROOT / "CHANGELOG.md", "."),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT),
        "--noconfirm",
        str(ROOT / "entry.py"),
    ]
    return subprocess.run(args, cwd=ROOT, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
