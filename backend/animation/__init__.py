"""Deterministic animation engine (math/science scenes).

An ``animation`` scene is AI-authored Manim code (the body of ``construct(self)``)
rather than an AI image/video. The code has full access to the Manim library and
is rendered in an isolated subprocess (see ``backend/adapters/animation.py``).
Voice sync is done at render time: the code calls ``self.cue("phrase")`` to look
up when a phrase is spoken in the scene's ElevenLabs word timestamps, so events
land on the words.

Engine-agnostic, dependency-light pieces here (no Manim import) so they unit-test
on their own:

- ``catalog``   — the Manim authoring guide, the ``{code, title}`` schema, and
                  ``normalize_spec`` (validation/cleaning).
- ``narration`` — ``build_cue_schedule``: resolves every ``self.cue(...)`` in the
                  authored code to a spoken second, in order, before rendering.
"""
from .catalog import ANIMATION_OBJECT_SCHEMA, catalog_for_prompt, normalize_spec
from .narration import CueTimeline, build_cue_schedule, phrase_time

__all__ = [
    "ANIMATION_OBJECT_SCHEMA",
    "CueTimeline",
    "build_cue_schedule",
    "catalog_for_prompt",
    "normalize_spec",
    "phrase_time",
]
