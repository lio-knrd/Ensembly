# Ensembly — Project Spec

## 1. What this is

A **local web app** for producing short-form narrated videos (TikTok/Shorts/Reels style) through a semi-automated AI pipeline. The user reviews and approves at three checkpoints (script, storyboard/images, video clips); everything else runs automatically. The app is a **production dashboard**, not just a script — it shows project status, storyboards, a character library, and an idea backlog.

The pipeline is **topic-agnostic**. It ships with a default configuration for Greek mythology, but the system prompt driving script generation must be editable in-app (or via config), so the same pipeline can later be repointed at any other topic/niche without code changes.

What stays fixed regardless of topic:
- The pipeline stages and data model (script → audio+timestamps → image prompts → images → optional video clips → final render)
- Adjustable target video length per project (e.g. 60s, 90s, 120s — not hardcoded)
- Every project produces: narration script, per-scene image prompts, generated images, TTS audio with word-level timestamps, optional generated video clips, a final rendered video, and social metadata (title, description, hashtags)

## 2. Explicit non-goals

- No auto-posting to TikTok or any platform. Output lands in a folder; the user uploads manually.
- No mobile app, no hosting/deployment concerns — this runs on `localhost` on the user's own machine.
- No user accounts/auth — single user, local tool.

## 3. Tech stack

- **Backend**: Python + FastAPI
- **Background jobs**: FastAPI `BackgroundTasks` or a simple async task queue (e.g. `arq` or in-process asyncio queue) — long-running generation must not block the UI. The UI polls or uses SSE/WebSocket for live progress updates.
- **Database**: SQLite (single file, zero setup)
- **Frontend**: React (Vite), served locally. Follow standard React best practices — clean component structure, sensible state management (React Query/TanStack Query for server state is a good fit here given the polling/async job nature of the app), no unnecessary abstraction. Use established external libraries where they meaningfully simplify things or improve UI/UX (e.g. a headless component library like Radix or shadcn/ui for accessible primitives, a charting/waveform library for audio visualization) rather than hand-rolling everything — but keep dependencies purposeful, not sprawling. Talks to the FastAPI backend over REST + WebSocket/SSE for progress.
- **Media processing**: FFmpeg (invoked via subprocess) for concatenation, audio muxing, and burned-in captions
- **Run command**: a single command (e.g. `python run.py` or `./start.sh`) starts the backend, and either auto-opens the browser to `http://localhost:PORT` or prints the URL. No Docker requirement, but a `docker-compose.yml` is a nice-to-have if it doesn't add friction.

## 4. Environment & configuration

All secrets and environment-specific config live in a single `.env` file at the project root (never committed — include `.env.example` with placeholders). Nothing else should hold API keys.

`.env.example`:
```
# LLM for script generation (pick one, code should support switching)
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
DEFAULT_LLM_PROVIDER=anthropic   # or "openai"

# Text-to-speech
ELEVENLABS_API_KEY=

# Image + video generation (aggregator — one key covers many models)
FAL_API_KEY=

# App config
APP_PORT=8420
PROJECTS_DIR=./data/projects
CHARACTERS_DIR=./data/characters
DATABASE_URL=sqlite:///./data/app.db
```

For a first version, **do not build in-app model switching** — that's more complexity than this needs right now. Instead, implement exactly **one default model per stage** (one LLM for script generation, one image model, one video model), configured via constants/config rather than a settings UI.

However, write each stage's model call behind a small interface/adapter (e.g. an abstract `ScriptGenerator`, `ImageGenerator`, `VideoGenerator` base class per stage, with one concrete implementation each for now). This keeps the door open to add more models and in-app switching later without restructuring the pipeline — a future concrete class plus a settings entry should be all that's needed, not a rewrite. The `.env` holds the raw API keys as already specified; a simple config file or constants module (not a DB `settings` table, for now) picks which concrete implementation is active per stage.

## 5. Prompt system: three layers

Script generation is built from **three layers of prompt** that get combined at generation time, not one editable blob. This keeps the tool's actual purpose locked in place while still making it fully reusable for other platforms and topics.

**Layer 1 — Core system prompt (fixed, not editable, lives in code)**
This is the tool's contract with itself and never changes regardless of platform or topic. It instructs the LLM that this is for generating a narrated short-form video, and that it must always return the same structured output: an ordered list of scenes, each with narration text, an image-generation prompt, a suggested scene type (still/video), and any character references — i.e. exactly the structured JSON script object defined in the data model below. This layer is what guarantees every generation, no matter what platform/content preset is active, is actually usable by the rest of the pipeline (audio+timestamps generation, image generation, etc.). This should live as a constant in code, not the database.

