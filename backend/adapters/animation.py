"""Animation rendering adapters.

``ManimAnimationGenerator`` executes AI-authored Manim code. The code (the body
of ``construct(self)``) is wrapped in a runner script that provides:

- portrait 9:16 render config,
- the scene's narration words + ``DURATION``, and the cue schedule resolved from
  the body's ``self.cue(...)`` calls, and
- a ``_SyncScene`` base with ``self.cue("phrase")`` / ``self.hold_until(t)`` /
  ``self.play_at(t, ...)`` so events bind to the voice.

Sync is deterministic: ``_SyncScene`` times everything off Manim's own frame
clock (``self.time``) rather than estimating animation lengths, so each hold
re-anchors to absolute narration time and drift cannot compound over a long
scene. ``play_at`` also caps each animation to the gap before the next cue, so
one long animation cannot push the rest of the scene late.

The runner is rendered by the Manim CLI in an **isolated subprocess with a hard
timeout**, so arbitrary generated code cannot hang or crash the app; failures
raise ``RuntimeError`` with the engine's stderr so the pipeline can auto-repair.
Manim is only required in that subprocess (same venv). No LaTeX is needed unless
the code uses ``MathTex``/``Tex``.

``OfflineAnimationGenerator`` is the dependency-free fallback (Pillow + ffmpeg).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from ..animation.latex import subprocess_env
from ..animation.narration import build_cue_schedule
from ..config import settings
from .base import AnimationGenerator

_BG = (11, 11, 15)
_RENDER_TIMEOUT_SECONDS = 240


def _construct_body(code: str) -> str:
    """Return clean construct-body statements from whatever the model returned.

    Tolerates a model that wrapped the code in a full ``def construct`` (takes the
    body) and strips stray top-level manim imports. Raises if nothing is left.
    """
    text = (code or "").strip()
    match = re.search(r"def\s+construct\s*\(\s*self[^)]*\)\s*:\s*\n", text)
    if match:
        text = text[match.end():]
    lines = [
        line for line in text.splitlines()
        if line.strip() not in ("from manim import *", "import manim")
    ]
    text = textwrap.dedent("\n".join(lines)).strip("\n")
    if not text.strip():
        raise RuntimeError("empty animation code")
    return text


def _runner_script(code: str, words: list[dict], duration: float, size: tuple[int, int]) -> str:
    """Assemble the full Manim runner script around the AI construct body.

    Every cue in the body is resolved against the word timings *here*, before the
    render starts, so the runner has the complete timeline (see ``_SyncScene``).
    """
    width, height = size
    frame_height = round(8.0 * height / width, 4)
    project_root = str(settings.root_dir)
    body = _construct_body(code)
    words_json = json.dumps(words or [])
    cues_json = json.dumps(build_cue_schedule(body, words or [], duration))
    # Header is an f-string (no literal braces inside); the AI body is concatenated
    # afterwards so its dict/set braces never interfere with formatting.
    header = f'''import sys, json
sys.path.insert(0, {project_root!r})
from manim import *

config.pixel_width = {width}
config.pixel_height = {height}
config.frame_width = 8.0
config.frame_height = {frame_height}
config.frame_rate = 30
config.background_color = "#0b0b0f"

WORDS = json.loads({words_json!r})
DURATION = {float(duration)}
# Every self.cue(...) in the body, resolved in source order. See
# backend/animation/narration.py :: build_cue_schedule.
CUES = json.loads({cues_json!r})
_CUE_TIMES = sorted(c["time"] for c in CUES if c["time"] is not None)
_FRAME = 1.0 / config.frame_rate


class _SyncScene(Scene):
    """Bind animation events to absolute narration time.

    All timing reads ``self.time`` — Manim's own clock, which counts the frames
    actually written. Nothing here estimates how long an animation takes, so
    every hold re-anchors to the audio and timing error cannot accumulate.
    """

    def setup(self):
        self._cue_index = 0
        self._live = None

    def cue(self, phrase, default=None):
        """The second at which `phrase` is spoken, or `default` if never."""
        for i in range(self._cue_index, len(CUES)):
            if CUES[i]["phrase"] == phrase:
                self._cue_index = i + 1
                time = CUES[i]["time"]
                return default if time is None else time
        # Not in the pre-resolved schedule: a phrase built at runtime (a loop or
        # an f-string). Resolve it live from where the narration has got to.
        if self._live is None:
            from backend.animation.narration import CueTimeline

            self._live = CueTimeline(WORDS, DURATION)
        time = self._live.resolve(phrase)
        return default if time is None else time

    def hold_until(self, t):
        """Wait until absolute second `t` of the narration. Never rewinds."""
        try:
            target = float(t)
        except (TypeError, ValueError):
            return  # unresolved cue: stay put rather than jumping to the start
        dt = target - self.time
        if dt >= _FRAME:
            self.wait(dt)

    def _budget(self, start):
        """Whole frames available before the next cue is spoken."""
        following = [t for t in _CUE_TIMES if t > start + 1e-6]
        gap = (following[0] if following else DURATION) - start
        return max(_FRAME, int(max(0.0, gap) * config.frame_rate + 1e-6) * _FRAME)

    @staticmethod
    def _natural_run_time(anims):
        """What Manim would have played these for, left alone."""
        times = [float(a.run_time) for a in anims
                 if isinstance(a, Animation) and getattr(a, "run_time", None)]
        return max(times) if times else 1.0

    def play_at(self, t, *anims, **kwargs):
        """Hold until `t`, then play — capped so it cannot overrun the next cue."""
        self.hold_until(t)
        if not anims:
            return
        requested = kwargs.get("run_time") or self._natural_run_time(anims)
        kwargs["run_time"] = min(float(requested), self._budget(self.time))
        self.play(*anims, **kwargs)


class GeneratedScene(_SyncScene):
    def construct(self):
'''
    body = textwrap.indent(body, " " * 8)
    tail = "\n        self.hold_until(DURATION)\n"
    return header + body + tail


class ManimAnimationGenerator(AnimationGenerator):
    """Execute AI-authored Manim code in an isolated subprocess."""

    name = "manim"

    def render(self, spec, words, out_path, duration_seconds, size=(1080, 1920)):
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        duration = max(1.0, float(duration_seconds or 1.0))
        code = (spec or {}).get("code", "")
        script = _runner_script(code, words or [], duration, size)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            script_path = tmp_path / "scene.py"
            script_path.write_text(script, encoding="utf-8")
            media_dir = tmp_path / "media"

            cmd = [
                sys.executable, "-m", "manim", "render",
                str(script_path), "GeneratedScene",
                "--media_dir", str(media_dir),
                "--format", "mp4",
                "--output_file", out_path.stem,
                "--disable_caching",
                "-v", "error",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=settings.root_dir,
                    capture_output=True,
                    text=True,
                    timeout=_RENDER_TIMEOUT_SECONDS,
                    env=subprocess_env(),
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"Manim render timed out after {_RENDER_TIMEOUT_SECONDS}s"
                ) from exc

            if proc.returncode != 0:
                raise RuntimeError(_clean_manim_error(proc.stderr or proc.stdout))

            produced = _newest_mp4(media_dir)
            if not produced:
                raise RuntimeError(_clean_manim_error(proc.stderr) or "Manim produced no output file")
            shutil.copyfile(produced, out_path)
        return out_path


def _newest_mp4(media_dir: Path) -> Path | None:
    candidates = sorted(media_dir.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _clean_manim_error(text: str | None) -> str:
    """Trim Manim's stderr to the useful tail for display / auto-repair."""
    text = (text or "").strip()
    if not text:
        return ""
    return text[-2600:]


