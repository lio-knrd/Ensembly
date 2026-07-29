# Ensembly

A **local web app** for producing short-form narrated videos (TikTok/Shorts/Reels
style) through a semi-automated AI pipeline. The user reviews and approves at four
checkpoints (script, cast, storyboard/images, video clips); everything else runs
automatically. The app is a **production dashboard**, not just a script — it shows
project status on a kanban board, storyboards, a grouped character library, an
editorial plan for an ordered body of work, and one-click publishing to TikTok and
YouTube.

The pipeline is **topic-agnostic**. It ships with defaults for Greek mythology and
a second, animation-enabled preset for math/science, but the prompt layers driving
script generation are editable in-app, so the same pipeline can be repointed at any
topic without code changes.

- **New here?** [`GETTING_STARTED.md`](GETTING_STARTED.md) — install, keys, first run.
- **Serving/deployment:** [`INFRA.md`](INFRA.md) — how it runs today and the hosting plan.
- **Publishing:** [`YOUTUBE_SETUP.md`](YOUTUBE_SETUP.md) and [`TIKTOK_SETUP.md`](TIKTOK_SETUP.md) — OAuth clients, keys, limits.
- This file is the reference for *what the system is and how it is put together*.

What stays fixed regardless of topic:
- The pipeline stages and data model (script → audio+timestamps → cast → images →
  optional clips/animations → final render)
- Adjustable target video length per project (not hardcoded)
- Every project produces: narration script, per-scene image prompts, generated
  images, TTS audio with word-level timestamps, optional generated video clips or
  deterministic animations, a cover/title card, a final rendered video, and social
  metadata (title, description, hashtags)

## 1. Non-goals

- No mobile app. This runs on `localhost` on the user's own machine (see
  [`INFRA.md`](INFRA.md) for the eventual hosting shape).
- No user accounts/auth — single user, local tool.
- No general in-app model marketplace. One default model per stage, selected in
  config; the only user-facing model choice is the image model (two options).

Note: "no auto-posting" was an original non-goal and **no longer holds** — the app
publishes to TikTok via the official Content Posting API and to YouTube via the
Data API v3 (section 6). Rendered files still land on disk and can always be
uploaded by hand instead.

## 2. Tech stack

- **Backend**: Python + FastAPI, run by uvicorn
- **Background jobs**: an in-process `ThreadPoolExecutor` (`pipeline.submit`), with
  per-project job tracking so a run can be canceled. Progress is broadcast to the
  UI over a WebSocket event bus (`backend/events.py`, `/ws`).
- **Database**: SQLite via SQLModel (single file, zero setup). Schema changes are
  applied as additive `ALTER TABLE` migrations on startup — see `_ADDED_COLUMNS`
  in [`backend/database.py`](backend/database.py). There is no Alembic.
- **Frontend**: React (Vite), React Router, TanStack Query, lucide icons. Plain CSS
  in one stylesheet with a light/dark theme toggle.
- **Media processing**: FFmpeg (subprocess) for Ken Burns segments, clip
  normalization, audio concat, music mixing, and burned-in ASS captions
