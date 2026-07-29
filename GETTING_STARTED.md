# Getting Started

A local web app that produces short-form narrated videos through a semi-automated
AI pipeline (script → audio+timestamps → images → optional clips → final render).
See [`README.md`](README.md) for the full spec.

> **Runs with zero API keys.** Every stage has an offline placeholder fallback, so
> you can drive the whole pipeline end-to-end (real FFmpeg render included) before
> adding any keys. Add keys in `.env` to swap in the real models per stage.

## Prerequisites

- **Python 3.11+**
- **Node.js 18+** (only to build the frontend)
- **FFmpeg** on your `PATH` (required for the final render; captions/Ken Burns)
  - Windows: `winget install Gyan.FFmpeg` · macOS: `brew install ffmpeg` · Linux: `apt install ffmpeg`

## 1. Configure

```bash
cp .env.example .env
```

Leave the keys blank to run fully offline, or fill any of:

| Stage | Variable | Provider |
|---|---|---|
| Script | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` + `DEFAULT_LLM_PROVIDER` | Anthropic / OpenAI |
| Audio + timestamps | `ELEVENLABS_API_KEY` | ElevenLabs |
| Images & video | `KREA_API_KEY` / `FAL_API_KEY` | Direct Krea for Krea 2; fal.ai for other image/video models |
| Music | `JAMENDO_CLIENT_ID` | Jamendo royalty-free soundtrack search |
| Publishing | `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` | TikTok Content Posting |
| Publishing | `YOUTUBE_CLIENT_ID` / `YOUTUBE_CLIENT_SECRET` | YouTube Data API v3 — see [`YOUTUBE_SETUP.md`](YOUTUBE_SETUP.md) |

## 2. Install & build

```bash
# Backend (a virtualenv is recommended)
python -m venv .venv
# Windows: .venv\Scripts\activate   ·   macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# Frontend (build once so run.py can serve it)
cd frontend
npm install
npm run build
cd ..
```

## 3. Run

```bash
python run.py
```

This starts the backend on `http://localhost:8420` (serving the built frontend)
and opens your browser.

### Development mode (hot reload)

Run the backend and the Vite dev server separately:

```bash
# Terminal 1 — backend
python run.py

# Terminal 2 — frontend with hot reload (proxies /api, /media, /ws to :8420)
cd frontend && npm run dev      # http://localhost:5173
```

## Using the app

1. **Board** — kanban of every project across the pipeline stages. Click **+ New
   Project**, give it a topic and duration; script generation starts automatically.
2. **Project detail** — review the pipeline scene-by-scene. Approval checkpoints:
   **script** → **cast (character sheets)** → **storyboard/images** → **clips**.
   Approving the script auto-runs narration audio, then pauses at **cast review**:
   any characters the script references but that have no reference sheet yet are
   flagged. Generate the missing sheets automatically (leave the description blank
   to let the AI write it, or type your own), then **Approve cast** — only then are
   the scene images generated, using each character's sheet for identity lock.
   Approving clips renders the final video. Regenerate any single scene or sheet
   without restarting.
3. **Character library** — global characters (name, description, reference image)
   referenced across projects for identity-locked visuals.
4. **Idea backlog** — queue topics and convert them into projects.
5. **Publishing** — link a TikTok account or a YouTube channel once in Settings,
   then pick it on a content preset. Any finished project in that group can be
   posted from its **Final video** panel. Both platforms restrict what an
   unaudited developer app may publish, so start with a TikTok draft or a private
   YouTube upload — details in [`YOUTUBE_SETUP.md`](YOUTUBE_SETUP.md).
6. **Settings** — edit platform & content presets (the two editable prompt
   layers), set defaults, and check which API keys / FFmpeg are loaded. Each
   **content preset** has a separate **Image style** field — a look applied to all
   generated images (character sheets + scenes) so you don't tweak per-scene
   prompts; it is kept out of the script prompt.

## Output on disk

Everything lands under `data/` (git-ignored):

```
data/
  characters/<name>/reference.png, variants/, metadata.json
  projects/<date>_<slug>/
    project.json script.json
    audio/  images/  clips/  final/<slug>.mp4 + metadata.json
```

The finished MP4 and its social metadata are in `final/`. Upload it manually, or
publish it from the project page to the TikTok account / YouTube channel the
project's content preset points at.
