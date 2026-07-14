"""Single-command launcher.

    python run.py            # production-like: serves the built frontend on :8420
    python run.py --dev      # development: hot-reloading frontend + backend on :5173

In production mode the FastAPI backend serves the pre-built frontend from
``frontend/dist`` (run ``npm run build`` first). In dev mode this launcher also
starts the Vite dev server, so both frontend and backend changes reload live —
open http://localhost:5173.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn

from backend.config import settings

FRONTEND_DIR = Path(__file__).resolve().parent / "frontend"


def _open_browser(url: str, delay: float = 1.5) -> None:
    threading.Timer(delay, lambda: webbrowser.open(url)).start()


def _stop(proc: subprocess.Popen) -> None:
    """Terminate a child process and its tree (Vite spawns node under cmd.exe)."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
    else:
        proc.terminate()


def _run_dev() -> None:
    # Start the Vite dev server (hot reload for the frontend).
    vite = subprocess.Popen("npm run dev", cwd=FRONTEND_DIR, shell=True)
    url = "http://localhost:5173"
    print(f"\n  Mythforge (dev) — {url}\n")
    _open_browser(url, delay=2.5)
    try:
        # reload_dirs keeps the watcher on the backend; Vite handles the frontend.
        uvicorn.run(
            "backend.main:app",
            host="127.0.0.1",
            port=settings.app_port,
            reload=True,
            reload_dirs=["backend"],
        )
    finally:
        _stop(vite)


def _run_prod() -> None:
    url = f"http://localhost:{settings.app_port}"
    print(f"\n  Mythforge — {url}\n")
    _open_browser(url)
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=settings.app_port,
        reload=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Mythforge.")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Hot-reloading dev mode: also starts the Vite dev server on :5173.",
    )
    args = parser.parse_args()

    settings.ensure_dirs()
    if args.dev:
        _run_dev()
    else:
        _run_prod()


if __name__ == "__main__":
    main()