**Layer 2 — Platform preset (editable, selectable, stored in DB)**
Defines the *delivery format*: target platform conventions, duration range, and what metadata is required. E.g. a "TikTok" preset specifies: target length ~60–90s, needs a strong hook in the first 1-2 seconds, a satisfying or cliffhanger ending, and that output must include a title, description, and hashtags suited to the platform. A user can add more platform presets later (e.g. "YouTube Shorts" with different conventions) without touching code.

**Layer 3 — Content/topic preset (editable, selectable, stored in DB)**
Defines the *subject matter and tone* — this is the "Greek Mythology" layer. E.g.: retell a specific myth faithfully to the classical sources, use an engaging narrator voice, keep names/events historically consistent. A user can add more topic presets later (e.g. "Norse Mythology," "True Crime," whatever) fully independently of the platform layer — any topic preset can be paired with any platform preset.

At generation time, the final prompt sent to the LLM is: **core system prompt + active platform preset + active topic preset + the project's one-line idea/target duration**. Ship one default of each preset type: platform = "TikTok," topic = "Greek Mythology."

Regardless of which presets are active, the LLM must return the same **structured JSON script object** (see data model below) — the structure is the fixed contract from Layer 1; platform and topic only shape tone, format, and metadata.

## 6. Data model

### `projects`
| field | type | notes |
|---|---|---|
| id | uuid/pk | |
| title | text | working title |
| topic_prompt | text | the user's one-line idea, e.g. "Theseus and the Minotaur" |
| platform_preset_id | fk | which platform/format preset was used (e.g. TikTok) |
| content_preset_id | fk | which content/topic preset was used (e.g. Greek Mythology) |
| target_duration_seconds | int | adjustable per project, e.g. 60–90 |
| stage | enum | see pipeline stages below |
| folder_path | text | path under `PROJECTS_DIR` |
| created_at / updated_at | datetime | |

### `scenes` (belongs to a project)
| field | type | notes |
|---|---|---|
| id | pk | |
| project_id | fk | |
| order_index | int | |
| narration_text | text | what's spoken |
| image_prompt | text | for image generation |
| scene_type | enum | `still` or `video` — user or LLM decides which scenes deserve motion |
| character_ids | array/join table | which global characters appear |
| audio_path | text | scene mp3 |
| timestamps_path | text | word-level JSON from ElevenLabs |
| image_path | text | generated still |
| clip_path | text | generated video clip, if scene_type = video |
| duration_seconds | float | derived from TTS timestamps — this is the target duration for image/video gen |
| approved | bool | per-scene approval, in addition to stage-level "approve all" |

### `characters` (global, not project-scoped)
| field | type | notes |
|---|---|---|
| id | pk | |
| name | text | e.g. "Zeus" |
| description | text | appearance notes, used to build prompts |
| reference_image_path | text | canonical identity-lock image |
| variant_paths | array | optional alternate refs (armored, angry, etc.) |
| created_at | datetime | |

### `project_characters` (join table)
`project_id`, `character_id` — many-to-many.

### `platform_presets`
`id`, `name` (e.g. "TikTok"), `format_prompt` (duration guidance, hook/ending conventions, required metadata fields), `is_default`

### `content_presets`
`id`, `name` (e.g. "Greek Mythology"), `content_prompt` (subject matter, tone, factual constraints), `is_default`

### `settings`
Simple key-value table for things that genuinely need to be user-adjustable now: default duration, default platform preset, default content preset, etc. Per-stage model selection is **not** in here for v1 — see section 4a on the adapter interface; that's a config/constants concern for now, not a DB-backed setting.

## 7. Folder structure on disk

Characters are **global**, projects reference them — do not copy character images into project folders.

```
/data
  /characters
    zeus/
      reference.png
      variants/
      metadata.json
    hercules/
      ...
  /projects
    2026-07-06_theseus-and-the-minotaur/
      project.json              # denormalized snapshot of DB row, for portability/backup
      script.json               # full scene list as returned by the LLM
      audio/
        scene_01.mp3
        scene_01.timestamps.json
        ...
        full_narration.mp3
      images/
        scene_01.png
        ...
      clips/
        scene_03.mp4            # only scenes marked "video"
        ...
      final/
        theseus-and-the-minotaur.mp4
        metadata.json           # title, description, hashtags (see below)
  app.db
```

## 8. Pipeline stages (state machine)

```
IDEA
  → SCRIPT_GENERATING → SCRIPT_READY → [USER APPROVAL] → SCRIPT_APPROVED
  → AUDIO_GENERATING (auto, no approval — per-scene TTS + timestamps)
  → STORYBOARD_GENERATING (per-scene images, using character refs) → STORYBOARD_READY → [USER APPROVAL] → STORYBOARD_APPROVED
  → CLIPS_GENERATING (image-to-video only for scenes marked "video") → CLIPS_READY → [USER APPROVAL] → CLIPS_APPROVED
  → RENDERING (ffmpeg: concat clips/stills, mux audio, burn captions from timestamps, generate metadata)
  → DONE
```