# --------------------------------------------------------------------------- #
# Offline fallback (Pillow + ffmpeg) — keeps the pipeline runnable w/o Manim.
# --------------------------------------------------------------------------- #
class OfflineAnimationGenerator(AnimationGenerator):
    """A titled placeholder card at the right duration. No heavy deps."""

    name = "offline"

    def render(self, spec, words, out_path, duration_seconds, size=(1080, 1920)):
        from PIL import Image, ImageDraw

        from ..pipeline import _title_font  # reuse the app's font resolver
        from ..services.ffmpeg import ken_burns_clip

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        width, height = size

        image = Image.new("RGB", (width, height), _BG)
        draw = ImageDraw.Draw(image)
        title = ((spec or {}).get("title") or "Animation").strip()

        draw.text((width / 2, height * 0.4), "ANIMATION", font=_title_font(46, "kicker"),
                  fill=(120, 130, 160), anchor="mm")
        draw.text((width / 2, height * 0.5), title, font=_title_font(70), fill=(242, 242, 247), anchor="mm")
        draw.text((width / 2, height * 0.57), "install Manim to render", font=_title_font(30, "kicker"),
                  fill=(120, 120, 130), anchor="mm")

        poster = out_path.with_suffix(".poster.png")
        image.save(poster, "PNG")
        try:
            ken_burns_clip(poster, out_path, max(1.0, float(duration_seconds or 1.0)))
        finally:
            poster.unlink(missing_ok=True)
        return out_path