- **Animations**: optional [Manim](https://www.manim.community/) for deterministic
  math/science scenes, rendered in an isolated subprocess
- **Run command**: `python run.py` (production-like, serves the built frontend on
  `:8420`) or `python run.py --dev` (Vite dev server on `:5173` + hot-reloading
  backend). A `Dockerfile`/`docker-compose.yml` exist but are not the primary path.

Everything runs **without any API keys**: each stage has an offline placeholder
adapter, gated by `ALLOW_OFFLINE_FALLBACK` (default on), so the full pipeline
including a real FFmpeg render is exercisable before any provider is configured.

## 3. Environment & configuration

All secrets live in a single `.env` at the project root (never committed; see
[`.env.example`](.env.example)). Nothing else holds API keys.

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, `DEFAULT_LLM_PROVIDER` | Script generation, Manim code authoring, editorial suggestions, scope analysis |
| `ELEVENLABS_API_KEY` | TTS with word-level timestamps |
| `FAL_API_KEY` | fal.ai image + image-to-video |
| `KREA_API_KEY`, `KREA_ASSET_CACHE_FILE` | Direct Krea image generation (used when the Krea model is active) |
| `JAMENDO_CLIENT_ID` | Royalty-free music search |
| `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, `TIKTOK_REDIRECT_URI`, `TIKTOK_USE_PKCE` | TikTok Login Kit + Content Posting |
| `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REDIRECT_URI` | YouTube Data API v3 upload (see [`YOUTUBE_SETUP.md`](YOUTUBE_SETUP.md)) |
| `APP_PORT`, `PROJECTS_DIR`, `CHARACTERS_DIR`, `DATABASE_URL` | App config |
| `ALLOW_OFFLINE_FALLBACK` | Placeholder generation when a key is missing (default `true`) |
| `ANIMATION_ENGINE`, `ELEVENLABS_MAX_CHARS` | Animation engine (`manim`/`offline`), TTS request chunk size |

**Per-stage model selection is a constants concern, not a settings table.** The
active model per stage lives in [`backend/config.py`](backend/config.py) and the
concrete class is picked in [`backend/adapters/registry.py`](backend/adapters/registry.py).
Every stage sits behind a small adapter interface (`ScriptGenerator`,
`TTSGenerator`, `ImageGenerator`, `VideoGenerator`, `AnimationGenerator` in
[`backend/adapters/base.py`](backend/adapters/base.py)), so adding a model is a new
class plus a registry line, not a pipeline change. The one exception exposed in the
UI is the **image model** (FLUX.1 dev vs Krea 2 Medium), stored as the
`active_image_model` setting.

## 4. Prompt system: three layers

Script generation is built from three layers combined at generation time, not one
editable blob.

**Layer 1 — Core system prompt (fixed, in code).** Lives in
[`backend/prompts.py`](backend/prompts.py). It is the contract with the rest of the
pipeline: always return the same structured scene list (narration text, image
prompt, continuity links, scene type, characters) plus social metadata, so the
downstream stages can consume it no matter which presets are active. Enforced with
a JSON schema (`SCRIPT_JSON_SCHEMA`) via structured output.

**Layer 2 — Platform preset (editable, in DB).** The delivery format: target
length, hook/ending conventions, required metadata. Ships with "TikTok".

**Layer 3 — Content preset (editable, in DB).** Subject matter and tone. Ships with
"Greek Mythology" and "Math & Science (animated)". A content preset also carries
four things beyond the prompt:
- `image_style_prompt` — a visual style applied to **image generation only**
  (character sheets + scene images), deliberately kept out of the script prompt so
  scene prompts stay style-neutral and the look stays consistent. It can be
  AI-drafted from the content prompt.
- `animation_style_prompt` — the same idea for **animation scenes only**: a house
  style (palette, type sizes, layout) handed to the Manim authoring call so
  diagrams carry the group's look instead of the catalog's generic defaults. Also
  kept out of the script prompt. Shown in the preset editor only when
  `enable_animations` is on, and empty by default — an empty value leaves the
  authoring prompt byte-identical to a build without this field.
- `voice_id` — the ElevenLabs voice for this group.
- `enable_animations` — opt-in; when on, the script LLM may mark scenes as
  deterministic animations (off by default so narrative presets never get diagrams).

A content preset doubles as a **group**: characters and the publishing accounts
(TikTok, YouTube) belong to one, so "Zeus" in the mythology group and a "Zeus" in
another group are separate library entries and never cross-match.

Final prompt = core system prompt + platform preset + content preset
(+ animation guidance if enabled) + the project's idea and target duration.

## 5. Data model

SQLModel tables in [`backend/models.py`](backend/models.py). Highlights rather than
every column:

### `projects`
Title (plus `title_is_custom`, so a generated title only replaces a working one),
topic prompt, platform/content preset FKs, target duration, stage, folder path,
status message / error / `failed_stage`, `cancel_requested`.
Also: soundtrack (`music_track_id`, `music_enabled`, `music_volume`), burned-in
subtitles (`subtitles_enabled`, `subtitle_position` — per project, so adjusting one
never moves the default for the next), and the cover/title card (source image,
kicker, title text, part label, prompt, status, version, variants).

### `scenes`
Order index, narration text, image prompt, `continuity_context` (explicit links to
earlier scene images for prop/location consistency), `scene_type`
(`still` | `video` | `animation`), character ids + per-scene `character_assignments`
(which character *form* to use), `suggested_characters` the library lacks, asset
paths (`audio_path`, `timestamps_path`, `image_path`, `clip_path`,
`animation_path`), the typed `animation_spec`, duration derived from TTS
timestamps, `approved`, and per-scene `status`.
Every generated candidate is retained in `image_variants` / `clip_variants` /
`animation_variants` / `audio_variants`; the singular paths point at the currently
selected take. fal queue state (`video_request_id` and friends) is persisted so a
timeout or restart resumes instead of paying for a duplicate generation.

### `characters` and `character_forms`
Characters are scoped to a content preset group, with a description, a reference
image (the identity lock), the prompt/style used to generate it, and selectable
`reference_variants`. **Forms** are alternate identity states of one character
(pre-curse vs post-curse, human vs divine form, child vs adult) with their own
reference sheets and trigger phrases — not clothing, mood, or pose.

### `platform_presets`, `content_presets`, `settings`
As described in section 4. `settings` is a key-value table for defaults (duration,
default presets, default music track, active image model).

### `ideas`, `editorial_plans`, `editorial_items`
The backlog grew into **editorial planning**: a plan is a reusable editorial context
(name, description, editorial rules, ordering mode, optional parent plan, preset
bindings) holding an ordered list of items. An item can be planned, external,
completed, or AI-suggested, may be grouped into multi-part series
(`part_group_id`/`part_number`), and converts into a project in one call.

### `music_tracks`, `tiktok_accounts`, `youtube_accounts`
A shared soundtrack library (bundled local track + Jamendo results, cached on disk)
and the authorized publishing accounts — TikTok creators and YouTube channels
(tokens, expiries, scopes) — owned by the app and *selected* by groups, so a second
group posting as the same creator never re-runs OAuth.

## 6. Feature map

**Board (home)** — kanban across pipeline stages, one card per project with title
card thumbnail, duration, and status pill. New projects can first be run through
**scope analysis**: the LLM judges whether the topic fits the target duration and,
if not, splits it into a coherent two-part series with an explicit hinge, reserved
material, and a bridging opening for part 2.

**Project detail** — the main workspace: title/cover card editor (generate a
background or reuse a scene image, then overlay kicker + title + part label),
scene-by-scene review with narration, image, audio take, scene type, per-scene
regenerate and variant selection, character sheets for the cast, soundtrack
picker with volume, subtitle toggle and vertical placement, the final video, and
TikTok publishing.

**Character library** — grouped characters with reference sheets and forms;
generate a sheet from an AI-written or hand-written description, regenerate, or
pick a previous variant.

**Editorial planning** — plans, ordered items, drag-to-reorder, AI-suggested next
works with rationale, part grouping, and conversion into projects.

**Settings** — defaults, active image model, environment status (which keys are
present, masked; whether FFmpeg is on PATH), platform/content preset editing
including image style and voice, and TikTok account linking.

**Publishing** — two destinations, selected independently per group.

*TikTok*: Login Kit OAuth links creator accounts once; each group selects one. A
finished project can be sent as a **Direct Post** or as a **draft to the creator's
inbox**. Two TikTok-imposed limits are reflected in the UI: until the developer app
passes TikTok's audit every Direct Post is forced private, and the redirect URI must
be an absolute https URL (a local install uses a tunnel or the
paste-the-redirected-URL fallback).

*YouTube*: Google OAuth links channels once; each group selects one. A finished
project is uploaded with `videos.insert` (resumable, resumes after a dropped
connection) with the title, description, and tags pre-filled from the project
metadata. A 9:16 render under 3 minutes is classified as a Short by YouTube itself
— there is no Shorts endpoint. The equivalent limits, also surfaced in the UI:
uploads from an unaudited API project are locked to private, there are 100 uploads
per day, and a consent screen left in "Testing" expires the link after 7 days.
Unlike TikTok, Google allows a `localhost` redirect, so linking needs no tunnel.
Setup: [`YOUTUBE_SETUP.md`](YOUTUBE_SETUP.md).

**Recovery controls** — cancel a running stage, step back or step forward through
stages, and retry a failed step. Interrupted stages are detected on restart.

## 7. Pipeline stages (state machine)

```
IDEA
  → SCRIPT_GENERATING → SCRIPT_READY → [USER APPROVAL] → SCRIPT_APPROVED
  → AUDIO_GENERATING (auto — per-scene TTS + word timestamps)
  → CAST_REVIEW → [USER APPROVAL: generate any missing character sheets]
  → STORYBOARD_GENERATING (per-scene images + title card) → STORYBOARD_READY → [USER APPROVAL]
  → STORYBOARD_APPROVED
  → CLIPS_GENERATING (image-to-video for "video" scenes; Manim for "animation" scenes)
  → CLIPS_READY → [USER APPROVAL] → CLIPS_APPROVED
  → RENDERING (ffmpeg: segments, audio mux, music bed, burned captions, metadata)
  → DONE
```
Plus `CANCELED` and `FAILED`, both recoverable. Any stage can be re-run for a
single scene or the whole project without restarting from IDEA.

## 8. Per-stage detail

**Script generation** — one LLM call with the combined three-layer prompt and the
structured-output schema. Returns scenes plus social metadata, including cover copy
(`cover_kicker`, `cover_title`) used by the title card. Characters are returned with
a `state` and `state_importance` so major identity changes map to character forms.
Consecutive animation scenes are merged into one continuous animation with a single
narration take.

**Audio + timestamps** — ElevenLabs per scene via the timestamps endpoint, so each
scene gets its own MP3 and word-level JSON; long narration is split across requests
(`ELEVENLABS_MAX_CHARS`) and stitched back into one file and one merged timeline.
Scene duration is derived from the timestamps and becomes the target duration for
that scene's image-to-video or animation.

**Cast review** — the pipeline computes the cast from the script, matched against
the project group's character library. Anything missing is flagged; sheets are
generated (AI-written description if none is supplied) and scene assignments are
reconciled to the right character form before images are made.

**Storyboard / images** — per scene via the active image model, with the content
preset's image style applied, character reference sheets attached for identity lock,
and continuity references to earlier scene images where the script asked for them.
The title card background is generated here too if it does not exist yet.

**Video clips** — only for `video` scenes, image-to-video on fal, duration matched
to the scene's audio, optionally animating toward the next scene's still as an end
frame. Queue state is persisted so a restart resumes rather than re-submits.

**Animations** — only for `animation` scenes, and only when the content preset opts
in. After audio exists, the LLM authors Manim code from that scene's narration and
the group's `animation_style_prompt`; cue
phrases in the code are matched against the real word timings
([`backend/animation/narration.py`](backend/animation/narration.py)) so the diagram
lands on the words. The code is rendered in an isolated subprocess with a repair
pass on failure, and falls back to a placeholder card when Manim (or LaTeX, for
typeset math) is unavailable.

**Final render (ffmpeg)** — stills become segments via a subtle Ken Burns move at
the scene's exact duration; clips and animations are normalized to the same size and
frame rate. Scene audio is concatenated, the music bed is mixed under it at the
project's volume, and word-synced captions are burned in from the merged timeline as
an ASS subtitle file at the project's chosen vertical position. Output lands in
`final/<slug>.mp4`.

**Metadata** — `final/metadata.json` carries the title, description, hashtags,
suggested caption, hook text, and required soundtrack attribution.

## 9. Folder structure on disk

Characters are global to their group; projects reference them and never copy
character images into project folders.

```
/data
  /characters
    zeus/
      reference.png
      variants/
      metadata.json
  /music/                       # bundled + downloaded soundtrack files
  /projects
    2026-07-06_theseus-and-the-minotaur/
      project.json              # denormalized snapshot of the DB row
      script.json               # full scene list as returned by the LLM
      audio/                    # scene_NN.mp3 + .timestamps.json, full_narration.mp3, timeline.json
      images/                   # scene_NN.png, title_card*.png
      clips/                    # only scenes marked "video"
      animations/               # only scenes marked "animation"
      final/
        segments/               # per-scene normalized video segments
        captions.ass
        theseus-and-the-minotaur.mp4
        metadata.json
  app.db
  krea_asset_cache.json
```

## 10. Layout of the code

```
backend/
  main.py          FastAPI app, WebSocket, serves frontend/dist in production
  config.py        .env loading + per-stage model constants
  models.py        SQLModel tables
  database.py      engine, startup migrations, legacy backfills
  prompts.py       Layer 1 core prompt + JSON schema
  pipeline.py      the whole state machine and every generation step
  events.py        thread-safe WebSocket event bus
  storage.py       on-disk folder helpers
  seed.py          default presets and settings
  adapters/        base interfaces + llm / tts / image / video / animation + registry
  animation/       Manim template catalog, narration cue sync, LaTeX detection
  routers/         projects, characters, presets, settings, ideas, voices, music,
                   media, tiktok, youtube
  services/        ffmpeg, jamendo, music_library, tiktok, youtube
frontend/src/
  App.jsx          sidebar + routes
  api.js           relative-path API client
  useEvents.js     WebSocket subscription
  pages/           Board, ProjectDetail, Characters, Ideas, Settings
tests/             unittest: animation (spec/merge/sync/pipeline), cast, character
                   groups, asset selection, subtitles, TikTok, YouTube, TTS chunking
```

The tests are stdlib `unittest`, so they need no extra dependency:

```bash
python -m unittest discover -s tests
```

## 11. UI conventions

Tone: calm, minimalist, modern — generous whitespace, muted palette, no dashboard
clutter. Minimalist must not mean hiding information behind extra clicks.

House rules the frontend follows: lucide icons only (no emoji or decorative
non-ASCII in the UI), equal-width buttons in a row, explicit dirty-state saves
rather than silent autosave, and the light/dark "ink and ember" palette in
`styles.css`.

## 12. Open implementation decisions

Where this document does not dictate an exact approach (component structure, exact
caption styling, exact Ken Burns curve, job queue internals), use good judgment and
standard practice. The hard requirements are the ones above: the fixed Layer 1
contract, adapter-per-stage model selection, per-scene approval and regeneration,
group-scoped characters, keys only in `.env`, and a calm UI.
