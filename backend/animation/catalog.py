"""The Manim authoring contract between the script LLM and the renderer.

The AI writes real Manim Community code — the body of ``construct(self)`` — with
full access to the library (any mobject, any animation, arbitrary positioning).
This module supplies:

  1. ``catalog_for_prompt`` — the authoring guide injected into the script prompt
     (canvas, the voice-sync helpers, LaTeX availability, constraints, example).
  2. ``ANIMATION_OBJECT_SCHEMA`` — the JSON-schema fragment for the ``animation``
     object: ``{code, title}``.
  3. ``normalize_spec`` — light validation/cleaning of the returned spec.

The code is executed in an isolated subprocess with a timeout (see
``backend/adapters/animation.py``); it is not run in-process.
"""
from __future__ import annotations

from typing import Any

__all__ = ["ANIMATION_OBJECT_SCHEMA", "catalog_for_prompt", "normalize_spec", "MAX_CODE_CHARS"]

MAX_CODE_CHARS = 24000


def catalog_for_prompt(latex_available: bool = False) -> str:
    """The Manim authoring guide injected into the script prompt when enabled."""
    latex_note = (
        "LaTeX IS available: use MathTex(...) / Tex(...) for real typeset math "
        "(fractions, integrals, exponents, Greek letters). Use Text(...) for plain "
        "prose labels."
        if latex_available
        else "LaTeX is NOT installed: do NOT use MathTex or Tex. Use Text(...) for "
        "every label, including math (e.g. Text('x^2') or Text('integral of x')). "
        "MathAnimations that require LaTeX will fail."
    )
    return f"""\
## Writing an animation scene (full Manim access)

For an animation scene, write the BODY of `construct(self)` for a Manim Community
(v0.19) Scene, and put it in the animation object's `code` field. You have the
ENTIRE Manim library: any mobject (Axes, NumberPlane, Text, Circle, Polygon,
Arrow, Line, Dot, Matrix, Table, VGroup, ...), any animation (Create, Write,
FadeIn, Transform, ReplacementTransform, GrowFromCenter, MoveAlongPath, Rotate,
Indicate, ...), updaters, ValueTracker, and free positioning
(.move_to / .next_to / .shift / .to_edge / .arrange / .get_center()). Place text
and objects wherever you want.

Rules for the code (IMPORTANT):
- Write ONLY the statements inside construct. Do NOT write `import`, the `class`
  line, or the `def construct` line. `from manim import *` is already in scope.
- The frame is PORTRAIT 9:16 (1080x1920). World coordinates: x runs about -4.0
  (left) to 4.0 (right), y runs about -7.1 (bottom) to 7.1 (top), origin at the
  center. Keep everything inside x in [-3.7, 3.7] and y in [-6.8, 6.8].
- The background is dark (#0b0b0f). Use bright colors (WHITE, BLUE, YELLOW,
  GREEN, RED, TEAL, ORANGE, PURPLE).
- {latex_note}
- No file, network, OS, or system access. Pure Manim + Python math only. Do not
  call config.* or self.interactive_embed(). Keep total submobjects reasonable.

Sync to the narration (this is what makes it feel authored to the voice):
- The clip MUST last `DURATION` seconds (a global float — the scene's spoken
  length). A final wait to reach it is added automatically.
- Helpers available on `self` (plus the globals `DURATION` and `WORDS`):
  * `self.cue("a phrase")` -> the second at which that phrase is spoken (float).
  * `self.hold_until(t)` -> wait until absolute time `t` seconds.
  * `self.play_at(t, *animations, run_time=...)` -> wait until `t`, then play.
  * `WORDS` -> list of {{word, start, end}} for the whole scene.
- Use `self.play_at(self.cue("..."), ...)` for EVERY reveal, e.g.
  `self.play_at(self.cue("the curve"), Create(curve))` or
  `self.play_at(self.cue("sixteen"), FadeIn(answer))`. Each event is held to its
  absolute spoken second, so an animation that runs long cannot push later ones
  off the voice — but only if you anchor every one of them.
- Phrases MUST be copied VERBATIM from this scene's narration_text — same words,
  same spelling, same number form ("sixteen", not "16"). A phrase that is not in
  the narration cannot be timed, and its animation just plays straight after the
  previous one.
- Write your cues in the order they are spoken. Repeating a phrase is fine: each
  `self.cue(...)` call takes the NEXT time that phrase is spoken, so a narration
  that says "the curve" four times can anchor four different reveals to it.
- Pick 3-8 word phrases that appear once, not single common words ("the", "is").
- Do NOT compute your own pacing: no `self.wait(...)` between events and no
  arithmetic on cue times. `run_time` is automatically capped so an animation
  ends before the next cue, so just state the run_time the motion wants.

Example `code` (narration: "As x grows, the area under the curve climbs to sixteen"):
```
axes = Axes(x_range=[0, 4], y_range=[0, 16, 4], x_length=6, y_length=7.5,
            axis_config={{"include_numbers": True}}).move_to([0, -0.5, 0])
curve = axes.plot(lambda x: x**2, x_range=[0, 4], color=BLUE)
area = axes.get_area(curve, x_range=[0, 4], color=BLUE, opacity=0.4)
answer = Text("= 16", color=YELLOW, font_size=64).to_edge(DOWN, buff=1.0)
self.play_at(self.cue("As x grows"), Create(axes), run_time=0.8)
self.play_at(self.cue("the area"), Create(curve), run_time=1.0)
self.play_at(self.cue("under the curve"), FadeIn(area), run_time=0.8)
self.play_at(self.cue("climbs to sixteen"), FadeIn(answer, shift=UP * 0.3), run_time=0.5)
```
Note how the four phrases appear in the narration in exactly that order, each
copied word for word.

Prefer an animation whenever the frame is a diagram, graph, equation, formula,
labeled figure, number line, table, counter, or a clean text-plus-equation-plus-
shapes card (summary / recap / definition / worked-example / check cards) — Manim
renders these exactly and can restate an earlier equation 1:1. Reserve still/video
for genuinely photographic or scenic real-world imagery a diagram cannot convey.\
"""


ANIMATION_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Only when scene_type is 'animation'. AI-authored Manim scene code that "
        "renders a deterministic diagram synced to the narration."
    ),
    "properties": {
        "title": {
            "type": "string",
            "description": "Optional short label for this animation (the code may also draw its own title).",
        },
        "code": {
            "type": "string",
            "description": (
                "The body of construct(self): Manim Community Python statements. "
                "No imports, no class, no def. See the animation authoring guide."
            ),
        },
    },
    # Structured outputs require additionalProperties:false and every listed
    # property in `required`. `title` may be an empty string for untitled scenes.
    "required": ["title", "code"],
    "additionalProperties": False,
}


def normalize_spec(raw: Any) -> dict | None:
    """Clean an animation spec (from the LLM or a user edit) or return None.

    Requires a non-empty ``code`` string; returns ``{code, title}`` so callers
    can treat a spec without code as "no animation yet".
    """
    if not isinstance(raw, dict):
        return None
    code = raw.get("code")
    if not isinstance(code, str) or not code.strip():
        return None
    code = code.strip()
    if len(code) > MAX_CODE_CHARS:
        code = code[:MAX_CODE_CHARS]
    title = raw.get("title")
    return {"code": code, "title": str(title).strip() if isinstance(title, str) else ""}