Failure/regeneration handling: any stage can be re-run for a single scene or for the whole project without restarting from IDEA (e.g. regenerate one bad image without redoing the script).

## 9. Per-stage detail

**Script generation**: LLM call using the combined prompt (core system prompt + active platform preset + active content preset). Input: topic + target duration. Output: JSON list of scenes, each with `narration_text` and `image_prompt`, following the required hook/ending conventions from the active platform preset. The LLM should also flag which characters appear (matched against the global character library, or flagged as "new character needed").

**Audio + timestamps**: ElevenLabs TTS called **per scene** (not once for the whole script) via the timestamps endpoint, so each scene gets its own audio file and word-level timing JSON. The resulting duration per scene becomes the target duration for that scene's image-to-video generation. Also concatenate into `full_narration.mp3` for final mux, and merge timestamp files into one global timeline for caption burning.

**Storyboard / images**: For each scene, generate an image via the configured image model (fal.ai), attaching the reference images of any characters tagged for that scene. User reviews the whole storyboard grid and approves or requests regeneration per-scene.

**Video clips**: Only for scenes marked `video` (default most scenes are `still`; the user or the LLM flags a handful of "hero" scenes for motion). Image-to-video generation via fal.ai, duration matched to the scene's audio length.

**Final render (ffmpeg)**:
- Concatenate scene visuals in order (still images become a video segment via a subtle Ken Burns pan/zoom at the scene's exact duration; generated clips are used as-is, trimmed/padded to match audio duration)
- Mux in `full_narration.mp3`
- Burn in **word-synced captions** as an overlay, driven directly by the merged timestamp data (karaoke-style word highlighting is a nice-to-have, plain synced captions is the requirement)
- Output to `final/<slug>.mp4`

**Metadata generation**: After render, one more LLM call (or reuse the script-generation call) produces `final/metadata.json`:
```json
{
  "title": "...",
  "description": "...",
  "hashtags": ["#greekmythology", "#theseus", "..."],
  "suggested_caption": "title + description + hashtags combined, ready to paste into TikTok"
}
```
Feel free to include whatever additional fields are useful for a creator's workflow (e.g. a one-line hook/thumbnail text suggestion) — use your judgment here.

## 10. UI/UX requirements

Tone: **calm, minimalist, modern** — generous whitespace, muted palette, no dashboard clutter or busy gradients. But it must still surface everything needed at a glance; minimalist should not mean hiding information behind extra clicks.

Screens:

1. **Board view (home)** — kanban-style columns matching pipeline stages, one card per project. Each card shows title, thumbnail (once available), target duration, and a status pill. A single clear "+ New Project" action.
2. **Project detail / storyboard view** — the main workspace. Scene-by-scene vertical list or grid: narration text, image thumbnail, audio waveform/duration, scene type (still/video) toggle, per-scene approve/regenerate controls, and one clear "Approve stage" action to move the whole project forward. Live progress indicators while a stage is generating (not a frozen screen).
3. **Character library** — grid of global characters with reference image, name, description, "used in N projects." Add/edit character flow generates or uploads a reference image.
4. **Idea backlog** — a lightweight list of queued topic ideas with target duration, waiting to be turned into projects.
5. **Settings** — platform presets and content presets (create/edit/select default for each, independently), default duration, API key status (masked, just confirms `.env` is loaded — never display raw keys in the UI). No model-switching UI for v1 (see section 4a) — the active model per stage is currently a config/constants concern, not something exposed here yet.

## 11. Suggested build order

1. Scaffold FastAPI + DB models + folder structure + `.env` loading
2. Script generation endpoint + the three-layer prompt system (core prompt in code, platform presets + content presets in DB) — get this fully working via API/CLI before touching UI
3. Audio + timestamps pipeline
4. Character library CRUD + image generation
5. Storyboard/image generation per scene, wired to characters
6. Video clip generation for flagged scenes
7. FFmpeg render + caption burning + metadata generation
8. Frontend: board view → project detail/storyboard view → character library → settings → idea backlog
9. Wire up progress polling/streaming so the UI reflects background job state live

## 12. Open implementation decisions left to you

Where this spec doesn't dictate an exact approach (e.g. exact caption styling, exact Ken Burns implementation, exact job queue library, component structure), use good judgment and standard practices — the priorities above (calm/minimalist UI, clean folder structure, editable prompts, per-scene approval) are the hard requirements; everything else is an implementation detail.