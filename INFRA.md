# Infrastructure & Deployment Notes

Living document for how Mythforge is served today and the decisions to make
when we host it. Not a task board for features — this is about runtime, serving,
and deployment. Update it as decisions get made.

---

## How it runs today

Two pieces, one API:

| Piece | Port | Role | Live changes? |
| --- | --- | --- | --- |
| Backend (FastAPI on **uvicorn**) | 8420 | Serves the API (`/api`, `/media`, `/ws`) **and** the built frontend from `frontend/dist` | Python: only with `--dev` reload; frontend: needs `npm run build` |
| Frontend (Vite dev server) | 5173 | Serves live source with hot reload; proxies `/api`, `/media`, `/ws` to 8420 | Yes, instant |

Launcher: [`run.py`](run.py)
- `python run.py` — production-like. Backend only, serves the pre-built `dist` on 8420. Frontend changes require `npm run build` first.
- `python run.py --dev` — starts the Vite dev server **and** the backend (with Python hot-reload), opens http://localhost:5173. Use this for development.

**During development, view the app at `localhost:5173`, not 8420.** 5173 is live; 8420 is the frozen build.

### Clearing up two misconceptions
- **uvicorn is not SSR and not optional.** It is the ASGI web server that runs the FastAPI Python app (opens the port, handles HTTP). FastAPI cannot listen on a port without one. It stays in every deployment shape.
- **There is no server-side rendering.** The frontend is a plain client-side React SPA. `npm run build` emits static HTML/JS/CSS; the browser downloads the bundle and renders in-browser. The backend serving those static files from `dist` is just file-serving, not rendering.

So we are already "backend = API, frontend = client-side app." The only twist is that the
backend *also* hands out the static frontend files, which keeps production to a single
process/port/domain.

### Why this is easy to change later
Every frontend call uses **relative paths** (`/api/...`, `/media/...`, `/ws`) — see
[`frontend/src/api.js`](frontend/src/api.js). No hardcoded host. That means single-origin
works with zero config, and splitting to a separate frontend host later is a small change
(add an API base URL + CORS), not a rewrite. We are not locked in.

---

## Deployment options

### A. Single-origin (current shape)
One process serves both API and the built frontend, same domain.
- Pro: dead simple — one deploy, one port, one domain, **no CORS**. Ideal for a single VPS.
- Con: frontend not on a CDN; frontend and backend coupled (a backend restart drops both).

### B. Split (frontend on a CDN, backend = API only)
Frontend on a static host/CDN (Cloudflare Pages, Vercel, Netlify, or nginx); backend serves only `/api`.
- Pro: frontend from a global CDN (fast, usually free tier); scales independently; standard for larger apps.
- Con: two deploys; must configure **CORS** and point the frontend at the API's URL.

---

## Recommendation / plan of record

- **Now:** keep the current single-origin setup. It costs nothing, `--dev` covers development, `python run.py` is a clean single-command deploy. Do **not** split prematurely.
- **Custom VPS (most likely):** stay single-origin, put **Caddy or nginx in front** of uvicorn for TLS/HTTPS and robustness.
  - Simplest: Caddy terminates TLS, reverse-proxies everything to uvicorn on `127.0.0.1:8420`; uvicorn serves both API and `dist` (today's code unchanged). Caddy handles certificates automatically.
  - Slightly faster: nginx serves `dist` static files directly and proxies only `/api`, `/ws`, `/media` to uvicorn.
  - uvicorn already binds `127.0.0.1`, so only the reverse proxy can reach it — keep it that way behind a proxy.
- **Managed provider (if we go that way instead):** split. Frontend -> Cloudflare Pages / Vercel / Netlify; backend -> Fly.io / Railway / Render. Then add the API base URL env var + CORS.

---

## Production checklist / backlog

Things to handle when we actually deploy (not needed for local dev):

- [ ] **Reverse proxy + TLS** — Caddy (auto-certs, simplest) or nginx in front of uvicorn. Add a `Caddyfile` / nginx config to the repo when the host is chosen.
- [ ] **Process management** — keep the backend running and restart on crash/reboot (systemd unit, or the platform's process manager). `run.py` is for launching, not supervising.
- [ ] **Config via env** — confirm `APP_PORT` and any secrets/API keys come from env / `.env`, nothing hardcoded. Document required vars.
- [ ] **Build step in deploy** — production serving depends on `frontend/dist`. Deploy must run `npm run build` (or ship a prebuilt `dist`). Decide: build on the box vs. build in CI and copy.
- [ ] **Workers / concurrency** — start with a **single** uvicorn worker. The `/ws` event bus and any in-memory state are per-process; multiple workers need a shared broker (e.g. Redis pub/sub) before scaling out. Revisit only if load requires it.
- [ ] **If/when splitting (option B):** add an API base URL env var to the frontend, enable CORS on the backend for the frontend origin, and update the WebSocket URL.
- [ ] **Media/storage** — `media/` files are served off local disk. On a VPS that is fine; on ephemeral/managed hosts, decide on persistent storage or object storage (e.g. S3-compatible).
- [ ] **Backups** — whatever holds project state/data needs a backup story before this is more than a personal tool.
- [ ] **Logging/observability** — where do uvicorn logs go in production; do we want structured logs / a basic error alert.

---

## Decision log

_Record dated decisions here as they are made (host chosen, proxy chosen, split-or-not, etc.)._

- 2026-07-14: Documented current architecture. No hosting decision yet. Staying single-origin for now; leaning VPS + Caddy when we deploy.
