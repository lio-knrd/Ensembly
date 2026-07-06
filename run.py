"""Single-command launcher.

    python run.py

Starts the FastAPI backend (which also serves the built frontend if present),
then opens the browser to the app. During development, run the Vite dev server
separately (`cd frontend && npm run dev`) for hot reload.
"""
from __future__ import annotations

import threading
import webbrowser

import uvicorn

from backend.config import settings


def _open_browser(url: str) -> None:
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()


def main() -> None:
    settings.ensure_dirs()
    url = f"http://localhost:{settings.app_port}"
    print(f"\n  AI Video Pipeline Studio — {url}\n")
    _open_browser(url)
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
