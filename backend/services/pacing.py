"""Cheap pre-production checks for scripts that would not play well as posted.

The checks deliberately run before TTS or image generation.  They cannot judge a
performance that does not exist yet, but they can catch the two structural
failures we care about: a script so long that a feed viewer leaves before the
payoff, and too many or too few distinct panels for the amount of narration a
calm voice would give them.

Neither check is a hard limit.  Each one produces rewrite feedback, the script
is regenerated once with that feedback attached, and whatever comes back is
what ships — a slightly long video is a far better outcome than a story with
its ending cut off.
"""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # importing the adapters package here would be circular:
    # prompts reads the constants below, and the adapters import prompts.
    from ..adapters.base import GeneratedScript


_WORD_RE = re.compile(r"\b[\w’'-]+\b", re.UNICODE)
_CALM_WORDS_PER_SECOND = 145 / 60

# How much longer than its duration reference a script may run before the draft
# is sent back. Runtime is elastic because a story needs the room its own beats
# ask for, but short-form is watched in a feed: past roughly twice the
# reference, the thing being protected stops being comprehension and starts
# being padding nobody watches to the end of.
MAX_RUNTIME_FACTOR = 2.0

# How much longer than the group's ``panel_seconds`` floor a panel may be held.
PANEL_HOLD_BAND = 2.5

# The longest any panel may be held, whatever the group's ``panel_seconds`` asks
# for. MEASURED on posted videos: a render that averaged 9.5 seconds a panel
# passed this review clean, because a group configured for a leisurely
# panel_seconds moved the whole band up with it. A feed viewer has read a panel
# well inside three seconds; everything after that is a wait, and the wait is
# what reads as slow. So the band has a hard lid that the group setting can
# lower but not raise.
MAX_PANEL_HOLD_SECONDS = 3.0

# Below this a panel is a flash, not a beat.
MIN_PANEL_HOLD_SECONDS = 1.2


def panel_hold_band(panel_seconds: float) -> tuple[float, float]:
    """The (floor, ceiling) seconds one panel may be held for this group.

    Shared with ``prompts._panel_guidance`` so the authoring instruction and the
    review below describe the same band rather than drifting apart. The ceiling
    is capped at ``MAX_PANEL_HOLD_SECONDS`` and the floor follows it down, which
    keeps the band a usable width instead of collapsing to a point when a group
    asks for a hold the cap will not grant.
    """
    requested = max(1.5, float(panel_seconds or 0))
    ceiling = min(requested + PANEL_HOLD_BAND, MAX_PANEL_HOLD_SECONDS)
    floor = max(MIN_PANEL_HOLD_SECONDS, ceiling - PANEL_HOLD_BAND)
    return floor, ceiling


def script_word_count(script: GeneratedScript) -> int:
    return sum(
        len(_WORD_RE.findall(scene.narration_text or ""))
        for scene in script.scenes
    )


def estimated_runtime_seconds(script: GeneratedScript) -> float:
    """Spoken runtime at a calm 145 words per minute.

    Measured against finished renders from this pipeline, which land within a
    few percent of it. Estimating from words rather than from the requested
    duration is the point: the requested duration is a reference the script is
    allowed to move away from, so it cannot say how long the draft actually is.
    """
    return script_word_count(script) / _CALM_WORDS_PER_SECOND


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _runtime_feedback(script: GeneratedScript, target_seconds: float) -> str:
    """Rewrite feedback when the draft would run well past its reference."""
    ceiling = target_seconds * MAX_RUNTIME_FACTOR
    estimated = estimated_runtime_seconds(script)
    if not target_seconds or estimated <= ceiling:
        return ""
    budget = int(round(ceiling * _CALM_WORDS_PER_SECOND * 0.95))
    return (
        f"The draft runs about {script_word_count(script)} spoken words, which is "
        f"roughly {_clock(estimated)} of narration — well past the {_clock(ceiling)} "
        f"ceiling for a {target_seconds:.0f}-second reference. Rewrite it to about "
        f"{budget} words or fewer.\n"
        "Keep every story beat, including the ending. Take the length out of the "
        "telling, not out of the plot: cut restatement and recap, cut adjectives "
        "and clauses that decorate a beat already made, merge two sentences that "
        "carry one idea, and drop background detail the payoff does not need. "
        "Prefer shorter sentences with concrete verbs. A beat that carries "
        "consequence or a turn keeps its room; a beat that only sets atmosphere "
        "loses it."
    )


def panel_pacing_feedback(
    script: GeneratedScript,
    *,
    panels_mode: bool,
    panel_seconds: float,
    target_seconds: float = 0.0,
) -> str:
    """Return actionable rewrite feedback, or an empty string when it reads well.

    Two independent findings, combined into one set of notes so a single rewrite
    can fix both. Length is checked for every group; panel density only for the
    groups that tell their stories in panels.

    ``panel_seconds`` asks for a readability band, which ``panel_hold_band``
    grants up to its cap. Panel holds are measured against the runtime the script is
    being asked to have — a draft that is told to lose a third of its words
    would otherwise be told, in the same breath, to add panels for words that
    are about to be cut.
    """
    if not script.scenes:
        return ""
    words = script_word_count(script)
    if not words:
        return ""

    notes = []
    runtime = _runtime_feedback(script, target_seconds)
    if runtime:
        notes.append(runtime)

    if not panels_mode:
        return "\n\n".join(notes)

    estimated_seconds = estimated_runtime_seconds(script)
    # After a trim, if one was asked for: the panels have to fit the script the
    # rewrite is meant to produce, not the one being rejected.
    planned_seconds = (
        min(estimated_seconds, target_seconds * MAX_RUNTIME_FACTOR)
        if runtime
        else estimated_seconds
    )
    average_hold = planned_seconds / len(script.scenes)
    floor, ceiling = panel_hold_band(panel_seconds)
    at_that_length = " at that length" if runtime else ""
    if average_hold > ceiling * 1.1:
        minimum_panels = max(1, math.ceil(planned_seconds / ceiling))
        notes.append(
            f"The draft has only {len(script.scenes)} panels for "
            f"{_clock(planned_seconds)} of narration. Each image would linger for "
            f"about {average_hold:.1f} seconds{at_that_length}, above this group's "
            f"comfortable {floor:.1f}-{ceiling:.1f}-second range, which is what "
            f"makes a video feel slow. Use roughly {minimum_panels} or more "
            "panels. Split places where the time, location, physical action, "
            "point of view, or emotional beat genuinely changes; do not split "
            "clauses that still describe one visual moment."
        )
    elif average_hold < floor * 0.9:
        comfortable_panels = max(1, math.floor(planned_seconds / floor))
        notes.append(
            f"The draft has {len(script.scenes)} panels for "
            f"{_clock(planned_seconds)} of narration. That gives each image only "
            f"about {average_hold:.1f} seconds{at_that_length}, below this group's "
            f"{floor:.1f}-second readability target. Use roughly "
            f"{comfortable_panels} or fewer panels, combining adjacent clauses "
            "that share one visual moment. Keep every essential event."
        )
    return "\n\n".join(notes)
