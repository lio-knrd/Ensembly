"""Locate a LaTeX distribution and expose it to the render subprocess.

MiKTeX/TeX installers add themselves to the user PATH, but a long-running app
process started *before* the install won't see it. To make MathTex/Tex work
regardless of process start order, we probe known install locations and prepend
the bin dir to the subprocess PATH. ``latex_available`` drives whether the script
prompt tells the AI it may use MathTex/Tex.
"""
from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

__all__ = ["latex_dir", "latex_available", "subprocess_env"]


def _candidates() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    appdata = os.environ.get("APPDATA", "")
    paths = [
        Path(local) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64",
        Path(program_files) / "MiKTeX" / "miktex" / "bin" / "x64",
        Path(r"C:\Program Files\MiKTeX\miktex\bin\x64"),
        Path(appdata) / "TinyTeX" / "bin" / "windows",
    ]
    return [p for p in paths if str(p)]


@lru_cache(maxsize=1)
def latex_dir() -> str | None:
    """Directory containing ``latex.exe`` (via PATH, then known locations)."""
    found = shutil.which("latex")
    if found:
        return str(Path(found).parent)
    for candidate in _candidates():
        if (candidate / "latex.exe").exists() or (candidate / "latex").exists():
            return str(candidate)
    return None


def latex_available() -> bool:
    return latex_dir() is not None


def subprocess_env() -> dict:
    """A copy of the environment with the LaTeX bin dir on PATH if found."""
    env = dict(os.environ)
    directory = latex_dir()
    if directory and directory.lower() not in env.get("PATH", "").lower():
        env["PATH"] = directory + os.pathsep + env.get("PATH", "")
    return env
